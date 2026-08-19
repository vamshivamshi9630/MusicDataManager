import json
import time
import asyncio
from typing import Dict, Any, Optional, List, Tuple
from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks, WebSocket, WebSocketDisconnect, Header, Query, status
from pydantic import BaseModel, Field

from backend.api.auth import get_current_user
from backend.services.job_manager import (
    job_manager,
    JobStatus,
    JobModel,
    JobConflictError,
    JobNotFoundError,
    JobAccessDeniedError
)
from backend.services.cloud_pipeline import CloudPipelineWorker

router = APIRouter(prefix="/api/jobs", tags=["Cloud Sync Jobs"])
ws_router = APIRouter(prefix="/api/ws", tags=["Cloud Job WebSockets"])

class SyncJobRequest(BaseModel):
    album_name: str = Field(..., description="Target album name to synchronize")
    staging_session_id: Optional[str] = Field(None, description="Staging session ID containing uploaded files")
    idempotency_key: Optional[str] = Field(None, description="Idempotency key to prevent duplicate submissions")
    modified_songs: List[Tuple[str, str]] = Field(default_factory=list, description="List of (album_name, song_filename) modified/uploaded")
    rename_operations: List[Dict[str, Any]] = Field(default_factory=list, description="List of song rename operations")
    push_enabled: bool = Field(False, description="Enable remote GitHub push (False for testing/dry-run)")

@router.post("/sync", status_code=status.HTTP_202_ACCEPTED)
def create_cloud_sync_job(
    req: SyncJobRequest,
    background_tasks: BackgroundTasks,
    user: Dict[str, Any] = Depends(get_current_user)
) -> Dict[str, Any]:
    user_id = user.get("sub", "admin")
    job_id = f"job_{int(time.time() * 1000)}_{user_id}"

    try:
        job = job_manager.create_job(
            job_id=job_id,
            user_id=user_id,
            album_name=req.album_name,
            idempotency_key=req.idempotency_key,
            staging_session_id=req.staging_session_id,
            modified_songs=req.modified_songs,
            rename_operations=req.rename_operations
        )
    except JobConflictError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))

    if job.job_id != job_id:
        return {
            "duplicate_request": True,
            "message": "Idempotent job already in progress or completed.",
            "job": job.to_dict()
        }

    worker = CloudPipelineWorker()
    background_tasks.add_task(
        worker.execute_cloud_sync_job,
        job_id=job.job_id,
        push_enabled=req.push_enabled
    )

    return {
        "success": True,
        "message": f"Sync job initialized for album '{req.album_name}'.",
        "job": job.to_dict()
    }

@router.get("/{job_id}")
def get_job_status(
    job_id: str,
    user: Dict[str, Any] = Depends(get_current_user)
) -> Dict[str, Any]:
    user_id = user.get("sub", "admin")
    try:
        job = job_manager.get_job(job_id, user_id=user_id)
        return job.to_dict()
    except JobNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except JobAccessDeniedError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))

@router.post("/{job_id}/cancel")
def cancel_job(
    job_id: str,
    user: Dict[str, Any] = Depends(get_current_user)
) -> Dict[str, Any]:
    user_id = user.get("sub", "admin")
    try:
        job = job_manager.request_cancel_job(job_id, user_id=user_id)
        return {
            "success": True,
            "message": "Job cancellation requested.",
            "job": job.to_dict()
        }
    except JobNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except JobAccessDeniedError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


@ws_router.websocket("/jobs/{job_id}")
async def job_websocket(
    websocket: WebSocket,
    job_id: str,
    token: Optional[str] = Query(None)
):
    await websocket.accept()
    
    if token:
        user_id = "admin"
    else:
        user_id = "admin"

    try:
        while True:
            try:
                job = job_manager.get_job(job_id, user_id=user_id)
                data = {
                    "job_id": job.job_id,
                    "status": job.status.value,
                    "progress": job.progress,
                    "current_step": job.current_step,
                    "message": job.message,
                    "error": job.error,
                    "result": job.result
                }
                await websocket.send_text(json.dumps(data))
                
                if job.status in (JobStatus.VERIFIED, JobStatus.FAILED, JobStatus.CANCELLED):
                    break
            except Exception as e:
                await websocket.send_text(json.dumps({"error": str(e)}))
                break

            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        pass
