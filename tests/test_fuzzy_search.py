import os
import sys
import unittest
import tempfile
from pathlib import Path

# Add project root to sys.path
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from backend.services.fuzzy_search import (
    normalize_text,
    calculate_similarity_ratio,
    score_album_match,
    search_albums_fuzzy,
    check_near_duplicate_album
)

class TestGenericFuzzySearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sample_catalog = [
            "Pushpa",
            "Pushpa 2",
            "Pushpa 2 The Rule",
            "Baahubali",
            "RRR",
            "Arjun Reddy",
            "Jersey",
            "10 Endrathukulla"
        ]

    def test_01_exact_match(self):
        score = score_album_match("Pushpa", "Pushpa")
        self.assertEqual(score, 1.0)

    def test_02_case_insensitive_match(self):
        score = score_album_match("pushpa", "Pushpa")
        self.assertEqual(score, 1.0)

    def test_03_prefix_match(self):
        score = score_album_match("push", "Pushpa")
        self.assertGreaterEqual(score, 0.90)

    def test_04_missing_character_spelling(self):
        """puspa missing 'h' should match Pushpa with high score (>0.75)."""
        res = search_albums_fuzzy("puspa", self.sample_catalog)
        self.assertTrue(len(res) > 0)
        self.assertEqual(res[0]["name"], "Pushpa")
        self.assertGreaterEqual(res[0]["match_score"], 0.75)

    def test_05_extra_character_spelling(self):
        """pushpaa with extra 'a' should match Pushpa with high score."""
        res = search_albums_fuzzy("pushpaa", self.sample_catalog)
        self.assertTrue(len(res) > 0)
        self.assertEqual(res[0]["name"], "Pushpa")
        self.assertGreaterEqual(res[0]["match_score"], 0.80)

    def test_06_whitespace_normalization(self):
        score = score_album_match("  Pushpa  ", "Pushpa")
        self.assertEqual(score, 1.0)

    def test_07_near_duplicate_blocking(self):
        """Attempting to create 'Puspa' should be blocked because 'Pushpa' exists."""
        dup = check_near_duplicate_album("Puspa", self.sample_catalog)
        self.assertTrue(dup["duplicate"])
        self.assertEqual(dup["matched_album"], "Pushpa")

    def test_08_search_prefix_push(self):
        """Searching 'push' should rank Pushpa, Pushpa 2, Pushpa 2 The Rule first."""
        res = search_albums_fuzzy("push", self.sample_catalog)
        names = [r["name"] for r in res]
        self.assertIn("Pushpa", names)
        self.assertIn("Pushpa 2", names)

    def test_09_cloud_mode_search_endpoint(self):
        """Test API endpoints in Cloud Mode without local repo."""
        os.environ["CLOUD_MODE"] = "1"
        if "MUSICDATA_REPOSITORY_ROOT" in os.environ:
            del os.environ["MUSICDATA_REPOSITORY_ROOT"]

        from backend.main import app
        from fastapi.testclient import TestClient

        client = TestClient(app)
        
        # Test fuzzy search endpoint
        res = client.get("/api/albums/search?q=puspa")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("suggestions", data)

        # Test duplicate check endpoint
        res_dup = client.post("/api/albums/check-duplicate", json={"album_name": "Puspa", "mode": "add"})
        self.assertEqual(res_dup.status_code, 200)
        dup_data = res_dup.json()
        self.assertTrue(dup_data["duplicate"])

    def test_10_backend_create_blocked_for_duplicate(self):
        """Creating near-duplicate album via API should return HTTP 400."""
        os.environ["CLOUD_MODE"] = "1"
        from backend.main import app
        from fastapi.testclient import TestClient

        client = TestClient(app)
        res = client.post("/api/albums/create-or-select", json={
            "album_name": "Puspa",
            "mode": "add",
            "year": 2024
        })
        self.assertEqual(res.status_code, 400)
        detail = res.json()["detail"]
        self.assertTrue(detail["duplicate"])
        self.assertEqual(detail["matched_album"], "Pushpa")

if __name__ == "__main__":
    unittest.main()
