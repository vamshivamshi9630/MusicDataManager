import sys
import logging
import traceback
import os
from pathlib import Path
import uvicorn
from fastapi import FastAPI, Request, status, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError

from backend.core.config import settings
from backend.api.routes import router as local_router
from backend.api.auth_routes import router as auth_router
from backend.api.cloud_routes import router as cloud_job_router, ws_router as cloud_ws_router
from backend.services.validation import FileValidationError
from backend.core.repository import PathTraversalError, CloudWorkspaceError
from backend.services.generator import GeneratorValidationError
from backend.services.git_sync import GitSyncError

logger = logging.getLogger("musicdata")

app = FastAPI(
    title="MusicData Manager API & Agent (Dual Mode: Cloud + Local)",
    version="2.0.0",
    description="Automated MusicData repository management, metadata generation, and Git synchronization backend."
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Custom Exception Handlers to Enforce Strict JSON API Contract
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    detail = exc.detail
    if isinstance(detail, dict):
        return JSONResponse(
            status_code=exc.status_code,
            content={"success": False, "error": detail.get("message", str(detail)), **detail}
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "stage": "http_request",
            "error": str(detail)
        }
    )

@app.exception_handler(GeneratorValidationError)
async def generator_validation_exception_handler(request: Request, exc: GeneratorValidationError):
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "success": False,
            "stage": exc.stage,
            "exit_code": exc.exit_code,
            "error": exc.message,
            "stdout": exc.stdout,
            "stderr": exc.stderr
        }
    )

@app.exception_handler(GitSyncError)
async def git_sync_exception_handler(request: Request, exc: GitSyncError):
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "success": False,
            "stage": getattr(exc, "stage", "git_sync"),
            "exit_code": getattr(exc, "exit_code", 1),
            "error": exc.message,
            "stdout": getattr(exc, "stdout", ""),
            "stderr": getattr(exc, "stderr", "")
        }
    )

@app.exception_handler(CloudWorkspaceError)
async def cloud_workspace_exception_handler(request: Request, exc: CloudWorkspaceError):
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "success": False,
            "stage": "cloud_workspace",
            "exit_code": 1,
            "error": str(exc)
        }
    )

@app.exception_handler(FileValidationError)
async def file_validation_exception_handler(request: Request, exc: FileValidationError):
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "success": False,
            "stage": "file_validation",
            "error": str(exc)
        }
    )

@app.exception_handler(PathTraversalError)
async def path_traversal_exception_handler(request: Request, exc: PathTraversalError):
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "success": False,
            "stage": "security_check",
            "error": str(exc)
        }
    )

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    error_trace = traceback.format_exc()
    logger.error(f"[SERVER ERROR 500] Unhandled exception on {request.url}: {exc}\n{error_trace}")
    print(f"[SERVER ERROR 500] Unhandled exception on {request.url}: {exc}\n{error_trace}")
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "success": False,
            "stage": "internal_server_error",
            "error": f"Server processing failed: {str(exc)}"
        }
    )

app.include_router(local_router)
app.include_router(auth_router)
app.include_router(cloud_job_router)
app.include_router(cloud_ws_router)

frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
if frontend_dir.exists():
    app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")

@app.get("/")
def read_root():
    index_path = frontend_dir / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return {
        "app": "MusicData Manager API & Agent",
        "version": "2.0.0",
        "docs": "/docs",
        "health": "/api/health"
    }

if __name__ == "__main__":
    print(f"Starting MusicData Manager on http://{settings.HOST}:{settings.API_PORT}")
    uvicorn.run("backend.main:app", host=settings.HOST, port=settings.API_PORT, reload=True)


# Startup checks
@app.on_event("startup")
def startup_checks():
    cloud_mode = os.environ.get("CLOUD_MODE", "").strip().lower() in ("1", "true")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("AGENT_AUTH_TOKEN")
    if cloud_mode and not token:
        logger.warning("Running in CLOUD_MODE but no GITHUB_TOKEN or AGENT_AUTH_TOKEN detected. \n"
                       "Cloud sync push operations will fail until a token or GitHub App is configured. \n"
                       "See docs/GitHub-Deploy.md for setup instructions.")
        print("[WARN] CLOUD_MODE active but no GITHUB_TOKEN or AGENT_AUTH_TOKEN set. See docs/GitHub-Deploy.md")
