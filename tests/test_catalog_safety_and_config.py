import io
import json
import socket
import time
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from backend.core.config import Settings, resolve_musicdata_github_repository
from backend.services.catalog import CatalogLookupError, CloudCatalogService
from backend.services.latest_releases.db import LatestReleasesDB
from backend.services.latest_releases.discovery import LatestReleasesDiscoveryEngine
from backend.services.latest_releases.models import (
    DiscoveredAlbum,
    DiscoveredSong,
    MatchClassification,
    MatchConfidence,
    NotificationResolutionStatus,
    NotificationType,
)
from backend.services.latest_releases.reconciliation import reconcile_active_notifications
from backend.services.latest_releases.song_comparator import SongComparator

REPO_ROOT = Path(__file__).resolve().parent.parent


def _http_error(code: int, url: str = "https://raw.githubusercontent.com/test", msg: str = "Err"):
    return urllib.error.HTTPError(url=url, code=code, msg=msg, hdrs=None, fp=io.BytesIO(b""))


class _FakeResponse:
    def __init__(self, payload: bytes, status: int = 200):
        self._payload = payload
        self.status = status

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class TestCatalogSafetyAndRepositoryConfig:
    """
    Regression suite verifying:
    - Dedicated MUSICDATA_GITHUB_REPOSITORY avoids GitHub Actions GITHUB_REPOSITORY collisions.
    - Centralized raw GitHub URL generation targets vamshivamshi9630/MusicData/main.
    - Catalog failures (404, 401/403, timeout, invalid JSON) raise CatalogLookupError cleanly,
      never return [] as a false empty catalog, never create false NEW_ALBUM notifications,
      and preserve existing songs, version matching, language checks, and reconciliation safety.
    """

    def test_01_github_actions_reserved_variable_collision_prevented(self, monkeypatch):
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("GITHUB_REPOSITORY", "vamshivamshi9630/MusicDataManager")
        monkeypatch.setenv("GITHUB_OWNER", "vamshivamshi9630")
        monkeypatch.setenv("MUSICDATA_GITHUB_REPOSITORY", "MusicData")
        monkeypatch.setenv("GITHUB_BRANCH", "main")

        s = Settings()
        assert s.MUSICDATA_GITHUB_REPOSITORY == "MusicData"
        assert s.musicdata_repo_slug == "vamshivamshi9630/MusicData"
        assert (
            s.build_raw_github_url("metadata/indexes/albums.json")
            == "https://raw.githubusercontent.com/vamshivamshi9630/MusicData/main/metadata/indexes/albums.json"
        )

        # Even if MUSICDATA_GITHUB_REPOSITORY is temporarily unset in GitHub Actions,
        # predefined GITHUB_REPOSITORY=owner/MusicDataManager must NEVER leak into catalog repo
        monkeypatch.delenv("MUSICDATA_GITHUB_REPOSITORY", raising=False)
        assert resolve_musicdata_github_repository() == "MusicData"

    def test_02_safe_backward_compatibility_and_owner_prefix_stripping(self, monkeypatch):
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.delenv("MUSICDATA_GITHUB_REPOSITORY", raising=False)
        monkeypatch.setenv("GITHUB_REPOSITORY", "CustomCatalogFork")
        assert resolve_musicdata_github_repository() == "CustomCatalogFork"

        # Reject unambiguous app-repo or owner/repo values in legacy GITHUB_REPOSITORY
        monkeypatch.setenv("GITHUB_REPOSITORY", "vamshivamshi9630/MusicDataManager")
        assert resolve_musicdata_github_repository() == "MusicData"

        monkeypatch.setenv("MUSICDATA_GITHUB_REPOSITORY", "vamshivamshi9630/MusicData")
        assert resolve_musicdata_github_repository() == "MusicData"

    def test_03_production_config_files_use_dedicated_variable(self):
        wf_text = (REPO_ROOT / ".github" / "workflows" / "latest-releases-daily.yml").read_text(encoding="utf-8")
        wf = yaml.safe_load(wf_text)
        job_env = wf["jobs"]["discovery-scan"]["env"]
        assert job_env["GITHUB_OWNER"] == "vamshivamshi9630"
        assert job_env["MUSICDATA_GITHUB_REPOSITORY"] == "MusicData"
        assert job_env["GITHUB_BRANCH"] == "main"
        assert job_env["REQUIRE_POSTGRES"] == "1"
        # Ensure workflow never overrides reserved GITHUB_REPOSITORY
        assert "GITHUB_REPOSITORY" not in job_env
        assert "secrets.DATABASE_URL" in wf_text

        render_cfg = yaml.safe_load((REPO_ROOT / "render.yaml").read_text(encoding="utf-8"))
        web_env = {item["key"]: item.get("value") for item in render_cfg["services"][0]["envVars"]}
        assert web_env["GITHUB_OWNER"] == "vamshivamshi9630"
        assert web_env["MUSICDATA_GITHUB_REPOSITORY"] == "MusicData"
        assert web_env["GITHUB_BRANCH"] == "main"
        assert "GITHUB_REPOSITORY" not in web_env

        env_ex = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        assert "MUSICDATA_GITHUB_REPOSITORY=MusicData" in env_ex
        assert "\nGITHUB_REPOSITORY=" not in env_ex

    def test_04_centralized_url_construction_in_catalog_service(self, monkeypatch):
        monkeypatch.setenv("GITHUB_OWNER", "vamshivamshi9630")
        monkeypatch.setenv("MUSICDATA_GITHUB_REPOSITORY", "MusicData")
        monkeypatch.setenv("GITHUB_BRANCH", "main")
        from backend.core.config import settings
        settings.refresh_from_env()

        svc = CloudCatalogService()
        assert svc.raw_base_url == "https://raw.githubusercontent.com/vamshivamshi9630/MusicData/main"

        captured_urls = []

        def fake_urlopen(req, timeout=10):
            captured_urls.append(req.full_url)
            return _FakeResponse(json.dumps([{"name": "Ranabaali"}]).encode("utf-8"))

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            idx = svc.get_albums_index(force_refresh=True)

        assert len(idx) == 1
        assert captured_urls[0].split("?")[0] == (
            "https://raw.githubusercontent.com/vamshivamshi9630/MusicData/main/metadata/indexes/albums.json"
        )

    def test_05_http_404_raises_catalog_lookup_error_never_empty_catalog(self):
        svc = CloudCatalogService(owner="vamshivamshi9630", repo="WrongRepo", branch="main")
        # Even if cache existed earlier, HTTP 404 must NOT silently treat missing repo as valid stale catalog
        svc._albums_index_cache = [{"name": "Cached Album"}]
        svc._last_fetch_time = time.time() - 120

        with patch("urllib.request.urlopen", side_effect=_http_error(404)):
            with pytest.raises(CatalogLookupError) as exc:
                svc.get_albums_index()
        assert exc.value.reason == "not_found"
        assert exc.value.status_code == 404

    def test_06_auth_failure_timeout_and_invalid_json_distinction(self, caplog):
        svc = CloudCatalogService(owner="vamshivamshi9630", repo="MusicData", branch="main")

        # 1. Auth error (401/403) must raise without exposing secrets
        with patch("urllib.request.urlopen", side_effect=_http_error(403, msg="Forbidden")):
            with pytest.raises(CatalogLookupError) as exc_auth:
                svc.get_albums_index(force_refresh=True, allow_stale_fallback=False)
        assert exc_auth.value.reason == "auth_error"
        assert exc_auth.value.status_code == 403

        # 2. Timeout must raise reason='timeout'
        with patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")):
            with pytest.raises(CatalogLookupError) as exc_to:
                svc.get_albums_index(force_refresh=True, allow_stale_fallback=False)
        assert exc_to.value.reason == "timeout"

        # 3. Invalid JSON must raise reason='invalid_json'
        with patch("urllib.request.urlopen", return_value=_FakeResponse(b"<html>not json</html>")):
            with pytest.raises(CatalogLookupError) as exc_json:
                svc.get_albums_index(force_refresh=True, allow_stale_fallback=False)
        assert exc_json.value.reason == "invalid_json"

        # 4. Non-list JSON format must raise reason='invalid_format'
        with patch("urllib.request.urlopen", return_value=_FakeResponse(json.dumps({"unexpected": True}).encode("utf-8"))):
            with pytest.raises(CatalogLookupError) as exc_fmt:
                svc.get_albums_index(force_refresh=True, allow_stale_fallback=False)
        assert exc_fmt.value.reason == "invalid_format"

    def test_07_stale_cache_fallback_bounded_and_flagged(self):
        svc = CloudCatalogService(owner="vamshivamshi9630", repo="MusicData", branch="main")
        svc._albums_index_cache = [{"name": "OM – Telugu", "language": "Telugu"}]
        svc._last_fetch_time = time.time() - 120  # expired fresh TTL (60s), within stale limit (3600s)

        # Transient timeout uses bounded stale cache and marks stale=True
        with patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")):
            cached = svc.get_albums_index(force_refresh=False, allow_stale_fallback=True)
        assert len(cached) == 1
        status = svc.get_cache_status()
        assert status["cached"] is True
        assert status["stale"] is True
        assert status["last_error"]["reason"] == "timeout"

        # Beyond MAX_STALE_CACHE_AGE_SECONDS, stale cache must expire cleanly and raise
        svc._last_fetch_time = time.time() - (svc.MAX_STALE_CACHE_AGE_SECONDS + 30)
        with patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")):
            with pytest.raises(CatalogLookupError) as exc_exp:
                svc.get_albums_index(force_refresh=False, allow_stale_fallback=True)
        assert exc_exp.value.reason == "timeout"

    def test_08_existing_album_never_becomes_new_album_on_catalog_failure(self, tmp_path):
        db = LatestReleasesDB(sqlite_path=str(tmp_path / "catalog_outage.db"))
        mock_source = MagicMock()
        mock_source.fetch_page.side_effect = [
            '<html><div class="entry-content"><a href="https://naasongs.com.co/ranabaali-2026-songs-c.html">Ranabaali</a></div></html>',
            None,
        ]
        mock_source.fetch_album_page.return_value = """
        <html><div class="entry-content">
            <p>Movie Title:- Ranabaali</p>
            <p>Music:- Ajay Atul</p>
            <p>Language:- Telugu</p>
            <p>Year:- 2026</p>
            <p>01. Endhayya Saami Song (Full) – Swetha Mohan</p>
            <p>128 Kbps - <a href="https://mp3teluguwap.net/s1.mp3">Download</a></p>
        </div></html>
        """

        engine = LatestReleasesDiscoveryEngine(db=db, source_client=mock_source)
        with patch(
            "backend.services.catalog.CloudCatalogService.get_albums_index",
            side_effect=CatalogLookupError("Simulated HTTP 404", reason="not_found", status_code=404, path="metadata/indexes/albums.json"),
        ):
            res = engine.run_scan(locked_by="test_outage", max_pages=1)

        assert res["success"] is False
        assert res["status"] == "FAILED"
        assert res["new_albums_count"] == 0
        # Zero false NEW_ALBUM notifications must be created
        assert db.get_notifications(status="all") == []

        # Also verify mark_album_uncertain never sets MISSING_FROM_MUSICDATA
        disc = DiscoveredAlbum(
            album_name="Ranabaali",
            normalized_name="ranabaali",
            source_url="https://naasongs.com.co/ranabaali-2026-songs-c.html",
            songs=[DiscoveredSong(song_title="Endhayya Saami Song (Full)", normalized_title="endhayya saami song full")],
        )
        uncertain = SongComparator.mark_album_uncertain(disc, reason="HTTP 404")
        assert uncertain.songs[0].match_classification == MatchClassification.UNCERTAIN
        assert uncertain.songs[0].match_confidence == MatchConfidence.UNCERTAIN

    def test_09_matching_language_version_and_reconciliation_preserved(self, tmp_path):
        svc = CloudCatalogService(owner="vamshivamshi9630", repo="MusicData", branch="main")
        mock_index = [
            {"id": "om-te", "name": "OM – Telugu", "language": "Telugu", "year": 2026, "partition": "O", "songCount": 1},
            {"id": "om-ta", "name": "OM – Tamil", "language": "Tamil", "year": 2026, "partition": "O", "songCount": 1},
            {"id": "ran", "name": "Ranabaali", "language": "Telugu", "year": 2026, "partition": "R", "songCount": 1},
        ]
        # Language verification still differentiates OM Telugu vs OM Tamil
        assert svc.find_canonical_album("OM", query_language="Telugu", candidate_albums=mock_index)["id"] == "om-te"
        assert svc.find_canonical_album("OM", query_language="Tamil", candidate_albums=mock_index)["id"] == "om-ta"

        # Existing songs & Promo vs Full version matching preserved
        cat_songs = [{"id": "s1", "title": "Endhayya Saami Song (Promo)", "singers": ["Swetha Mohan"]}]
        res_exact = svc.match_song_detailed("Endhayya Saami Song (Promo)", cat_songs, source_singer="Swetha Mohan")
        assert res_exact["exists_in_catalog"] is True
        assert res_exact["classification"] == "EXACT_MATCH"

        res_ver = svc.match_song_detailed("Endhayya Saami Song (Full)", cat_songs, source_singer="Swetha Mohan")
        assert res_ver["exists_in_catalog"] is False
        assert res_ver["classification"] == "POSSIBLE_VERSION_MATCH"

        # Reconciliation preserves pending notifications on CatalogLookupError
        db = LatestReleasesDB(sqlite_path=str(tmp_path / "recon_safe.db"))
        db.create_or_update_notification(
            album_id=1,
            notification_type=NotificationType.MISSING_SONGS,
            title="Missing Songs: Ranabaali",
            message="1 missing",
            album_name="Ranabaali",
            artwork_url=None,
            music_director="Ajay Atul",
            year=2026,
            source_url="https://naasongs.com.co/ranabaali-2026-songs-c.html",
            songs_list=[{"song_title": "Kobali Song", "exists_in_catalog": False}],
            missing_count=1,
            language="Telugu",
        )
        failing_comp = MagicMock()
        failing_comp.find_matching_catalog_album.side_effect = CatalogLookupError("HTTP 503", reason="http_error", status_code=503)

        summary = reconcile_active_notifications(db=db, comparator=failing_comp)
        assert summary["resolved_notifications"] == 0
        assert summary["deferred_on_catalog_error"] == 1
        active = db.get_notifications(status="active")
        assert len(active) == 1
        assert active[0]["resolution_status"] == NotificationResolutionStatus.PENDING.value

    def test_10_routes_return_503_not_404_on_catalog_failure(self, monkeypatch):
        from fastapi.testclient import TestClient
        from backend.main import app

        monkeypatch.setenv("CLOUD_MODE", "1")
        client = TestClient(app)

        err = CatalogLookupError("Simulated HTTP 404 from wrong repo", reason="not_found", status_code=404)
        with patch("backend.services.catalog.CloudCatalogService.get_albums_index", side_effect=err):
            r_album = client.get("/api/albums/Ranabaali")
            assert r_album.status_code == 503
            r_search = client.get("/api/albums/search?q=ran")
            assert r_search.status_code == 503
            r_dup = client.post("/api/albums/check-duplicate", json={"album_name": "Ranabaali", "mode": "add"})
            assert r_dup.status_code == 503
