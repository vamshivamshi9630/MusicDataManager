import pytest
from fastapi.testclient import TestClient
from backend.main import app
from backend.services.catalog import CatalogLookupError
from backend.services.latest_releases.db import LatestReleasesDB
from backend.services.latest_releases.reconciliation import reconcile_active_notifications
from backend.services.latest_releases.models import (
    NotificationType,
    NotificationResolutionStatus,
    TrackingStatus
)

client = TestClient(app)

@pytest.fixture
def test_db(tmp_path):
    db_file = tmp_path / "test_reconciliation.db"
    db = LatestReleasesDB(sqlite_path=str(db_file))
    return db

class MockComparator:
    def __init__(self, album_match=None, catalog_songs=None, error=False):
        self.album_match = album_match
        self.catalog_songs = catalog_songs or []
        self.error = error

    def find_matching_catalog_album(self, album_name, language=None, year=None):
        if self.error:
            raise CatalogLookupError("Simulated network failure")
        return self.album_match

    def fetch_catalog_album_songs(self, catalog_album):
        if self.error:
            raise CatalogLookupError("Simulated network failure")
        return self.catalog_songs

class TestLatestReleasesV3Reconciliation:
    """
    Tests live notification auto-repair and reconciliation logic:
    - Auto-resolves notifications to COMPLETED when songs exist in MusicData.
    - Transitions to PARTIALLY_COMPLETED when partial songs exist.
    - Preserves existing notifications without changes if catalog lookup fails.
    - Tests the /api/latest-releases/reconcile endpoint.
    """

    def test_reconcile_all_songs_match_marks_completed(self, test_db):
        test_db.create_or_update_notification(
            album_id=1,
            notification_type=NotificationType.MISSING_SONGS,
            title="Missing songs in OM – Telugu",
            message="2 missing songs found",
            album_name="OM – Telugu",
            artwork_url="https://example.com/art.png",
            music_director="Unknown",
            year=2026,
            source_url="https://naasongs.com.co/om-songs.html",
            songs_list=[
                {"song_title": "Alaakaa Loova Song", "exists_in_catalog": False},
                {"song_title": "Veera Veera", "exists_in_catalog": False}
            ],
            missing_count=2,
            language="Telugu"
        )
        notif_id = test_db.get_notifications(limit=1)[0]["id"]

        mock_comp = MockComparator(
            album_match={"name": "OM – Telugu", "partition": "O"},
            catalog_songs=[
                {"title": "Alaakaa Loova Song", "id": "song1"},
                {"title": "Veera Veera", "id": "song2"}
            ]
        )

        summary = reconcile_active_notifications(db=test_db, comparator=mock_comp)
        assert summary["resolved_notifications"] == 1
        assert summary["partially_resolved"] == 0

        # Verify DB state (it is now completed, so query with status="completed" or "all")
        notifs = test_db.get_notifications(status="completed", limit=10)
        assert len(notifs) == 1
        n = notifs[0]
        assert n["id"] == notif_id
        assert n["resolution_status"] == NotificationResolutionStatus.COMPLETED.value
        assert n["missing_count"] == 0
        assert n["is_read"] == 1
        for s in n["songs"]:
            assert s["exists_in_catalog"] is True
            assert s["tracking_status"] == TrackingStatus.ADDED_TO_MUSICDATA.value

    def test_reconcile_partial_songs_match_marks_partially_completed(self, test_db):
        test_db.create_or_update_notification(
            album_id=2,
            notification_type=NotificationType.MISSING_SONGS,
            title="Missing songs in OM – Telugu",
            message="2 missing songs found",
            album_name="OM – Telugu",
            artwork_url="https://example.com/art.png",
            music_director="Unknown",
            year=2026,
            source_url="https://naasongs.com.co/om-songs.html",
            songs_list=[
                {"song_title": "Alaakaa Loova Song", "exists_in_catalog": False},
                {"song_title": "Unreleased Secret Song", "exists_in_catalog": False}
            ],
            missing_count=2,
            language="Telugu"
        )
        notif_id = test_db.get_notifications(limit=1)[0]["id"]

        mock_comp = MockComparator(
            album_match={"name": "OM – Telugu", "partition": "O"},
            catalog_songs=[
                {"title": "Alaakaa Loova Song", "id": "song1"}
            ]
        )

        summary = reconcile_active_notifications(db=test_db, comparator=mock_comp)
        assert summary["resolved_notifications"] == 0
        assert summary["partially_resolved"] == 1

        notifs = test_db.get_notifications(limit=10)
        assert len(notifs) == 1
        n = notifs[0]
        assert n["id"] == notif_id
        assert n["resolution_status"] == NotificationResolutionStatus.PARTIALLY_COMPLETED.value
        assert n["missing_count"] == 1

        songs = n["songs"]
        assert songs[0]["song_title"] == "Alaakaa Loova Song"
        assert songs[0]["exists_in_catalog"] is True
        assert songs[1]["song_title"] == "Unreleased Secret Song"
        assert songs[1]["exists_in_catalog"] is False

    def test_reconcile_preserves_notifications_on_catalog_error(self, test_db):
        test_db.create_or_update_notification(
            album_id=3,
            notification_type=NotificationType.MISSING_SONGS,
            title="Missing songs in OM – Telugu",
            message="1 missing songs found",
            album_name="OM – Telugu",
            artwork_url="https://example.com/art.png",
            music_director="Unknown",
            year=2026,
            source_url="https://naasongs.com.co/om-songs.html",
            songs_list=[
                {"song_title": "Alaakaa Loova Song", "exists_in_catalog": False}
            ],
            missing_count=1,
            language="Telugu"
        )
        notif_id = test_db.get_notifications(limit=1)[0]["id"]

        # Comparator that throws error
        mock_comp = MockComparator(error=True)

        summary = reconcile_active_notifications(db=test_db, comparator=mock_comp)
        assert summary["resolved_notifications"] == 0
        assert summary["partially_resolved"] == 0

        # Verify notification remains PENDING with missing_count=1
        notifs = test_db.get_notifications(limit=10)
        assert len(notifs) == 1
        n = notifs[0]
        assert n["id"] == notif_id
        assert n["resolution_status"] == NotificationResolutionStatus.PENDING.value
        assert n["missing_count"] == 1

    def test_reconcile_api_endpoint(self, monkeypatch, test_db):
        # Override dependency in route to use test_db
        monkeypatch.setattr(
            "backend.services.latest_releases.db.get_latest_releases_db",
            lambda: test_db
        )
        monkeypatch.setattr(
            "backend.api.latest_releases_routes.get_latest_releases_db",
            lambda: test_db
        )

        test_db.create_or_update_notification(
            album_id=4,
            notification_type=NotificationType.MISSING_SONGS,
            title="Missing songs in OM – Telugu",
            message="2 missing songs found",
            album_name="OM – Telugu",
            artwork_url="https://example.com/art.png",
            music_director="Unknown",
            year=2026,
            source_url="https://naasongs.com.co/om-songs.html",
            songs_list=[
                {"song_title": "Alaakaa Loova Song", "exists_in_catalog": False},
                {"song_title": "Veera Veera", "exists_in_catalog": False}
            ],
            missing_count=2,
            language="Telugu"
        )

        res = client.post("/api/latest-releases/reconcile")
        assert res.status_code == 200
        data = res.json()
        assert data.get("success") is True
        assert "reconciled" in data
        assert data["reconciled"]["reconciled_count"] >= 1
        assert data["reconciled"]["resolved_notifications"] >= 1
