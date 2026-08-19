import time
import threading
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, Optional, Any, List, Set, Tuple

class JobStatus(str, Enum):
    QUEUED = "QUEUED"
    STAGING = "STAGING"
    PROVISIONING = "PROVISIONING"
    VALIDATING = "VALIDATING"
    GENERATING = "GENERATING"
    VERIFYING = "VERIFYING"
    COMMITTING = "COMMITTING"
    PUSHING = "PUSHING"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

class JobConflictError(Exception):
    pass

class JobNotFoundError(Exception):
    pass

class JobAccessDeniedError(Exception):
    pass

@dataclass
class JobModel:
    job_id: str
    user_id: str
    album_name: str
    status: JobStatus = JobStatus.QUEUED
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    progress: int = 0
    current_step: str = "Job Queued"
    message: str = "Job initialized"
    error: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    idempotency_key: Optional[str] = None
    staging_session_id: Optional[str] = None
    modified_songs: List[Tuple[str, str]] = field(default_factory=list)
    rename_operations: List[Dict[str, Any]] = field(default_factory=list)
    cancel_requested: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "user_id": self.user_id,
            "album_name": self.album_name,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "progress": self.progress,
            "current_step": self.current_step,
            "message": self.message,
            "error": self.error,
            "result": self.result,
            "idempotency_key": self.idempotency_key,
            "cancel_requested": self.cancel_requested
        }


class AlbumLockManager:
    """Single-process in-memory album lock registry."""

    def __init__(self):
        self._locks: Dict[str, str] = {}
        self._lock = threading.Lock()

    def lock_album(self, album_name: str, job_id: str) -> bool:
        norm_name = album_name.strip().lower()
        with self._lock:
            if norm_name in self._locks and self._locks[norm_name] != job_id:
                return False
            self._locks[norm_name] = job_id
            return True

    def unlock_album(self, album_name: str, job_id: str) -> bool:
        norm_name = album_name.strip().lower()
        with self._lock:
            if norm_name in self._locks and self._locks[norm_name] == job_id:
                del self._locks[norm_name]
                return True
            return False

    def is_album_locked(self, album_name: str) -> bool:
        norm_name = album_name.strip().lower()
        with self._lock:
            return norm_name in self._locks


class JobManager:
    """In-memory thread-safe Sync Job Manager."""

    def __init__(self):
        self._jobs: Dict[str, JobModel] = {}
        self._idempotency_map: Dict[str, str] = {}
        self.lock_manager = AlbumLockManager()
        self._lock = threading.Lock()

    def create_job(
        self,
        job_id: str,
        user_id: str,
        album_name: str,
        idempotency_key: Optional[str] = None,
        staging_session_id: Optional[str] = None,
        modified_songs: Optional[List[Tuple[str, str]]] = None,
        rename_operations: Optional[List[Dict[str, Any]]] = None
    ) -> JobModel:
        with self._lock:
            # Check Idempotency Key
            if idempotency_key:
                if idempotency_key in self._idempotency_map:
                    existing_job_id = self._idempotency_map[idempotency_key]
                    if existing_job_id in self._jobs:
                        return self._jobs[existing_job_id]

            # Check Album Lock
            if not self.lock_manager.lock_album(album_name, job_id):
                raise JobConflictError(f"Album '{album_name}' is currently locked by an active sync job.")

            job = JobModel(
                job_id=job_id,
                user_id=user_id,
                album_name=album_name,
                idempotency_key=idempotency_key,
                staging_session_id=staging_session_id,
                modified_songs=modified_songs or [],
                rename_operations=rename_operations or []
            )

            self._jobs[job_id] = job
            if idempotency_key:
                self._idempotency_map[idempotency_key] = job_id

            return job

    def get_job(self, job_id: str, user_id: Optional[str] = None) -> JobModel:
        with self._lock:
            if job_id not in self._jobs:
                raise JobNotFoundError(f"Sync job '{job_id}' not found.")
            job = self._jobs[job_id]
            if user_id and job.user_id != user_id:
                raise JobAccessDeniedError(f"Access denied to job '{job_id}'.")
            return job

    def update_job_progress(
        self,
        job_id: str,
        status: JobStatus,
        progress: int,
        current_step: str,
        message: str,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None
    ) -> JobModel:
        with self._lock:
            if job_id not in self._jobs:
                raise JobNotFoundError(f"Sync job '{job_id}' not found.")

            job = self._jobs[job_id]
            job.status = status
            job.progress = progress
            job.current_step = current_step
            job.message = message
            job.updated_at = time.time()

            if result is not None:
                job.result = result
            if error is not None:
                job.error = error

            # Auto unlock album when job finishes terminal state
            if status in (JobStatus.VERIFIED, JobStatus.FAILED, JobStatus.CANCELLED):
                self.lock_manager.unlock_album(job.album_name, job_id)

            return job

    def request_cancel_job(self, job_id: str, user_id: str) -> JobModel:
        with self._lock:
            job = self.get_job(job_id, user_id)
            if job.status in (JobStatus.VERIFIED, JobStatus.FAILED, JobStatus.CANCELLED):
                return job

            job.cancel_requested = True
            job.status = JobStatus.CANCELLED
            job.current_step = "Job Cancelled"
            job.message = "Sync job cancelled by user."
            job.updated_at = time.time()
            self.lock_manager.unlock_album(job.album_name, job_id)
            return job


# Singleton JobManager instance
job_manager = JobManager()
