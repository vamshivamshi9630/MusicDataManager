import pytest
from unittest.mock import MagicMock
from backend.services.latest_releases.models import NotificationType, NotificationResolutionStatus
from backend.services.latest_releases.db import LatestReleasesDB
from backend.services.latest_releases.song_comparator import SongComparator
from backend.services.latest_releases.reconciliation import reconcile_active_notifications

class TestLatestReleasesV2Reconciliation:

    def test_partial_and_full_reconciliation(self, tmp_path):
        db_file = tmp_path / "test_reconciliation_db.db"
        db = LatestReleasesDB(sqlite_path=str(db_file))

        # Initial state: Album "Pushpa 2" with 3 songs discovered, all 3 missing
        initial_songs = [
            {"song_title": "Pushpa Pushpa", "exists_in_catalog": False},
            {"song_title": "Sooseki", "exists_in_catalog": False},
            {"song_title": "Peelings", "exists_in_catalog": False}
        ]

        db.create_or_update_notification(
            album_id=1,
            notification_type=NotificationType.NEW_ALBUM,
            title="New Album: Pushpa 2",
            message="3 songs missing",
            album_name="Pushpa 2",
            artwork_url=None,
            music_director="DSP",
            year=2024,
            source_url="https://naasongs.com.co/pushpa-2.html",
            songs_list=initial_songs,
            missing_count=3,
            language="Telugu"
        )

        assert db.get_unread_count() == 1

        # Stage 1: User adds "Pushpa Pushpa" and "Sooseki" into MusicData catalog
        catalog_mock = MagicMock()
        catalog_mock.find_matching_catalog_album.return_value = {"name": "Pushpa 2", "language": "Telugu"}
        catalog_mock.fetch_catalog_album_songs.return_value = [
            {"title": "Pushpa Pushpa"},
            {"title": "Sooseki"}
        ]
        catalog_mock.HIGH_CONFIDENCE_THRESHOLD = 0.88
        catalog_mock.normalize_song_title = SongComparator.normalize_song_title

        recon_res = reconcile_active_notifications(db=db, comparator=catalog_mock)
        assert recon_res["reconciled_count"] == 1
        assert recon_res["partially_resolved"] == 1
        assert recon_res["resolved_notifications"] == 0

        # Check DB notification updated to PARTIALLY_COMPLETED, missing_count=1
        active_notifs = db.get_notifications(status="active")
        assert len(active_notifs) == 1
        assert active_notifs[0]["resolution_status"] == NotificationResolutionStatus.PARTIALLY_COMPLETED.value
        assert active_notifs[0]["missing_count"] == 1
        # Pushpa Pushpa and Sooseki should now show exists_in_catalog = True
        songs = active_notifs[0]["songs"]
        assert songs[0]["exists_in_catalog"] is True
        assert songs[1]["exists_in_catalog"] is True
        assert songs[2]["exists_in_catalog"] is False

        # Stage 2: User adds remaining song "Peelings" into MusicData
        catalog_mock.fetch_catalog_album_songs.return_value = [
            {"title": "Pushpa Pushpa"},
            {"title": "Sooseki"},
            {"title": "Peelings"}
        ]

        recon_res2 = reconcile_active_notifications(db=db, comparator=catalog_mock)
        assert recon_res2["resolved_notifications"] == 1

        # Check notification is now COMPLETED and removed from active list!
        active_notifs2 = db.get_notifications(status="active")
        assert len(active_notifs2) == 0

        # It is still present in completed / all history
        completed_notifs = db.get_notifications(status="completed")
        assert len(completed_notifs) == 1
        assert completed_notifs[0]["resolution_status"] == NotificationResolutionStatus.COMPLETED.value
        assert completed_notifs[0]["missing_count"] == 0

        # Unread count should now be 0!
        assert db.get_unread_count() == 0
