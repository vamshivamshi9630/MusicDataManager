import os
import time
import tempfile
import unittest
from pathlib import Path
from backend.services.latest_releases.db import LatestReleasesDB
from backend.services.latest_releases.models import (
    NotificationType,
    TrackingStatus,
    MatchConfidence,
    DiscoveredAlbum,
    DiscoveredSong
)

class TestLatestReleasesDB(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_latest_releases.db"
        # Force SQLite mode for isolated local unit tests
        os.environ["LATEST_RELEASES_DB_PATH"] = str(self.db_path)
        self.db = LatestReleasesDB(database_url="", sqlite_path=str(self.db_path))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_schema_initialization(self):
        self.assertTrue(self.db_path.exists())
        conn = self.db._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [r[0] for r in cur.fetchall()]
        conn.close()

        self.assertIn("latest_releases_lock", tables)
        self.assertIn("discovery_scans", tables)
        self.assertIn("discovered_albums", tables)
        self.assertIn("discovered_songs", tables)
        self.assertIn("notifications", tables)

    def test_distributed_locking(self):
        # 1. First acquire succeeds
        acquired = self.db.acquire_scan_lock(locked_by="test_worker_1", timeout_minutes=5)
        self.assertTrue(acquired)

        status = self.db.get_lock_status()
        self.assertTrue(status["is_locked"])
        self.assertEqual(status["locked_by"], "test_worker_1")

        # 2. Second concurrent acquire fails
        acquired2 = self.db.acquire_scan_lock(locked_by="test_worker_2", timeout_minutes=5)
        self.assertFalse(acquired2)

        # 3. Release lock
        self.db.release_scan_lock(locked_by="test_worker_1")
        status_after = self.db.get_lock_status()
        self.assertFalse(status_after["is_locked"])

        # 4. Now second worker can acquire
        acquired3 = self.db.acquire_scan_lock(locked_by="test_worker_2", timeout_minutes=5)
        self.assertTrue(acquired3)
        self.db.release_scan_lock()

    def test_scan_telemetry(self):
        scan_id = self.db.record_scan_start(locked_by="test_cron")
        self.assertIsInstance(scan_id, int)

        self.db.record_scan_complete(
            scan_id=scan_id,
            albums_found=10,
            new_albums=2,
            new_songs=3,
            error_message=None
        )

        scans = self.db.get_recent_scans(limit=5)
        self.assertEqual(len(scans), 1)
        self.assertEqual(scans[0]["status"], "COMPLETED")
        self.assertEqual(scans[0]["albums_found"], 10)
        self.assertEqual(scans[0]["new_albums_count"], 2)
        self.assertEqual(scans[0]["new_songs_count"], 3)

    def test_upsert_album_and_songs(self):
        album = DiscoveredAlbum(
            album_name="Tyson Naidu",
            normalized_name="tyson naidu",
            source_url="https://naasongs.com.co/tyson-naidu.html",
            music_director="Bheems",
            year=2026
        )
        album_id = self.db.upsert_discovered_album(album)
        self.assertIsInstance(album_id, int)

        song = DiscoveredSong(
            song_title="Madarasi Rabbaru Bomma",
            normalized_title="madarasi rabbaru bomma",
            track_number=1,
            source_audio_url="https://mp3teluguwap.net/song.mp3",
            exists_in_catalog=False
        )
        song_id = self.db.upsert_discovered_song(album_id, song)
        self.assertIsInstance(song_id, int)

        # Re-upserting same song updates instead of inserting duplicate
        song.track_number = 2
        song_id_2 = self.db.upsert_discovered_song(album_id, song)
        self.assertEqual(song_id, song_id_2)

    def test_notification_creation_and_deduplication(self):
        songs_list = [
            {"song_title": "Song A", "exists_in_catalog": False},
            {"song_title": "Song B", "exists_in_catalog": True}
        ]

        # Scan 1: creates notification
        created1 = self.db.create_or_update_notification(
            album_id=1,
            notification_type=NotificationType.NEW_SONG,
            title="New Song: Example",
            message="1 song released",
            album_name="Example Movie",
            artwork_url="https://example.com/art.jpg",
            music_director="Director A",
            year=2026,
            source_url="https://naasongs.com.co/example.html",
            songs_list=songs_list,
            missing_count=1
        )
        self.assertTrue(created1)

        # Unread count should be 1
        self.assertEqual(self.db.get_unread_count(), 1)

        # Scan 2: next morning scan runs with identical missing songs -> MUST DEDUPLICATE
        created2 = self.db.create_or_update_notification(
            album_id=1,
            notification_type=NotificationType.NEW_SONG,
            title="New Song: Example",
            message="1 song released",
            album_name="Example Movie",
            artwork_url="https://example.com/art.jpg",
            music_director="Director A",
            year=2026,
            source_url="https://naasongs.com.co/example.html",
            songs_list=songs_list,
            missing_count=1
        )
        self.assertFalse(created2) # DEDUPLICATED!

        # Unread count is still 1!
        self.assertEqual(self.db.get_unread_count(), 1)

        # Mark read
        notifs = self.db.get_notifications()
        self.assertEqual(len(notifs), 1)
        self.db.mark_notification_read(notifs[0]["id"])
        self.assertEqual(self.db.get_unread_count(), 0)

    def test_update_tracking_status(self):
        album = DiscoveredAlbum(
            album_name="Track Movie",
            normalized_name="track movie",
            source_url="https://naasongs.com.co/track.html"
        )
        album_id = self.db.upsert_discovered_album(album)
        song = DiscoveredSong(
            song_title="Track Song",
            normalized_title="track song",
            tracking_status=TrackingStatus.DISCOVERED
        )
        song_id = self.db.upsert_discovered_song(album_id, song)

        # User confirms downloaded
        self.db.update_song_tracking_status(song_id, TrackingStatus.DOWNLOADED_BY_USER)

        conn = self.db._get_connection()
        cur = conn.cursor()
        cur.execute("SELECT tracking_status FROM discovered_songs WHERE id = ?;", (song_id,))
        status = cur.fetchone()[0]
        conn.close()
        self.assertEqual(status, TrackingStatus.DOWNLOADED_BY_USER.value)

if __name__ == "__main__":
    unittest.main()
