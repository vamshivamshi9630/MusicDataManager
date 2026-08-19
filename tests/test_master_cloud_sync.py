import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from PIL import Image

# Add project root to sys.path
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from backend.services.validation import ValidationService, FileValidationError, PNG_MAGIC_BYTES
from backend.services.fuzzy_search import score_album_match, search_albums_fuzzy, check_near_duplicate_album
from backend.core.repository import CloudRepository

class TestMasterCloudSyncPipeline(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["CLOUD_MODE"] = "1"
        from backend.main import app
        from fastapi.testclient import TestClient
        cls.client = TestClient(app)

    def _create_sample_image(self, fmt: str = "JPEG", mode: str = "RGB", size=(100, 100)) -> bytes:
        img = Image.new(mode, size, color="purple")
        buf = io.BytesIO()
        img.save(buf, format=fmt)
        return buf.getvalue()

    def test_01_cloud_health_and_stats(self):
        res_health = self.client.get("/api/health")
        self.assertEqual(res_health.status_code, 200)
        self.assertEqual(res_health.json()["mode"], "CLOUD")

        res_stats = self.client.get("/api/stats")
        self.assertEqual(res_stats.status_code, 200)
        self.assertEqual(res_stats.json()["mode"], "CLOUD")

    def test_02_fuzzy_search_ranking_and_irrelevant_filtering(self):
        albums = [
            {"name": "Pushpa", "musicDirector": "DSP", "year": 2021},
            {"name": "Pushpa 2 The Rule", "musicDirector": "DSP", "year": 2024},
            {"name": "Pushpaka Vimanam", "musicDirector": "Ram Miriyala", "year": 2021},
            {"name": "Josh", "musicDirector": "Sandeep", "year": 2009},
            {"name": "Hushaaru", "musicDirector": "Radhan", "year": 2018},
            {"name": "Ashok", "musicDirector": "Mani Sharma", "year": 2006},
            {"name": "Kushi", "musicDirector": "Hesham", "year": 2023}
        ]

        # Search 'push' should return Pushpa, Pushpa 2, Pushpaka Vimanam and filter out Josh, Ashok, etc.
        results = search_albums_fuzzy("push", albums)
        matched_names = [a["name"] for a in results]

        self.assertIn("Pushpa", matched_names)
        self.assertIn("Pushpa 2 The Rule", matched_names)
        self.assertIn("Pushpaka Vimanam", matched_names)
        self.assertNotIn("Josh", matched_names)
        self.assertNotIn("Hushaaru", matched_names)
        self.assertNotIn("Ashok", matched_names)

    def test_03_exact_and_near_duplicate_blocking(self):
        existing = ["Pushpa", "Pushpa 2", "Lover"]

        # Exact duplicate
        exact_dup = check_near_duplicate_album("Pushpa", existing)
        self.assertTrue(exact_dup["duplicate"])
        self.assertTrue(exact_dup["exact"])

        # Near duplicate typo (Puspa -> Pushpa)
        near_dup = check_near_duplicate_album("Puspa", existing)
        self.assertTrue(near_dup["duplicate"])
        self.assertFalse(near_dup["exact"])
        self.assertEqual(near_dup["matched_album"], "Pushpa")

        # Multi-word distinct phrase (Lovers Day vs Lover -> NOT duplicate)
        phrase_check = check_near_duplicate_album("Lovers Day", existing)
        self.assertFalse(phrase_check["duplicate"])

    def test_04_automatic_image_conversion_to_png(self):
        jpeg_bytes = self._create_sample_image(fmt="JPEG")
        png_out = ValidationService.process_and_convert_to_png(jpeg_bytes, "Lovers-day-jpeg-1.jpg")
        self.assertTrue(png_out.startswith(PNG_MAGIC_BYTES))

    def test_05_invalid_image_and_mp3_rejection(self):
        with self.assertRaises(FileValidationError):
            ValidationService.process_and_convert_to_png(b"not_an_image", "fake.png")

        with self.assertRaises(FileValidationError):
            ValidationService.process_and_convert_to_png(b"", "empty.png")

    def test_06_minimal_sparse_blobless_workspace_provisioning(self):
        job_id = "test_sparse_workspace_job"
        cloud_repo = CloudRepository(job_id=job_id)
        ws_path = cloud_repo.provision_blobless_workspace(album_name="Lovers Day")

        self.assertTrue(ws_path.exists())
        self.assertTrue((ws_path / "metadata").exists())
        self.assertTrue((ws_path / "generate_metadata.py").exists())
        self.assertTrue((ws_path / "generate_or_update_songs_with_details.py").exists())

        # Cleanup workspace
        cloud_repo.cleanup_workspace()

    def test_07_structured_json_error_contract(self):
        # Trigger an invalid song upload to verify structured JSON response
        res = self.client.post(
            "/api/upload/song/Lovers Day",
            files={"file": ("fake_song.mp3", b"invalid_mp3_data", "audio/mp3")}
        )
        self.assertEqual(res.status_code, 400)
        data = res.json()
        self.assertFalse(data["success"])
        self.assertIn("error", data)

    def test_08_cloud_create_or_select_saves_album_info(self):
        res = self.client.post(
            "/api/albums/create-or-select",
            json={
                "album_name": "Test Lovers Day",
                "year": 2019,
                "musicDirector": "Shan Rahman",
                "mode": "add"
            }
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        
        staging_info = Path(tempfile.gettempdir()) / "musicdata_staging" / "Test Lovers Day" / "album_info.json"
        self.assertTrue(staging_info.exists())

if __name__ == "__main__":
    unittest.main()
