import os
import sys
import unittest
from pathlib import Path

# Ensure project root is in sys.path
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

class TestCloudIsolatedNoLocalRepo(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # 1. Enable Cloud Mode
        os.environ["CLOUD_MODE"] = "1"
        # 2. Explicitly UNSET MUSICDATA_REPOSITORY_ROOT to prove Cloud Mode needs NO local repo
        if "MUSICDATA_REPOSITORY_ROOT" in os.environ:
            del os.environ["MUSICDATA_REPOSITORY_ROOT"]

        from backend.main import app
        from fastapi.testclient import TestClient
        cls.client = TestClient(app)

    def test_01_health_endpoint_isolated(self):
        res = self.client.get("/api/health")
        self.assertEqual(res.status_code, 200, f"Health check failed: {res.text}")
        data = res.json()
        self.assertEqual(data["mode"], "CLOUD")
        self.assertEqual(data["repository_root"], "Ephemeral Cloud Workspace")

    def test_02_stats_endpoint_isolated(self):
        res = self.client.get("/api/stats")
        self.assertEqual(res.status_code, 200, f"Stats check failed: {res.text}")
        data = res.json()
        self.assertGreaterEqual(data["albums"], 0)
        self.assertGreaterEqual(data["songs"], 0)

    def test_03_sync_diagnostics_endpoint_isolated(self):
        res = self.client.get("/api/sync/diagnostics")
        self.assertEqual(res.status_code, 200, f"Sync diagnostics failed: {res.text}")
        data = res.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["mode"], "CLOUD")
        self.assertEqual(data["repository"], "vamshivamshi9630/MusicData")
        self.assertTrue(data["token_present"])
        self.assertTrue(data["push_possible"])
        self.assertTrue(data["clone_possible"])
        self.assertTrue(data["generators_available"])

if __name__ == "__main__":
    unittest.main()
