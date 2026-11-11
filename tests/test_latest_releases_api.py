import os
import unittest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from backend.main import app
from backend.core.config import settings
from backend.services.latest_releases.db import get_latest_releases_db
from backend.services.latest_releases.models import NotificationType

class TestLatestReleasesAPI(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.db = get_latest_releases_db()

    def test_get_unread_count(self):
        resp = self.client.get("/api/latest-releases/unread-count")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])
        self.assertIn("unread_count", data)

    def test_get_notifications_and_read(self):
        # Insert a test notification directly
        self.db.create_or_update_notification(
            album_id=1,
            notification_type=NotificationType.NEW_ALBUM,
            title="API Test Album",
            message="Test message",
            album_name="API Test Movie",
            artwork_url=None,
            music_director="Director X",
            year=2026,
            source_url="https://naasongs.com.co/api-test.html",
            songs_list=[{"song_title": "Track 1", "exists_in_catalog": False}],
            missing_count=1
        )

        resp = self.client.get("/api/latest-releases/notifications?category=all")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["success"])
        self.assertGreaterEqual(data["count"], 1)

        first_notif = data["notifications"][0]
        notif_id = first_notif["id"]

        # Mark read
        read_resp = self.client.post(f"/api/latest-releases/notifications/{notif_id}/read")
        self.assertEqual(read_resp.status_code, 200)
        self.assertTrue(read_resp.json()["success"])

        # Mark all read
        mark_all_resp = self.client.post("/api/latest-releases/notifications/mark-all-read")
        self.assertEqual(mark_all_resp.status_code, 200)
        self.assertTrue(mark_all_resp.json()["success"])

    def test_get_status_and_history(self):
        status_resp = self.client.get("/api/latest-releases/status")
        self.assertEqual(status_resp.status_code, 200)
        data = status_resp.json()
        self.assertTrue(data["success"])
        self.assertIn("is_locked", data)

        history_resp = self.client.get("/api/latest-releases/history")
        self.assertEqual(history_resp.status_code, 200)
        self.assertTrue(history_resp.json()["success"])
        self.assertIn("scans", history_resp.json())

    def test_cron_trigger_endpoint_authorization(self):
        # 1. Missing secret should be rejected with 401
        unauth_resp = self.client.post("/api/latest-releases/cron-trigger")
        self.assertEqual(unauth_resp.status_code, 401)

        # 2. Correct secret should pass through to run_scan
        with patch("backend.services.latest_releases.discovery.LatestReleasesDiscoveryEngine.run_scan") as mock_scan:
            mock_scan.return_value = {"success": True, "status": "COMPLETED", "albums_found": 5}
            auth_resp = self.client.post(
                "/api/latest-releases/cron-trigger",
                headers={"X-Cron-Secret": settings.CRON_SECRET}
            )
            self.assertEqual(auth_resp.status_code, 200)
            self.assertTrue(auth_resp.json()["success"])

    def test_manual_check_now_lock_conflict(self):
        # When engine is locked, check-now should return HTTP 409 Conflict
        with patch("backend.services.latest_releases.discovery.LatestReleasesDiscoveryEngine.run_scan") as mock_scan:
            mock_scan.return_value = {
                "success": False,
                "status": "LOCKED",
                "message": "Another scan is already in progress."
            }
            resp = self.client.post("/api/latest-releases/check-now")
            self.assertEqual(resp.status_code, 409)

if __name__ == "__main__":
    unittest.main()
