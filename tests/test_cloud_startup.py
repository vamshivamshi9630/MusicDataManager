import os
import sys
import unittest
import tempfile
from pathlib import Path

# Add project root to sys.path
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

class TestCloudStartupWithoutLocalRepo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Force CLOUD_MODE=1 and remove local repo overrides
        os.environ["CLOUD_MODE"] = "1"
        if "MUSICDATA_REPOSITORY_ROOT" in os.environ:
            del os.environ["MUSICDATA_REPOSITORY_ROOT"]
        if "SOURCE_REPO_OVERRIDE" in os.environ:
            del os.environ["SOURCE_REPO_OVERRIDE"]

        # Create isolated empty temp dir
        cls.empty_temp_dir = tempfile.mkdtemp(prefix="cloud_startup_test_")
        cls.original_cwd = os.getcwd()
        os.chdir(cls.empty_temp_dir)

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls.original_cwd)
        if "CLOUD_MODE" in os.environ:
            del os.environ["CLOUD_MODE"]

    def test_01_cloud_startup_import_and_health(self):
        """Test FastAPI app imports and responds to /api/health in Cloud Mode without local repo."""
        try:
            from backend.main import app
            from fastapi.testclient import TestClient
        except Exception as e:
            self.fail(f"Failed to import backend.main in Cloud Mode: {e}")

        client = TestClient(app)
        res = client.get("/api/health")
        self.assertEqual(res.status_code, 200)

        data = res.json()
        self.assertEqual(data["status"], "online")
        self.assertEqual(data["mode"], "CLOUD")
        self.assertEqual(data["repository_root"], "Ephemeral Cloud Workspace")
        self.assertTrue("github_repository" in data)
        self.assertTrue("git" in data)

    def test_02_local_endpoint_rejection_in_cloud_mode(self):
        """Test that local agent endpoints gracefully reject calls in Cloud Mode with informative 400."""
        from backend.main import app
        from fastapi.testclient import TestClient

        client = TestClient(app)
        res = client.get("/api/albums")
        self.assertEqual(res.status_code, 400)
        self.assertIn("Local agent repository operations are disabled in Cloud Mode", res.json()["detail"])

if __name__ == "__main__":
    unittest.main()
