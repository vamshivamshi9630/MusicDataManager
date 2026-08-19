import io
import os
import sys
import unittest
from pathlib import Path
from PIL import Image

# Ensure project root is in sys.path
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from backend.services.validation import PNG_MAGIC_BYTES

class TestCloudRealEndToEndPipeline(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Force CLOUD_MODE=1 for Cloud execution test
        os.environ["CLOUD_MODE"] = "1"
        from backend.main import app
        from fastapi.testclient import TestClient
        cls.client = TestClient(app)
        
        cls.source_dir = Path(r"C:\Users\vamshi\Downloads\New folder")
        cls.album_name = "Lovers Day"
        cls.music_director = "Unknown"
        cls.year = 2019
        cls.release_date = "2019-02-14"

        print(f"\n[CLOUD REAL E2E TEST] Executing Cloud Sync Pipeline for '{cls.album_name}'...")

    def test_01_cloud_create_or_select_album(self):
        res = self.client.post(
            "/api/albums/create-or-select",
            json={
                "album_name": self.album_name,
                "musicDirector": self.music_director,
                "year": self.year,
                "releaseDate": self.release_date,
                "mode": "add"
            }
        )
        self.assertEqual(res.status_code, 200, f"create-or-select failed: {res.text}")
        data = res.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["album_name"], self.album_name)

    def test_02_cloud_upload_artwork(self):
        art_path = next(iter(self.source_dir.glob("*.png")))
        raw_bytes = art_path.read_bytes()

        res = self.client.post(
            f"/api/upload/artwork/{self.album_name}",
            files={"file": (art_path.name, raw_bytes, "image/png")}
        )
        self.assertEqual(res.status_code, 200, f"artwork upload failed: {res.text}")
        data = res.json()
        self.assertTrue(data["success"])

    def test_03_cloud_upload_mp3s(self):
        mp3_files = sorted(list(self.source_dir.glob("*.mp3")))
        self.assertEqual(len(mp3_files), 8, f"Expected 8 MP3 files in {self.source_dir}")

        for mp3 in mp3_files:
            raw_mp3 = mp3.read_bytes()
            res = self.client.post(
                f"/api/upload/song/{self.album_name}",
                files={"file": (mp3.name, raw_mp3, "audio/mp3")}
            )
            self.assertEqual(res.status_code, 200, f"MP3 upload failed for {mp3.name}: {res.text}")
            data = res.json()
            self.assertTrue(data["success"])

    def test_04_cloud_sync_execute_and_push_to_github(self):
        res = self.client.post(
            f"/api/sync/execute/{self.album_name}",
            json={
                "album_name": self.album_name,
                "mode": "add"
            }
        )
        self.assertEqual(res.status_code, 200, f"Cloud sync execution failed: {res.text}")
        data = res.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["status"], "COMPLETED")
        self.assertTrue(data["git_summary"]["pushed"], "Git changes must be pushed to GitHub main branch.")

if __name__ == "__main__":
    unittest.main()
