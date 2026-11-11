import os
import pytest
from fastapi.testclient import TestClient
from backend.services.catalog import cloud_catalog_service, CatalogLookupError
from backend.services.latest_releases.song_comparator import SongComparator
from backend.services.latest_releases.models import (
    DiscoveredAlbum,
    DiscoveredSong,
    NotificationType,
    TrackingStatus,
    MatchConfidence
)
from backend.main import app

@pytest.fixture(autouse=True)
def enable_cloud_mode(monkeypatch):
    monkeypatch.setenv("CLOUD_MODE", "1")

class TestLatestReleasesV3Identity:

    def test_canonical_album_matching_om_telugu_vs_om_tamil(self):
        cloud_catalog_service.refresh_catalog()

        # 1. OM with Telugu must match 'OM – Telugu'
        match_tel = cloud_catalog_service.find_canonical_album("OM", "Telugu")
        assert match_tel is not None
        assert "Telugu" in match_tel["name"]
        assert match_tel["name"] == "OM – Telugu"

        # 2. OM with Tamil must match 'OM – Tamil'
        match_tam = cloud_catalog_service.find_canonical_album("OM", "Tamil")
        assert match_tam is not None
        assert "Tamil" in match_tam["name"]
        assert match_tam["name"] == "OM – Tamil"

        # 3. OM with Hindi must NOT match Telugu or Tamil
        match_hin = cloud_catalog_service.find_canonical_album("OM", "Hindi")
        assert match_hin is None

        # 4. OM with unknown/None language must NOT guess between multi-lingual releases
        match_unk = cloud_catalog_service.find_canonical_album("OM", None)
        assert match_unk is None

    def test_authoritative_metadata_loading_quoted_url(self):
        cloud_catalog_service.refresh_catalog()
        om_tel = cloud_catalog_service.find_canonical_album("OM", "Telugu")
        assert om_tel is not None

        meta = cloud_catalog_service.get_album_metadata(om_tel)
        assert isinstance(meta, dict)
        assert "songs" in meta
        songs = meta["songs"]
        assert len(songs) >= 3

        song_titles = [s["title"] for s in songs]
        assert "Alaakaa Loova Song" in song_titles
        assert "Alaakaa Loova" in song_titles
        assert "Veera Veera" in song_titles

    def test_comparator_recognizes_all_existing_songs_no_notification(self):
        comp = SongComparator()
        disc = DiscoveredAlbum(
            album_name="OM",
            normalized_name="om",
            source_url="https://naasongs.com.co/om-2026-telugu-songs-a.html",
            language="Telugu",
            year=2026,
            songs=[
                DiscoveredSong(song_title="Alaakaa Loova", normalized_title="alaakaa loova"),
                DiscoveredSong(song_title="Veera Veera", normalized_title="veera veera")
            ]
        )

        notif_type, evaluated = comp.compare_album(disc)
        assert evaluated.exists_in_catalog is True
        assert evaluated.catalog_album_name == "OM – Telugu"
        # All songs already exist -> no active notification
        assert notif_type is None
        assert all(s.exists_in_catalog is True for s in evaluated.songs)
        assert all(s.tracking_status == TrackingStatus.ADDED_TO_MUSICDATA for s in evaluated.songs)

    def test_comparator_notifies_only_genuinely_missing_song(self):
        comp = SongComparator()
        disc = DiscoveredAlbum(
            album_name="OM",
            normalized_name="om",
            source_url="https://naasongs.com.co/om-2026-telugu-songs-a.html",
            language="Telugu",
            year=2026,
            songs=[
                DiscoveredSong(song_title="Alaakaa Loova", normalized_title="alaakaa loova"),
                DiscoveredSong(song_title="Veera Veera", normalized_title="veera veera"),
                DiscoveredSong(song_title="Brand New Unreleased Track", normalized_title="brand new unreleased track")
            ]
        )

        notif_type, evaluated = comp.compare_album(disc)
        assert evaluated.exists_in_catalog is True
        assert notif_type == NotificationType.NEW_SONG

        # First two songs exist
        assert evaluated.songs[0].exists_in_catalog is True
        assert evaluated.songs[1].exists_in_catalog is True

        # Only the third is missing
        missing_song = evaluated.songs[2]
        assert missing_song.exists_in_catalog is False
        assert missing_song.song_title == "Brand New Unreleased Track"
        assert missing_song.tracking_status == TrackingStatus.MISSING_FROM_MUSICDATA

    def test_comparator_multiple_missing_songs(self):
        comp = SongComparator()
        disc = DiscoveredAlbum(
            album_name="OM",
            normalized_name="om",
            source_url="https://naasongs.com.co/om-2026-telugu-songs-a.html",
            language="Telugu",
            year=2026,
            songs=[
                DiscoveredSong(song_title="Alaakaa Loova", normalized_title="alaakaa loova"),
                DiscoveredSong(song_title="New Track 1", normalized_title="new track 1"),
                DiscoveredSong(song_title="New Track 2", normalized_title="new track 2")
            ]
        )

        notif_type, evaluated = comp.compare_album(disc)
        assert evaluated.exists_in_catalog is True
        assert notif_type == NotificationType.MISSING_SONGS
        missing_count = sum(1 for s in evaluated.songs if not s.exists_in_catalog)
        assert missing_count == 2

    def test_catalog_error_does_not_treat_failed_fetch_as_empty_album(self, monkeypatch):
        comp = SongComparator()
        cloud_catalog_service.refresh_catalog()

        def mock_error_fetch(path, bypass_cache=True):
            raise CatalogLookupError(f"Simulated network timeout for {path}")

        monkeypatch.setattr(cloud_catalog_service, "_fetch_json", mock_error_fetch)

        with pytest.raises(CatalogLookupError):
            comp.fetch_catalog_album_songs({"name": "OM – Telugu", "partition": "O"})

    def test_backend_download_protection_rejects_existing_song(self):
        client = TestClient(app)
        res = client.get(
            "/api/latest-releases/download-track"
            "?url=https://mp3teluguwap.net/mp3/2026/Om/Telugu/Alaakaa%20Loova.mp3"
            "&album_name=OM%20%E2%80%93%20Telugu"
            "&song_title=Alaakaa%20Loova"
        )
        assert res.status_code == 400
        data = res.json()
        error_msg = data.get("error") or data.get("detail") or ""
        assert "Duplicate Download Blocked" in error_msg
        assert "already exists in MusicData" in error_msg
