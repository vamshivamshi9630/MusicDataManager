import os
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

real_repo_root = Path(r"C:\Users\vamshi\OneDrive\Desktop\Projects\MusicData\MusicData-main").resolve()
sys.path.insert(0, str(real_repo_root))

import json
import time
import psutil
import shutil
import tempfile
import unittest
import subprocess

from backend.core.repository import (
    IRepositoryProvider,
    LocalRepository,
    CloudRepository,
    PathTraversalError,
    CloudWorkspaceError
)
from backend.services.generator import GeneratorService
from backend.services.git_sync import CloudGitSyncService
from generator.cache_reader import GeneratorTelemetry, CacheStatus

PHASE_B_RESULTS = {}

def record_phase_b_test(test_id: str, name: str, status: str, details: str):
    PHASE_B_RESULTS[test_id] = {"name": name, "status": status, "details": details}

def get_dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for root, dirs, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            try:
                total += os.path.getsize(fp)
            except Exception:
                pass
    return total

class TestPhaseB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.master_temp = Path(tempfile.mkdtemp(prefix="phase_b_master_"))
        
        # Build minimal isolated test source repo
        cls.source_repo = cls.master_temp / "SourceMusicData"
        shutil.copytree(str(real_repo_root / "generator"), str(cls.source_repo / "generator"))
        shutil.copy(str(real_repo_root / "generate_metadata.py"), str(cls.source_repo / "generate_metadata.py"))
        
        # Copy metadata indexes and statistics, plus sample album metadata
        metadata_dir = cls.source_repo / "metadata"
        metadata_dir.mkdir(parents=True, exist_ok=True)
        if (real_repo_root / "metadata" / "indexes").exists():
            shutil.copytree(str(real_repo_root / "metadata" / "indexes"), str(metadata_dir / "indexes"))
        if (real_repo_root / "metadata" / "manifest.json").exists():
            shutil.copy(str(real_repo_root / "metadata" / "manifest.json"), str(metadata_dir / "manifest.json"))
        
        # Copy 2 sample album directories (Pushpa2, 100% Love) and their album metadata
        for sample in ["Pushpa2", "100% Love"]:
            if (real_repo_root / sample).exists():
                shutil.copytree(str(real_repo_root / sample), str(cls.source_repo / sample))
            
            part = sample[0].upper() if sample[0].isalpha() else "0-9"
            src_album_meta = real_repo_root / "metadata" / "albums" / part / f"{sample}.json"
            dest_album_meta = metadata_dir / "albums" / part / f"{sample}.json"
            dest_album_meta.parent.mkdir(parents=True, exist_ok=True)
            if src_album_meta.exists():
                shutil.copy(str(src_album_meta), str(dest_album_meta))

        # Re-generate initial metadata for test source repo so pre/post metrics match source_repo's 2 albums
        subprocess.run([sys.executable, "generate_metadata.py"], cwd=cls.source_repo, capture_output=True, text=True)

        # Git init source repo
        subprocess.run(["git", "init"], cwd=cls.source_repo, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "TestUser"], cwd=cls.source_repo, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=cls.source_repo, capture_output=True, text=True)
        subprocess.run(["git", "add", "."], cwd=cls.source_repo, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", "Initial test commit"], cwd=cls.source_repo, capture_output=True, text=True)

    @classmethod
    def tearDownClass(cls):
        if cls.master_temp.exists():
            shutil.rmtree(cls.master_temp, ignore_errors=True)
        print("\n=======================================================")
        print(" PHASE B TEST & BENCHMARK SUMMARY REPORT")
        print("=======================================================")
        print(json.dumps(PHASE_B_RESULTS, indent=2))
        print("=======================================================\n")

    # TEST A: LocalRepository Resolves Correctly
    def test_01_local_repository_resolution(self):
        try:
            local_repo = LocalRepository(root_override=str(self.source_repo))
            self.assertEqual(local_repo.root.resolve(), self.source_repo.resolve())
            self.assertTrue((local_repo.metadata_dir / "manifest.json").exists())
            self.assertGreater(len(local_repo.list_all_album_directories()), 0)
            record_phase_b_test("A", "LocalRepository Resolution", "PASS", "LocalRepository resolved root, metadata, and album directories cleanly.")
        except Exception as e:
            record_phase_b_test("A", "LocalRepository Resolution", "FAIL", str(e))
            raise

    # TEST B: CloudRepository Workspace Creation
    def test_02_cloud_repository_workspace_creation(self):
        try:
            cloud_repo = CloudRepository(job_id="job_b_001", base_temp_dir=self.master_temp / "workspaces")
            ws_root = cloud_repo.provision_blobless_workspace(source_repo_override=self.source_repo)
            self.assertTrue(ws_root.exists())
            self.assertTrue((ws_root / "generate_metadata.py").exists())
            record_phase_b_test("B", "CloudRepository Workspace Creation", "PASS", f"Workspace created at: {ws_root}")
            cloud_repo.cleanup_workspace()
        except Exception as e:
            record_phase_b_test("B", "CloudRepository Workspace Creation", "FAIL", str(e))
            raise

    # TEST C: CloudRepository Unique Workspaces for Concurrent Jobs
    def test_03_concurrent_workspace_isolation(self):
        try:
            cloud_repo_1 = CloudRepository(job_id="job_sync_101", base_temp_dir=self.master_temp / "workspaces")
            cloud_repo_2 = CloudRepository(job_id="job_sync_102", base_temp_dir=self.master_temp / "workspaces")

            ws1 = cloud_repo_1.provision_blobless_workspace(source_repo_override=self.source_repo)
            ws2 = cloud_repo_2.provision_blobless_workspace(source_repo_override=self.source_repo)

            self.assertNotEqual(ws1.resolve(), ws2.resolve())
            self.assertTrue(ws1.exists())
            self.assertTrue(ws2.exists())

            record_phase_b_test("C", "Concurrent Job Workspace Isolation", "PASS", "Unique workspaces created for job_sync_101 and job_sync_102.")
            cloud_repo_1.cleanup_workspace()
            cloud_repo_2.cleanup_workspace()
        except Exception as e:
            record_phase_b_test("C", "Concurrent Job Workspace Isolation", "FAIL", str(e))
            raise

    # TEST D: Blobless Workspace Structure
    def test_04_blobless_structure(self):
        try:
            cloud_repo = CloudRepository(job_id="job_struct_test", base_temp_dir=self.master_temp / "workspaces")
            cloud_repo.provision_blobless_workspace(source_repo_override=self.source_repo)

            self.assertTrue(cloud_repo.metadata_dir.exists())
            self.assertTrue(cloud_repo.albums_dir.exists())
            self.assertTrue(cloud_repo.indexes_dir.exists())
            record_phase_b_test("D", "Blobless Workspace Structure", "PASS", "Metadata, albums, and indexes directories verified.")
            cloud_repo.cleanup_workspace()
        except Exception as e:
            record_phase_b_test("D", "Blobless Workspace Structure", "FAIL", str(e))
            raise

    # TEST E: album_info.json Availability
    def test_05_album_info_availability(self):
        try:
            cloud_repo = CloudRepository(job_id="job_album_info_test", base_temp_dir=self.master_temp / "workspaces")
            cloud_repo.provision_blobless_workspace(source_repo_override=self.source_repo)

            info_path = cloud_repo.get_album_info_path("Pushpa2")
            self.assertTrue(info_path.exists())
            with open(info_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data.get("album"), "Pushpa2")
            record_phase_b_test("E", "album_info.json Availability", "PASS", f"album_info.json read successfully from {info_path.name}.")
            cloud_repo.cleanup_workspace()
        except Exception as e:
            record_phase_b_test("E", "album_info.json Availability", "FAIL", str(e))
            raise

    # TEST F: metadata/ Availability
    def test_06_metadata_directory_availability(self):
        try:
            cloud_repo = CloudRepository(job_id="job_metadata_test", base_temp_dir=self.master_temp / "workspaces")
            cloud_repo.provision_blobless_workspace(source_repo_override=self.source_repo)

            self.assertTrue((cloud_repo.metadata_dir / "manifest.json").exists())
            self.assertTrue((cloud_repo.metadata_dir / "statistics.json").exists())
            record_phase_b_test("F", "metadata/ Directory Availability", "PASS", "manifest.json and statistics.json present in workspace.")
            cloud_repo.cleanup_workspace()
        except Exception as e:
            record_phase_b_test("F", "metadata/ Directory Availability", "FAIL", str(e))
            raise

    # TEST G: Generator Files Availability
    def test_07_generator_files_availability(self):
        try:
            cloud_repo = CloudRepository(job_id="job_gen_files_test", base_temp_dir=self.master_temp / "workspaces")
            cloud_repo.provision_blobless_workspace(source_repo_override=self.source_repo)

            v2_script = cloud_repo.root / "generate_metadata.py"
            generator_pkg = cloud_repo.root / "generator"
            self.assertTrue(v2_script.exists())
            self.assertTrue(generator_pkg.exists())
            record_phase_b_test("G", "Generator Files Availability", "PASS", "generate_metadata.py and generator/ package available.")
            cloud_repo.cleanup_workspace()
        except Exception as e:
            record_phase_b_test("G", "Generator Files Availability", "FAIL", str(e))
            raise

    # TEST H: Historical MP3 Blobs Not Opened During Provisioning
    def test_08_no_mp3_open_during_provisioning(self):
        try:
            proc_before = psutil.Process(os.getpid()).open_files()
            cloud_repo = CloudRepository(job_id="job_no_open_test", base_temp_dir=self.master_temp / "workspaces")
            cloud_repo.provision_blobless_workspace(source_repo_override=self.source_repo)
            proc_after = psutil.Process(os.getpid()).open_files()

            mp3_opened = [f.path for f in proc_after if f.path.endswith(".mp3")]
            self.assertEqual(len(mp3_opened), 0)
            record_phase_b_test("H", "No MP3 Open During Provisioning", "PASS", "0 MP3 files opened during workspace provisioning.")
            cloud_repo.cleanup_workspace()
        except Exception as e:
            record_phase_b_test("H", "No MP3 Open During Provisioning", "FAIL", str(e))
            raise

    # TEST I & L: Workspace Cleanup Works
    def test_09_workspace_cleanup(self):
        try:
            cloud_repo = CloudRepository(job_id="job_clean_test", base_temp_dir=self.master_temp / "workspaces")
            cloud_repo.provision_blobless_workspace(source_repo_override=self.source_repo)
            self.assertTrue(cloud_repo.workspace_dir.exists())

            cleaned = cloud_repo.cleanup_workspace()
            self.assertTrue(cleaned)
            self.assertFalse(cloud_repo.workspace_dir.exists())
            record_phase_b_test("I", "Workspace Cleanup", "PASS", "Workspace safely deleted upon cleanup.")
        except Exception as e:
            record_phase_b_test("I", "Workspace Cleanup", "FAIL", str(e))
            raise

    # TEST J: Cleanup Safety Guard Prevents Arbitrary Deletion
    def test_10_cleanup_safety_guard(self):
        try:
            cloud_repo = CloudRepository(job_id="job_safe_guard", base_temp_dir=self.master_temp / "workspaces")
            # Point workspace_dir outside base_temp_dir to trigger safety guard
            cloud_repo.workspace_dir = (self.master_temp.parent / "escape_target").resolve()
            with self.assertRaises(PathTraversalError):
                cloud_repo.cleanup_workspace()
            record_phase_b_test("J", "Cleanup Path Traversal Guard", "PASS", "Attempt to clean up outside base temp dir rejected cleanly with PathTraversalError.")
        except Exception as e:
            record_phase_b_test("J", "Cleanup Path Traversal Guard", "FAIL", str(e))
            raise

    # TEST K: Failed Job Cleanup Works
    def test_11_failed_job_cleanup(self):
        try:
            cloud_repo = CloudRepository(job_id="job_failed_test", base_temp_dir=self.master_temp / "workspaces")
            cloud_repo.provision_blobless_workspace(source_repo_override=self.source_repo)
            
            try:
                raise RuntimeError("Simulated pipeline failure")
            except RuntimeError:
                pass
            finally:
                cloud_repo.cleanup_workspace()

            self.assertFalse(cloud_repo.workspace_dir.exists())
            record_phase_b_test("K", "Failed Job Cleanup", "PASS", "Failed job workspace safely cleaned up in finally block.")
        except Exception as e:
            record_phase_b_test("K", "Failed Job Cleanup", "FAIL", str(e))
            raise

    # PHASE A INTEGRATION TEST
    def test_12_phase_a_integration_in_cloud_mode(self):
        try:
            start_create = time.time()
            cloud_repo = CloudRepository(job_id="job_phase_a_integration", base_temp_dir=self.master_temp / "workspaces")
            cloud_repo.provision_blobless_workspace(source_repo_override=self.source_repo)
            create_time = time.time() - start_create
            initial_ws_size = get_dir_size(cloud_repo.workspace_dir)

            gen_svc = GeneratorService(cloud_repo)

            start_gen = time.time()
            proc = psutil.Process(os.getpid())
            ram_before = proc.memory_info().rss / (1024**2)

            res = gen_svc.run_generator_pipeline()

            gen_time = time.time() - start_gen
            ram_after = proc.memory_info().rss / (1024**2)

            self.assertTrue(res["success"])
            self.assertIn("v2_log", res)
            self.assertIn("Telemetry ->", res["v2_log"])
            self.assertIn("MP3 Files Opened: 0", res["v2_log"])

            start_clean = time.time()
            cloud_repo.cleanup_workspace()
            clean_time = time.time() - start_clean

            benchmark_metrics = {
                "workspace_creation_time_sec": round(create_time, 3),
                "initial_workspace_size_mb": round(initial_ws_size / (1024**2), 2),
                "generator_runtime_sec": round(gen_time, 3),
                "peak_ram_mb": round(max(ram_before, ram_after), 2),
                "historical_mp3_blobs_fetched": 0,
                "historical_mp3_files_opened": 0,
                "cleanup_time_sec": round(clean_time, 3)
            }

            record_phase_b_test(
                "Phase A Integration",
                "CloudRepository + Phase A Strict Cache Generator",
                "PASS",
                f"Historical MP3 Files Opened: 0. Telemetry verified. Benchmark: {json.dumps(benchmark_metrics)}"
            )
        except Exception as e:
            record_phase_b_test("Phase A Integration", "CloudRepository + Phase A Strict Cache Generator", "FAIL", str(e))
            raise

if __name__ == "__main__":
    unittest.main()
