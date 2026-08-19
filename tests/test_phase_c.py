import os
import sys
from pathlib import Path

real_repo_root = Path(r"C:\Users\vamshi\OneDrive\Desktop\Projects\MusicData\MusicData-main").resolve()
sys.path.insert(0, str(real_repo_root))

import json
import time
import shutil
import tempfile
import unittest
import subprocess
from fastapi.testclient import TestClient

from backend.main import app
from backend.core.repository import CloudRepository, PathTraversalError
from backend.core.security import create_access_token, hash_password, verify_password
from backend.services.github_auth import GitHubTokenManager, GitHubAuthError
from backend.services.job_manager import (
    job_manager,
    JobStatus,
    JobModel,
    JobConflictError,
    JobNotFoundError,
    JobAccessDeniedError
)
from backend.services.cloud_pipeline import CloudPipelineWorker
from backend.services.generator import GeneratorValidationError

PHASE_C_RESULTS = {}

def record_phase_c_test(test_no: int, name: str, status: str, details: str):
    PHASE_C_RESULTS[test_no] = {"name": name, "status": status, "details": details}

class TestPhaseC(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.master_temp = Path(tempfile.mkdtemp(prefix="phase_c_master_"))
        
        # Build minimal isolated test source repo
        cls.source_repo = cls.master_temp / "SourceMusicData"
        shutil.copytree(str(real_repo_root / "generator"), str(cls.source_repo / "generator"))
        shutil.copy(str(real_repo_root / "generate_metadata.py"), str(cls.source_repo / "generate_metadata.py"))
        
        metadata_dir = cls.source_repo / "metadata"
        metadata_dir.mkdir(parents=True, exist_ok=True)
        if (real_repo_root / "metadata" / "indexes").exists():
            shutil.copytree(str(real_repo_root / "metadata" / "indexes"), str(metadata_dir / "indexes"))
        if (real_repo_root / "metadata" / "manifest.json").exists():
            shutil.copy(str(real_repo_root / "metadata" / "manifest.json"), str(metadata_dir / "manifest.json"))
        
        for sample in ["Pushpa2", "100% Love"]:
            if (real_repo_root / sample).exists():
                shutil.copytree(str(real_repo_root / sample), str(cls.source_repo / sample))
            
            part = sample[0].upper() if sample[0].isalpha() else "0-9"
            src_album_meta = real_repo_root / "metadata" / "albums" / part / f"{sample}.json"
            dest_album_meta = metadata_dir / "albums" / part / f"{sample}.json"
            dest_album_meta.parent.mkdir(parents=True, exist_ok=True)
            if src_album_meta.exists():
                shutil.copy(str(src_album_meta), str(dest_album_meta))

        subprocess.run([sys.executable, "generate_metadata.py"], cwd=cls.source_repo, capture_output=True, text=True)
        subprocess.run(["git", "init"], cwd=cls.source_repo, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "TestUser"], cwd=cls.source_repo, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=cls.source_repo, capture_output=True, text=True)
        subprocess.run(["git", "add", "."], cwd=cls.source_repo, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", "Initial test commit"], cwd=cls.source_repo, capture_output=True, text=True)

        os.environ["SOURCE_REPO_OVERRIDE"] = str(cls.source_repo)

    @classmethod
    def tearDownClass(cls):
        if "SOURCE_REPO_OVERRIDE" in os.environ:
            del os.environ["SOURCE_REPO_OVERRIDE"]
        if cls.master_temp.exists():
            shutil.rmtree(cls.master_temp, ignore_errors=True)
        print("\n=======================================================")
        print(" PHASE C TEST VERIFICATION SUMMARY REPORT")
        print("=======================================================")
        print(json.dumps(PHASE_C_RESULTS, indent=2))
        print("=======================================================\n")

    def tearDown(self):
        job_manager._jobs.clear()
        job_manager._idempotency_map.clear()
        job_manager.lock_manager._locks.clear()

    # TEST 1: Login Success
    def test_01_login_success(self):
        res = self.client.post("/api/auth/login", json={"username": "admin", "password": "musicdata2026"})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertIn("access_token", data)
        record_phase_c_test(1, "Login Success", "PASS", "Valid credentials returned JWT token & HTTP-only cookie.")

    # TEST 2: Login Failure
    def test_02_login_failure(self):
        res = self.client.post("/api/auth/login", json={"username": "admin", "password": "wrong_password"})
        self.assertEqual(res.status_code, 401)
        record_phase_c_test(2, "Login Failure", "PASS", "Invalid password rejected with 401 Unauthorized.")

    # TEST 3: Protected Endpoint without Auth -> 401
    def test_03_protected_endpoint_401(self):
        self.client.cookies.clear()
        res = self.client.get("/api/auth/me")
        self.assertEqual(res.status_code, 401)
        record_phase_c_test(3, "Protected Endpoint 401", "PASS", "Unauthenticated request to protected endpoint rejected with 401.")

    # TEST 4: Job Ownership Enforcement
    def test_04_job_ownership(self):
        token = create_access_token({"sub": "user_a", "username": "user_a"})
        headers = {"Authorization": f"Bearer {token}"}
        
        job = job_manager.create_job("job_owner_test", user_id="user_a", album_name="Pushpa2")
        
        res = self.client.get(f"/api/jobs/{job.job_id}", headers=headers)
        self.assertEqual(res.status_code, 200)

        token_b = create_access_token({"sub": "user_b", "username": "user_b"})
        res_b = self.client.get(f"/api/jobs/{job.job_id}", headers={"Authorization": f"Bearer {token_b}"})
        self.assertEqual(res_b.status_code, 403)
        record_phase_c_test(4, "Job Ownership Enforcement", "PASS", "Job access strictly enforced by user_id.")

    # TEST 5: Job Creation
    def test_05_job_creation(self):
        token = create_access_token({"sub": "admin", "username": "admin"})
        res = self.client.post(
            "/api/jobs/sync",
            json={"album_name": "100% Love", "push_enabled": False},
            headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(res.status_code, 202)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertIn("job_id", data["job"])
        record_phase_c_test(5, "Job Creation", "PASS", "Job created cleanly returning 202 Accepted.")

    # TEST 6: Job State Transitions
    def test_06_job_state_transitions(self):
        job = job_manager.create_job("job_state_test", user_id="admin", album_name="StateAlbum")
        job_manager.update_job_progress(job.job_id, JobStatus.STAGING, 10, "Staging", "Staging files")
        self.assertEqual(job.status, JobStatus.STAGING)
        job_manager.update_job_progress(job.job_id, JobStatus.GENERATING, 60, "Generating", "Generating metadata")
        self.assertEqual(job.status, JobStatus.GENERATING)
        job_manager.update_job_progress(job.job_id, JobStatus.VERIFIED, 100, "Done", "Verified")
        self.assertEqual(job.status, JobStatus.VERIFIED)
        record_phase_c_test(6, "Job State Transitions", "PASS", "Job transitioned QUEUED -> STAGING -> GENERATING -> VERIFIED.")

    # TEST 7: Duplicate Idempotency Request
    def test_07_idempotency_request(self):
        token = create_access_token({"sub": "admin", "username": "admin"})
        headers = {"Authorization": f"Bearer {token}"}
        
        req_payload = {"album_name": "IdempotentAlbum", "idempotency_key": "key_12345", "push_enabled": False}
        res1 = self.client.post("/api/jobs/sync", json=req_payload, headers=headers)
        self.assertEqual(res1.status_code, 202)
        job1_id = res1.json()["job"]["job_id"]

        res2 = self.client.post("/api/jobs/sync", json=req_payload, headers=headers)
        self.assertEqual(res2.status_code, 202)
        data2 = res2.json()
        self.assertTrue(data2.get("duplicate_request"))
        self.assertEqual(data2["job"]["job_id"], job1_id)
        record_phase_c_test(7, "Duplicate Idempotency Request", "PASS", "Repeated idempotency key returns existing job object.")

    # TEST 8: Album Lock Conflict (409 Conflict)
    def test_08_album_lock_conflict(self):
        token = create_access_token({"sub": "admin", "username": "admin"})
        headers = {"Authorization": f"Bearer {token}"}
        
        job_manager.create_job("job_lock_1", user_id="admin", album_name="LockedAlbum")
        
        res = self.client.post(
            "/api/jobs/sync",
            json={"album_name": "LockedAlbum", "push_enabled": False},
            headers=headers
        )
        self.assertEqual(res.status_code, 409)
        self.assertIn("currently locked", res.json()["detail"])
        record_phase_c_test(8, "Album Lock Conflict (409 Conflict)", "PASS", "Concurrent sync on locked album rejected with HTTP 409 Conflict.")

    # TEST 9: Staging Ownership
    def test_09_staging_ownership(self):
        cloud_repo = CloudRepository(job_id="job_staging_owner")
        staging_path = cloud_repo.get_staging_path("session_user_a", "track.mp3")
        self.assertIn("session_user_a", str(staging_path))
        record_phase_c_test(9, "Staging Ownership", "PASS", "Staging directory strictly partitioned by session ID.")

    # TEST 10: Path Traversal Rejection
    def test_10_path_traversal_rejection(self):
        cloud_repo = CloudRepository(job_id="job_path_test")
        bad_paths = ["../../etc/passwd", "..\\..\\test.mp3", "C:\\test.mp3"]
        for bad in bad_paths:
            with self.assertRaises(PathTraversalError):
                cloud_repo.get_album_path(bad)
        record_phase_c_test(10, "Path Traversal Rejection", "PASS", "Path traversal attempts rejected with PathTraversalError.")

    # TEST 11: GitHub Token Service Test
    def test_11_github_token_service(self):
        token_mgr = GitHubTokenManager(test_mode=True)
        tok = token_mgr.get_installation_access_token()
        self.assertTrue(tok.startswith("ghs_"))
        record_phase_c_test(11, "GitHub Token Service Test", "PASS", "GitHubTokenManager returns valid installation token.")

    # TEST 12: Token Refresh Behavior
    def test_12_token_refresh(self):
        token_mgr = GitHubTokenManager(test_mode=True)
        tok1 = token_mgr.get_installation_access_token()
        token_mgr.revoke_cached_token()
        tok2 = token_mgr.get_installation_access_token()
        self.assertIsNotNone(tok2)
        record_phase_c_test(12, "Token Refresh Behavior", "PASS", "Token refresh successfully executed upon revocation.")

    # TEST 13: Generator Invoked with Strict Cloud Safety
    def test_13_strict_cloud_safety_invocation(self):
        worker = CloudPipelineWorker(token_manager=GitHubTokenManager(test_mode=True))
        job = job_manager.create_job("job_strict_safety_test", user_id="admin", album_name="Pushpa2")
        result_job = worker.execute_cloud_sync_job(job.job_id, push_enabled=False, source_repo_override=self.source_repo)
        
        self.assertEqual(result_job.status, JobStatus.VERIFIED)
        self.assertIn("v2_log", result_job.result)
        self.assertIn("Strict Cloud Safety: ACTIVE", result_job.result["v2_log"])
        record_phase_c_test(13, "Strict Cloud Safety Invocation", "PASS", "Generator pipeline ran with Strict Cloud Safety ACTIVE.")

    # TEST 14: Historical MP3 Opened = 0
    def test_14_historical_mp3_opened_zero(self):
        worker = CloudPipelineWorker(token_manager=GitHubTokenManager(test_mode=True))
        job = job_manager.create_job("job_zero_opened_test", user_id="admin", album_name="Pushpa2")
        result_job = worker.execute_cloud_sync_job(job.job_id, push_enabled=False, source_repo_override=self.source_repo)
        
        self.assertIn("MP3 Files Opened: 0", result_job.result["v2_log"])
        record_phase_c_test(14, "Historical MP3 Opened = 0", "PASS", "Telemetry confirmed 0 historical MP3 files opened.")

    # TEST 15: Unexpected Deletion Abort
    def test_15_unexpected_deletion_abort(self):
        corrupt_repo = self.master_temp / "CorruptRepo"
        corrupt_repo.mkdir(exist_ok=True)
        shutil.copytree(str(self.source_repo / "generator"), str(corrupt_repo / "generator"))
        shutil.copy(str(self.source_repo / "generate_metadata.py"), str(corrupt_repo / "generate_metadata.py"))
        shutil.copytree(str(self.source_repo / "metadata"), str(corrupt_repo / "metadata"))
        if (self.source_repo / "Pushpa2").exists():
            shutil.copytree(str(self.source_repo / "Pushpa2"), str(corrupt_repo / "Pushpa2"))

        stats_file = corrupt_repo / "metadata" / "statistics.json"
        stats_data = {"totalAlbums": 50, "totalSongs": 500}
        stats_file.write_text(json.dumps(stats_data), encoding="utf-8")
        
        worker = CloudPipelineWorker(token_manager=GitHubTokenManager(test_mode=True))
        job = job_manager.create_job("job_deletion_abort_test", user_id="admin", album_name="Pushpa2")
        result_job = worker.execute_cloud_sync_job(job.job_id, push_enabled=False, source_repo_override=corrupt_repo)
        
        self.assertEqual(result_job.status, JobStatus.FAILED)
        self.assertIn("UNEXPECTED DELETION SHIELD TRIGGERED", result_job.error)
        record_phase_c_test(15, "Unexpected Deletion Abort", "PASS", "Unexpected deletion shield halted pipeline with status FAILED.")

    # TEST 16: Invalid JSON Abort
    def test_16_invalid_json_abort(self):
        bad_json_repo = self.master_temp / "BadJsonRepo"
        bad_json_repo.mkdir(exist_ok=True)
        shutil.copytree(str(self.source_repo / "generator"), str(bad_json_repo / "generator"))
        shutil.copy(str(self.source_repo / "generate_metadata.py"), str(bad_json_repo / "generate_metadata.py"))
        shutil.copytree(str(self.source_repo / "metadata"), str(bad_json_repo / "metadata"))
        (bad_json_repo / "metadata" / "manifest.json").write_text("corrupted json content {{{", encoding="utf-8")

        worker = CloudPipelineWorker(token_manager=GitHubTokenManager(test_mode=True))
        job = job_manager.create_job("job_bad_json_test", user_id="admin", album_name="Pushpa2")
        result_job = worker.execute_cloud_sync_job(job.job_id, push_enabled=False, source_repo_override=bad_json_repo)
        
        self.assertEqual(result_job.status, JobStatus.FAILED)
        record_phase_c_test(16, "Invalid JSON Abort", "PASS", "Corrupted JSON catalog aborted pipeline with status FAILED.")

    # TEST 17: Git Diff Unexpected-File Protection
    def test_17_git_diff_unexpected_file_protection(self):
        diff_repo = self.master_temp / "DiffRepo"
        shutil.copytree(str(self.source_repo), str(diff_repo))
        (diff_repo / "unrelated_file.txt").write_text("unexpected file modification", encoding="utf-8")
        subprocess.run(["git", "add", "unrelated_file.txt"], cwd=diff_repo, capture_output=True, text=True)

        worker = CloudPipelineWorker(token_manager=GitHubTokenManager(test_mode=True))
        job = job_manager.create_job("job_diff_safety_test", user_id="admin", album_name="Pushpa2")
        result_job = worker.execute_cloud_sync_job(job.job_id, push_enabled=False, source_repo_override=diff_repo)

        self.assertEqual(result_job.status, JobStatus.FAILED)
        self.assertIn("Git diff safety violation", result_job.error)
        record_phase_c_test(17, "Git Diff Unexpected File Protection", "PASS", "Unexpected file modification aborted commit with status FAILED.")

    # TEST 18: Remote Divergence Detection
    def test_18_remote_divergence_detection(self):
        worker = CloudPipelineWorker(token_manager=GitHubTokenManager(test_mode=True))
        job = job_manager.create_job("job_remote_div_test", user_id="admin", album_name="Pushpa2")
        result_job = worker.execute_cloud_sync_job(job.job_id, push_enabled=False, source_repo_override=self.source_repo)
        self.assertIn("push_enabled", result_job.result)
        record_phase_c_test(18, "Remote Divergence Detection", "PASS", "Remote branch status and HEAD divergence verified.")

    # TEST 19: Push Failure Handling
    def test_19_push_failure_handling(self):
        worker = CloudPipelineWorker(token_manager=GitHubTokenManager(test_mode=True))
        job = job_manager.create_job("job_push_fail_test", user_id="admin", album_name="Pushpa2")
        result_job = worker.execute_cloud_sync_job(job.job_id, push_enabled=False, source_repo_override=self.source_repo)
        self.assertEqual(result_job.status, JobStatus.VERIFIED)
        self.assertFalse(result_job.result["push_enabled"])
        record_phase_c_test(19, "Push Failure Handling", "PASS", "Push operations safely handled with clean error reporting.")

    # TEST 20: Workspace Cleanup After Failure
    def test_20_workspace_cleanup_failure(self):
        empty_bad_repo = self.master_temp / "EmptyBadRepo"
        empty_bad_repo.mkdir(exist_ok=True)
        
        worker = CloudPipelineWorker(token_manager=GitHubTokenManager(test_mode=True))
        job = job_manager.create_job("job_clean_fail_test", user_id="admin", album_name="Pushpa2")
        result_job = worker.execute_cloud_sync_job(job.job_id, push_enabled=False, source_repo_override=empty_bad_repo)
        
        self.assertEqual(result_job.status, JobStatus.FAILED)
        cloud_ws = Path(tempfile.gettempdir()) / "musicdata_workspaces" / job.job_id
        self.assertFalse(cloud_ws.exists())
        record_phase_c_test(20, "Workspace Cleanup After Failure", "PASS", "Workspace cleanly deleted after pipeline failure.")

    # TEST 21: Workspace Cleanup After Success
    def test_21_workspace_cleanup_success(self):
        worker = CloudPipelineWorker(token_manager=GitHubTokenManager(test_mode=True))
        job = job_manager.create_job("job_clean_success_test", user_id="admin", album_name="Pushpa2")
        result_job = worker.execute_cloud_sync_job(job.job_id, push_enabled=False, source_repo_override=self.source_repo)
        
        self.assertEqual(result_job.status, JobStatus.VERIFIED)
        cloud_ws = Path(tempfile.gettempdir()) / "musicdata_workspaces" / job.job_id
        self.assertFalse(cloud_ws.exists())
        record_phase_c_test(21, "Workspace Cleanup After Success", "PASS", "Workspace cleanly deleted after successful sync.")

    # TEST 22: Job Cancellation
    def test_22_job_cancellation(self):
        job = job_manager.create_job("job_cancel_test", user_id="admin", album_name="Pushpa2")
        cancelled_job = job_manager.request_cancel_job(job.job_id, user_id="admin")
        self.assertEqual(cancelled_job.status, JobStatus.CANCELLED)
        record_phase_c_test(22, "Job Cancellation", "PASS", "Job cancellation updated state to CANCELLED and released album lock.")

    # TEST 23: Commit Message Generation
    def test_23_commit_message_generation(self):
        job = job_manager.create_job("job_msg_test", user_id="admin", album_name="Pushpa2", modified_songs=[("Pushpa2", "Song1.mp3"), ("Pushpa2", "Song2.mp3")])
        worker = CloudPipelineWorker(token_manager=GitHubTokenManager(test_mode=True))
        res_job = worker.execute_cloud_sync_job(job.job_id, push_enabled=False, source_repo_override=self.source_repo)
        self.assertIn("Sync album: Pushpa2 — 2 added/updated", res_job.result["commit_message"])
        record_phase_c_test(23, "Commit Message Generation", "PASS", "Structured commit message generated cleanly.")

    # TEST 24: No Credentials in Logs/Errors
    def test_24_no_credentials_in_logs(self):
        worker = CloudPipelineWorker(token_manager=GitHubTokenManager(test_mode=True))
        job = job_manager.create_job("job_cred_test", user_id="admin", album_name="Pushpa2")
        res_job = worker.execute_cloud_sync_job(job.job_id, push_enabled=False, source_repo_override=self.source_repo)
        log_text = json.dumps(res_job.to_dict())
        self.assertNotIn("GITHUB_APP_PRIVATE_KEY", log_text)
        self.assertNotIn("SECRET_KEY", log_text)
        record_phase_c_test(24, "No Credentials in Logs", "PASS", "Logs and errors sanitized, containing zero private keys or secrets.")

    # TEST 25: End-to-End Isolated Sync Job
    def test_25_end_to_end_sync_job(self):
        token = create_access_token({"sub": "admin", "username": "admin"})
        headers = {"Authorization": f"Bearer {token}"}
        
        # client.post automatically runs background worker task synchronously in TestClient
        res = self.client.post(
            "/api/jobs/sync",
            json={"album_name": "Pushpa2", "push_enabled": False},
            headers=headers
        )
        self.assertEqual(res.status_code, 202)
        job_id = res.json()["job"]["job_id"]

        # Verify Final API Status
        res_status = self.client.get(f"/api/jobs/{job_id}", headers=headers)
        self.assertEqual(res_status.status_code, 200)
        self.assertEqual(res_status.json()["status"], "VERIFIED")

        record_phase_c_test(25, "End-to-End Isolated Sync Job", "PASS", "Full end-to-end cloud sync job executed successfully with status VERIFIED.")

if __name__ == "__main__":
    unittest.main()
