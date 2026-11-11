import os
import pytest
from fastapi.testclient import TestClient
from backend.main import app
from backend.services.catalog import cloud_catalog_service

client = TestClient(app)

class TestEditAlbumExistingSongs:
    """
    Verifies that Edit Album retrieves authoritative songs from the catalog
    and marks them with is_existing=True and status='EXISTS' in Cloud Mode and Local Mode.
    """

    def test_cloud_mode_get_album_returns_existing_songs(self, monkeypatch):
        # Force cloud mode
        monkeypatch.setenv("CLOUD_MODE", "1")

        # Request OM – Telugu
        res = client.get("/api/albums/OM%20%E2%80%93%20Telugu")
        assert res.status_code == 200
        data = res.json()

        assert data["album_name"] == "OM – Telugu"
        assert data["song_count"] >= 3
        assert len(data["songs"]) >= 3

        song_titles = [s["title"] for s in data["songs"]]
        assert "Alaakaa Loova Song" in song_titles
        assert "Alaakaa Loova" in song_titles
        assert "Veera Veera" in song_titles

        for song in data["songs"]:
            assert song["is_existing"] is True
            assert song["status"] == "EXISTS"
            assert song["track_number"] is not None
            assert song["filename"].endswith(".mp3")
            assert "audio_url" in song
            assert song["audio_url"].startswith("https://raw.githubusercontent.com/")

    def test_cloud_mode_canonical_resolution_returns_existing_songs(self, monkeypatch):
        monkeypatch.setenv("CLOUD_MODE", "1")

        # Request using query parameter language
        res = client.get("/api/albums/OM?language=Telugu")
        assert res.status_code == 200
        data = res.json()
        assert data["album_name"] == "OM – Telugu"
        assert len(data["songs"]) >= 3
        for s in data["songs"]:
            assert s["status"] == "EXISTS"

        # Request using formatted name with parentheses language
        res2 = client.get("/api/albums/OM%20(Telugu)")
        assert res2.status_code == 200
        data2 = res2.json()
        assert data2["album_name"] == "OM – Telugu"

    def test_nonexistent_album_returns_404(self, monkeypatch):
        monkeypatch.setenv("CLOUD_MODE", "1")
        res = client.get("/api/albums/NonExistentAlbum999xyz")
        assert res.status_code == 404

    def test_local_mode_enriches_existing_songs(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CLOUD_MODE", raising=False)
        from backend.core.config import settings
        monkeypatch.setattr(settings, "MUSICDATA_REPOSITORY_ROOT", str(tmp_path))
        (tmp_path / "generate_metadata.py").write_text("# dummy")
        (tmp_path / "generator").mkdir()
        (tmp_path / "metadata").mkdir()
        (tmp_path / ".git").mkdir()

        album_dir = tmp_path / "Test Movie"
        album_dir.mkdir(parents=True)
        (album_dir / "01 - Intro.mp3").write_bytes(b"dummy mp3 data 1")
        (album_dir / "02 - Main Song.mp3").write_bytes(b"dummy mp3 data 2")
        (album_dir / "Test Movie.png").write_bytes(b"dummy png")

        res = client.get("/api/albums/Test%20Movie")
        assert res.status_code == 200
        data = res.json()

        assert data["album_name"] == "Test Movie"
        assert data["song_count"] == 2
        assert len(data["songs"]) == 2

        titles = [s["title"] for s in data["songs"]]
        assert "01 - Intro" in titles
        assert "02 - Main Song" in titles

        for s in data["songs"]:
            assert s["is_existing"] is True
            assert s["status"] == "EXISTS"
            assert s["track_number"] in [1, 2]
            assert s["file_size"] > 0
