import unittest
from fastapi.testclient import TestClient
from backend.main import app
from backend.core.repository import RepositoryContext
from backend.services.validation import ValidationService, FileValidationError
from backend.services.metadata import MetadataService
from backend.services.duplicate import DuplicateDetectionService

class TestMusicDataManager(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.repo = RepositoryContext()
        cls.metadata_svc = MetadataService(cls.repo)
        cls.val_svc = ValidationService()
        cls.dup_svc = DuplicateDetectionService(cls.repo)

    def test_01_health_endpoint(self):
        res = self.client.get("/api/health")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "online")
        self.assertIn("branch", data["git"])

    def test_02_albums_list_endpoint(self):
        res = self.client.get("/api/albums")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertGreaterEqual(data["total_albums"], 658)

    def test_03_music_director_autocomplete(self):
        res = self.client.get("/api/directors/autocomplete?q=devi")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("Devi Sri Prasad", data["suggestions"])

    def test_04_png_magic_byte_validation(self):
        valid_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
        invalid_jpg = b"\xff\xd8\xff\xe0" + b"\x00" * 20
        
        self.assertTrue(self.val_svc.validate_png_bytes(valid_png, "test.png"))
        
        with self.assertRaises(FileValidationError):
            self.val_svc.validate_png_bytes(invalid_jpg, "test.jpg")

    def test_05_duplicate_detection(self):
        analysis = self.dup_svc.analyze_uploaded_song(
            album_name="Pushpa2",
            song_filename="Pushpa Pushpa Song.mp3",
            file_size=10811498,
            duration_seconds=259
        )
        self.assertEqual(analysis["status"], "EXACT_DUPLICATE")

if __name__ == "__main__":
    unittest.main()
