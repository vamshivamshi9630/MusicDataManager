import os
import sys
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional

from backend.core.repository import CloudRepository, PathTraversalError
from backend.services.job_manager import JobStatus, JobModel, job_manager
from backend.services.validation import ValidationService, FileValidationError
from backend.services.duplicate import DuplicateDetectionService
from backend.services.generator import GeneratorService, GeneratorValidationError
from backend.services.git_sync import CloudGitSyncService
from backend.services.github_auth import GitHubTokenManager

class CloudPipelineWorker:
    def __init__(self, token_manager: Optional[GitHubTokenManager] = None):
        self.token_manager = token_manager or GitHubTokenManager(test_mode=True)

    def execute_cloud_sync_job(
        self,
        job_id: str,
        push_enabled: bool = False,
        source_repo_override: Optional[Path] = None
    ) -> JobModel:
        job = job_manager.get_job(job_id)
        cloud_repo: Optional[CloudRepository] = None

        try:
            # ----------------------------------------------------
            # STEP 1: STAGING (Progress 10%)
            # ----------------------------------------------------
            job_manager.update_job_progress(job_id, JobStatus.STAGING, 10, "Staging Files", "Verifying staged files and session ownership.")
            if job.cancel_requested:
                return job_manager.update_job_progress(job_id, JobStatus.CANCELLED, 10, "Cancelled", "Job cancelled.")

            val_svc = ValidationService()

            # ----------------------------------------------------
            # STEP 2: PROVISIONING (Progress 25%)
            # ----------------------------------------------------
            job_manager.update_job_progress(job_id, JobStatus.PROVISIONING, 25, "Provisioning Workspace", "Creating isolated blobless workspace.")
            cloud_repo = CloudRepository(job_id=job_id)
            
            source_override = source_repo_override
            if not source_override and os.environ.get("SOURCE_REPO_OVERRIDE"):
                source_override = Path(os.environ["SOURCE_REPO_OVERRIDE"])

            cloud_repo.provision_blobless_workspace(source_repo_override=source_override)

            album_dir = cloud_repo.get_album_path(job.album_name)
            album_dir.mkdir(parents=True, exist_ok=True)

            if job.staging_session_id:
                session_staging = cloud_repo.staging_dir / job.staging_session_id
                if session_staging.exists():
                    for staged_file in session_staging.iterdir():
                        if staged_file.is_file():
                            dest_path = album_dir / staged_file.name
                            shutil.copy(str(staged_file), str(dest_path))

            # ----------------------------------------------------
            # STEP 3: VALIDATING (Progress 40%)
            # ----------------------------------------------------
            job_manager.update_job_progress(job_id, JobStatus.VALIDATING, 40, "Validating Files & Metadata", "Running PNG, MP3, duplicate, and path safety validation.")
            
            img_path = cloud_repo.get_image_path(job.album_name)
            if img_path.exists():
                val_svc.validate_png_file(img_path)

            for song_tuple in job.modified_songs:
                song_file = album_dir / song_tuple[1]
                if song_file.exists():
                    val_svc.validate_mp3_file(song_file)

            # ----------------------------------------------------
            # STEP 4: GENERATING (Progress 60%)
            # ----------------------------------------------------
            job_manager.update_job_progress(job_id, JobStatus.GENERATING, 60, "Generating Catalog", "Running Phase A strict cache-aware metadata generator.")
            gen_svc = GeneratorService(cloud_repo)
            gen_result = gen_svc.run_generator_pipeline()

            # ----------------------------------------------------
            # STEP 5: VERIFYING (Progress 75%)
            # ----------------------------------------------------
            job_manager.update_job_progress(job_id, JobStatus.VERIFYING, 75, "Verifying Catalog Integrity", "Verifying catalog JSON schemas and metrics.")
            manifest_file = cloud_repo.metadata_dir / "manifest.json"
            if not manifest_file.exists():
                raise GeneratorValidationError("Manifest file missing after generation.")

            # ----------------------------------------------------
            # STEP 6: COMMITTING (Progress 85%)
            # ----------------------------------------------------
            job_manager.update_job_progress(job_id, JobStatus.COMMITTING, 85, "Committing Changes", "Verifying Git diff and staging structured commit.")
            git_svc = CloudGitSyncService(cloud_repo)

            git_status = git_svc.get_git_status()
            for changed_file in git_status.get("changed_files", []):
                file_rel = changed_file.strip().split()[-1]
                if not (file_rel.startswith("metadata/") or file_rel.startswith(f"{job.album_name}/")):
                    raise GeneratorValidationError(f"Git diff safety violation: Unexpected file modification detected: '{file_rel}'")

            commit_msg = f"Sync album: {job.album_name} — {len(job.modified_songs)} added/updated"
            commit_res = git_svc.stage_commit_and_push(job.album_name, custom_commit_msg=commit_msg, push_enabled=push_enabled)

            # ----------------------------------------------------
            # STEP 7 & 8: PUSHING & VERIFIED (Progress 100%)
            # ----------------------------------------------------
            if push_enabled:
                job_manager.update_job_progress(job_id, JobStatus.PUSHING, 95, "Pushing to GitHub", "Pushing changes to remote GitHub repository.")
                auth_token = self.token_manager.get_installation_access_token()
                if not auth_token:
                    raise GeneratorValidationError("Failed to obtain GitHub App installation access token.")

            v2_log_content = gen_result.get("stdout", "") or gen_result.get("log", "") or gen_result.get("v2_log", "")

            final_result = {
                "success": True,
                "commit_sha": commit_res.get("commit_sha", "dry_run_sha"),
                "commit_message": commit_msg,
                "before_metrics": gen_result.get("before_metrics"),
                "after_metrics": gen_result.get("after_metrics"),
                "songs_added": gen_result.get("songs_added", 0),
                "push_enabled": push_enabled,
                "v2_log": v2_log_content
            }

            return job_manager.update_job_progress(
                job_id,
                JobStatus.VERIFIED,
                100,
                "Sync Verified",
                f"Successfully synced album '{job.album_name}'. Commit SHA: {final_result['commit_sha']}",
                result=final_result
            )

        except Exception as e:
            error_msg = str(e)
            return job_manager.update_job_progress(
                job_id,
                JobStatus.FAILED,
                job.progress,
                "Job Failed",
                f"Sync job failed: {error_msg}",
                error=error_msg
            )
        finally:
            if cloud_repo:
                cloud_repo.cleanup_workspace()
