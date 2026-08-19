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

    def test_02_cloud_stats_endpoint(self):
        """Test GET /api/stats returns stable schema with mode=CLOUD."""
        from backend.main import app
        from fastapi.testclient import TestClient

        client = TestClient(app)
        res = client.get("/api/stats")
        self.assertEqual(res.status_code, 200)
        data = res.json()

        self.assertEqual(data["mode"], "CLOUD")
        self.assertEqual(data["zero_byte_shield"], "Active")
        self.assertIn("albums", data)
        self.assertIn("songs", data)
        self.assertIn("png_artwork", data)
        self.assertIsInstance(data["albums"], int)
        self.assertIsInstance(data["songs"], int)

    def test_03_cloud_albums_list_endpoint(self):
        """Test GET /api/albums returns dynamic catalog list in Cloud Mode."""
        from backend.main import app
        from fastapi.testclient import TestClient

        client = TestClient(app)
        res = client.get("/api/albums")
        self.assertEqual(res.status_code, 200)
        data = res.json()

        self.assertIn("total_albums", data)
        self.assertIn("albums", data)
        self.assertIsInstance(data["albums"], list)

    def test_04_cloud_directors_autocomplete(self):
        """Test GET /api/directors/autocomplete in Cloud Mode."""
        from backend.main import app
        from fastapi.testclient import TestClient

        client = TestClient(app)
        res = client.get("/api/directors/autocomplete?q=Rahman")
        self.assertEqual(res.status_code, 200)
        data = res.json()

        self.assertIn("suggestions", data)
        self.assertIsInstance(data["suggestions"], list)

if __name__ == "__main__":
    unittest.main()
