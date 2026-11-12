import http.server
import io
import json
import os
import shutil
import socketserver
import subprocess
import sys
import threading
import urllib.error
from pathlib import Path
from typing import Dict, Any, List, Optional
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from backend.main import app
from backend.core.config import settings
from backend.core.repository import CloudRepository
from backend.services.catalog import CatalogLookupError, cloud_catalog_service
from backend.services.git_sync import BaseGitSyncService, GitSyncError
from backend.services.latest_releases.db import LatestReleasesDB
from backend.services.latest_releases.models import DiscoveredAlbum, NotificationResolutionStatus, NotificationType
from backend.services.latest_releases.musicdata_uploader import (
    STATUS_ADDED,
    STATUS_FAILED,
    STATUS_PARTIALLY_FAILED,
    STATUS_SKIPPED,
    MusicDataSyncOrchestrator,
    UploadPipelineError,
    build_add_to_musicdata_preview,
    download_and_validate_mp3,
    is_public_safe_url,
    sanitize_sensitive_text,
    selective_reconcile_notification,
    verify_album_files_in_repo,
)
from backend.services.validation import ValidationService, FileValidationError


def make_valid_mp3_bytes(frames: int = 45, tag: str = "default") -> bytes:
    """Builds valid MPEG1 Layer-III 128kbps 44.1kHz frames (>=1s duration) unique per `tag`."""
    # Sync=0xFFF, MPEG1=1, Layer3=01, noCRC=1 -> 0xFF 0xFB; 128kbps(1001), 44100Hz(00) -> 0x90 0x00
    # Frame length = 144 * 128000 / 44100 = 417 bytes
    tag_bytes = tag.encode("utf-8")[:100]
    pad_len = 413 - len(tag_bytes)
    frame = b"\xff\xfb\x90\x00" + tag_bytes + (b"\x00" * pad_len)
    return frame * frames


def make_valid_jpeg_bytes() -> bytes:
    img = Image.new("RGB", (120, 120), color=(30, 144, 255))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def create_bare_and_working_musicdata_repo(tmp_path: Path) -> Dict[str, Path]:
    """Creates a realistic MusicData git repository + bare remote with existing albums & generators."""
    remote_dir = tmp_path / "MusicData.git"
    work_dir = tmp_path / "MusicData_seed"
    remote_dir.mkdir(parents=True)
    work_dir.mkdir(parents=True)

    subprocess.run(["git", "init", "--bare", str(remote_dir)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(work_dir)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=work_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@musicdata.internal"], cwd=work_dir, check=True)

    # Create generator package and authoritative generator scripts
    (work_dir / "generator").mkdir(parents=True)
    (work_dir / "generator" / "__init__.py").write_text("", encoding="utf-8")
    (work_dir / "generator" / "config.py").write_text(
        "from pathlib import Path\n"
        "import os\n"
        "BASE_PATH = Path(os.environ.get('MUSICDATA_REPOSITORY_ROOT', Path(__file__).resolve().parent.parent))\n"
        "AUDIO_EXTENSIONS = ['.mp3']\n"
        "IMAGE_EXTENSIONS = ['.png', '.jpg', '.jpeg']\n",
        encoding="utf-8",
    )
    (work_dir / "generator" / "scanner.py").write_text("# overwritten in cloud mode\n", encoding="utf-8")

    gen1_code = '''import json, os, hashlib
from pathlib import Path

root = Path(os.environ.get("MUSICDATA_REPOSITORY_ROOT", Path(__file__).resolve().parent))
meta_dir = root / "metadata"
albums_dir = meta_dir / "albums"
idx_dir = meta_dir / "indexes"
albums_dir.mkdir(parents=True, exist_ok=True)
idx_dir.mkdir(parents=True, exist_ok=True)

ignored = {".git", "generator", "metadata", "tmp", "__pycache__", ".idea", "MusicDirectorImages", "New_Icons", ".staging"}
albums_idx = []
total_songs = 0

for d in sorted([p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".") and p.name not in ignored], key=lambda x: x.name.lower()):
    info_file = d / "album_info.json"
    if not info_file.exists():
        continue
    info = json.loads(info_file.read_text(encoding="utf-8"))
    mp3s = sorted([f for f in d.glob("*.mp3") if f.stat().st_size > 0], key=lambda x: x.name.lower())
    if not mp3s:
        continue
    img = f"{d.name}.png" if (d / f"{d.name}.png").exists() else None
    part = d.name[0].upper() if d.name[0].isalpha() else "0-9"
    (albums_dir / part).mkdir(parents=True, exist_ok=True)
    songs_list = []
    for idx, mp3 in enumerate(mp3s, start=1):
        sid = hashlib.sha256(f"{d.name}{mp3.name}".lower().encode()).hexdigest()[:12]
        songs_list.append({
            "id": sid,
            "title": mp3.stem,
            "audio": mp3.name,
            "trackNumber": idx,
            "fileSize": mp3.stat().st_size,
            "singers": ["Singer A"],
            "composer": info.get("musicDirector", "Unknown"),
        })
    total_songs += len(songs_list)
    alb_payload = {
        "album": {
            "name": d.name,
            "musicDirector": info.get("musicDirector", "Unknown"),
            "year": info.get("year", 2026),
            "genre": info.get("genre", "Soundtrack"),
            "language": info.get("language", "Telugu"),
            "image": img,
        },
        "songs": songs_list,
    }
    (albums_dir / part / f"{d.name}.json").write_text(json.dumps(alb_payload, indent=2), encoding="utf-8")
    albums_idx.append({
        "name": d.name,
        "artist": info.get("musicDirector", "Unknown"),
        "year": info.get("year", 2026),
        "language": info.get("language", "Telugu"),
        "image": img,
        "songCount": len(songs_list),
        "partition": part,
    })

(idx_dir / "albums.json").write_text(json.dumps(albums_idx, indent=2), encoding="utf-8")
(idx_dir / "search_index.json").write_text(json.dumps({"albums": len(albums_idx)}), encoding="utf-8")
(idx_dir / "artists.json").write_text(json.dumps([]), encoding="utf-8")
(meta_dir / "manifest.json").write_text(json.dumps({"version": 1}), encoding="utf-8")
(meta_dir / "statistics.json").write_text(json.dumps({"totalAlbums": len(albums_idx), "totalSongs": total_songs}), encoding="utf-8")
'''
    (work_dir / "generate_metadata.py").write_text(gen1_code, encoding="utf-8")
    (work_dir / "generate_or_update_songs_with_details.py").write_text(
        'import json, os\nfrom pathlib import Path\n'
        'root = Path(os.environ.get("MUSICDATA_REPOSITORY_ROOT", Path(__file__).resolve().parent))\n'
        '(root / "songs_with_details.json").write_text(json.dumps({"ok": True}), encoding="utf-8")\n',
        encoding="utf-8",
    )

    # Existing Album 1: "Existing Hits" (Telugu) with 1 existing song & protected artwork
    ex_dir = work_dir / "Existing Hits"
    ex_dir.mkdir(parents=True)
    (ex_dir / "album_info.json").write_text(
        json.dumps({
            "album": "Existing Hits",
            "year": 2026,
            "musicDirector": "Thaman S",
            "genre": "Tollywood Soundtrack",
            "language": "Telugu",
            "country": "India",
            "releaseDate": "2026-01-15",
            "director": "Original Director",
            "producer": "Original Producer",
            "banner": "Original Banner",
        }, indent=2),
        encoding="utf-8",
    )
    (ex_dir / "First Track.mp3").write_bytes(make_valid_mp3_bytes(tag="existing_first"))
    (ex_dir / "Existing Hits.png").write_bytes(ValidationService.process_and_convert_to_png(make_valid_jpeg_bytes(), "art.jpg"))

    # Existing Album 2: "OM – Telugu" (strict language test)
    om_dir = work_dir / "OM – Telugu"
    om_dir.mkdir(parents=True)
    (om_dir / "album_info.json").write_text(
        json.dumps({"album": "OM – Telugu", "year": 2026, "musicDirector": "Devi", "language": "Telugu"}, indent=2),
        encoding="utf-8",
    )
    (om_dir / "Veera Veera.mp3").write_bytes(make_valid_mp3_bytes())
    (om_dir / "OM – Telugu.png").write_bytes(ValidationService.process_and_convert_to_png(make_valid_jpeg_bytes(), "om.jpg"))

    # Generate initial catalog JSONs
    env = os.environ.copy()
    env["MUSICDATA_REPOSITORY_ROOT"] = str(work_dir)
    subprocess.run([sys.executable, str(work_dir / "generate_metadata.py")], cwd=work_dir, env=env, check=True)
    subprocess.run([sys.executable, str(work_dir / "generate_or_update_songs_with_details.py")], cwd=work_dir, env=env, check=True)

    subprocess.run(["git", "add", "."], cwd=work_dir, check=True)
    subprocess.run(["git", "commit", "-m", "Initial seed"], cwd=work_dir, check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote_dir)], cwd=work_dir, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=work_dir, check=True, capture_output=True)

    return {"remote": remote_dir, "seed": work_dir}


def seed_notification(
    db: LatestReleasesDB,
    album_name: str,
    notification_type: NotificationType,
    language: str,
    songs: List[Dict[str, Any]],
    artwork_url: str = "https://naasongs.com.co/wp-content/uploads/art.jpg",
    source_url: Optional[str] = None,
) -> int:
    src_url = source_url or f"https://naasongs.com.co/{album_name.lower().replace(' ', '-')}-songs.html"
    alb_id = db.upsert_discovered_album(
        DiscoveredAlbum(
            album_name=album_name,
            normalized_name=album_name.lower(),
            source_url=src_url,
            music_director="Test Director",
            year=2026,
            language=language,
            artwork_url=artwork_url,
            exists_in_catalog=(notification_type != NotificationType.NEW_ALBUM),
            catalog_album_name=album_name if notification_type != NotificationType.NEW_ALBUM else None,
        )
    )
    missing_cnt = sum(1 for s in songs if not s.get("exists_in_catalog"))
    db.create_or_update_notification(
        album_id=alb_id,
        notification_type=notification_type,
        title=f"{notification_type.value}: {album_name}",
        message=f"[{language}] Test notification",
        album_name=album_name,
        artwork_url=artwork_url,
        music_director="Test Director",
        year=2026,
        source_url=src_url,
        songs_list=songs,
        missing_count=missing_cnt,
        language=language,
        source_page=1,
        source_position=1,
    )
    notifs = db.get_notifications(status="all", limit=50)
    return int(next(n["id"] for n in notifs if n["album_name"] == album_name and n["language"] == language))


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOUD_MODE", "0")
    monkeypatch.setenv("GIT_PUSH_ENABLED", "1")
    db_file = tmp_path / "test_latest_releases.db"
    db = LatestReleasesDB(sqlite_path=str(db_file), require_postgres=False)
    repos = create_bare_and_working_musicdata_repo(tmp_path)
    monkeypatch.setenv("MUSICDATA_REPOSITORY_ROOT", str(repos["seed"]))
    monkeypatch.setenv("SOURCE_REPO_OVERRIDE", str(repos["seed"]))
    monkeypatch.setattr(settings, "MUSICDATA_REPOSITORY_ROOT", str(repos["seed"]))
    monkeypatch.setattr(settings, "GITHUB_OWNER", "vamshivamshi9630")
    monkeypatch.setattr(settings, "MUSICDATA_GITHUB_REPOSITORY", "MusicData")
    monkeypatch.setattr(settings, "GITHUB_BRANCH", "main")
    cloud_catalog_service.refresh_catalog()
    yield {"db": db, "seed": repos["seed"], "remote": repos["remote"], "tmp": tmp_path}
    cloud_catalog_service.refresh_catalog()


class _FixtureHTTPHandler(http.server.BaseHTTPRequestHandler):
    routes: Dict[str, Any] = {}

    def do_GET(self):
        entry = self.routes.get(self.path)
        if entry is None:
            self.send_response(404)
            self.end_headers()
            return
        status_code, ctype, body, hdr_len = entry
        self.send_response(status_code)
        self.send_header("Content-Type", ctype)
        if hdr_len is not None:
            self.send_header("Content-Length", str(hdr_len))
        else:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


class TestAddNotificationsToMusicData:

    def test_01_new_album_upload_end_to_end(self, isolated_env):
        db: LatestReleasesDB = isolated_env["db"]
        seed_repo: Path = isolated_env["seed"]

        nid = seed_notification(
            db,
            album_name="Skyfall Anthem",
            notification_type=NotificationType.NEW_ALBUM,
            language="Telugu",
            songs=[
                {
                    "song_title": "Hero Entry Song",
                    "track_number": 1,
                    "singer": "Anirudh",
                    "available_bitrate": "320 Kbps",
                    "download_links": {"320 Kbps": "https://naasongs.com.co/dl/hero.mp3"},
                    "source_audio_url": "https://naasongs.com.co/dl/hero.mp3",
                    "exists_in_catalog": False,
                },
                {
                    "song_title": "Melody Breeze",
                    "track_number": 2,
                    "singer": "Shreya Ghoshal",
                    "available_bitrate": "128 Kbps",
                    "download_links": {"128 Kbps": "https://naasongs.com.co/dl/melody.mp3"},
                    "source_audio_url": "https://naasongs.com.co/dl/melody.mp3",
                    "exists_in_catalog": False,
                },
            ],
        )

        # Preview checks destination folder, new album flag, songs, artwork
        prev = build_add_to_musicdata_preview([nid], db=db, local_root_override=str(seed_repo))
        assert prev["summary"]["total_albums"] == 1
        assert prev["summary"]["total_songs_to_download"] == 2
        alb_p = prev["albums"][0]
        assert alb_p["action_mode"] == "NEW_ALBUM"
        assert alb_p["destination_folder"] == "Skyfall Anthem"
        assert alb_p["artwork_will_download"] is True

        def fake_dl_mp3(cands, dest_path: Path, song_title: str, on_validating=None, timeout=45):
            if on_validating:
                on_validating()
            dest_path.write_bytes(make_valid_mp3_bytes(tag=song_title))
            return {"audio_specs": ValidationService.validate_mp3_file(dest_path), "used_bitrate": "320 Kbps"}

        def fake_dl_art(url: str, dest_png: Path, album_name: str, timeout=25):
            png_bytes = ValidationService.process_and_convert_to_png(make_valid_jpeg_bytes(), f"{album_name}.jpg")
            dest_png.write_bytes(png_bytes)
            return dest_png

        orch = MusicDataSyncOrchestrator(
            db=db,
            local_root_override=str(seed_repo),
            downloader=fake_dl_mp3,
            artwork_downloader=fake_dl_art,
        )
        res = orch.start_sync_job(
            selections=[{"notification_id": nid}],
            idempotency_key="new-alb-1",
            run_async=False,
        )
        job = res["job"]
        assert job["status"] == STATUS_ADDED
        assert job["result"]["songs_added"] == 2
        assert job["result"]["songs_failed"] == 0
        assert job["result"]["destination_folders"] == ["Skyfall Anthem"]
        assert len(job["result"]["commit_links"]) == 1
        assert "github.com/vamshivamshi9630/MusicData/commit/" in job["result"]["commit_links"][0]["commit_url"]

        # Verify actual files + metadata created in MusicData repository
        new_dir = seed_repo / "Skyfall Anthem"
        assert (new_dir / "Hero Entry Song.mp3").exists()
        assert (new_dir / "Melody Breeze.mp3").exists()
        assert (new_dir / "Skyfall Anthem.png").exists()
        assert (new_dir / "album_info.json").exists()
        assert (seed_repo / "metadata" / "albums" / "S" / "Skyfall Anthem.json").exists()

        # Verify bare remote actually received commit
        ls_remote = subprocess.run(
            ["git", "ls-remote", str(isolated_env["remote"]), "refs/heads/main"],
            capture_output=True, text=True, check=True,
        ).stdout
        head_local = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=seed_repo, capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert head_local in ls_remote

        # Verify notification resolved to COMPLETED and marked read
        updated_notif = db.get_notification_by_id(nid)
        assert updated_notif["resolution_status"] == NotificationResolutionStatus.COMPLETED.value
        assert updated_notif["missing_count"] == 0
        assert updated_notif["is_read"] is True

    def test_02_existing_album_missing_song_preserves_existing_files_and_metadata(self, isolated_env):
        db: LatestReleasesDB = isolated_env["db"]
        seed_repo: Path = isolated_env["seed"]

        orig_track = (seed_repo / "Existing Hits" / "First Track.mp3").read_bytes()
        orig_art = (seed_repo / "Existing Hits" / "Existing Hits.png").read_bytes()
        orig_info = (seed_repo / "Existing Hits" / "album_info.json").read_text(encoding="utf-8")

        nid = seed_notification(
            db,
            album_name="Existing Hits",
            notification_type=NotificationType.MISSING_SONGS,
            language="Telugu",
            songs=[
                {
                    "song_title": "First Track",
                    "track_number": 1,
                    "source_audio_url": "https://naasongs.com.co/dl/first.mp3",
                    "exists_in_catalog": True,
                },
                {
                    "song_title": "Brand New Mass Song",
                    "track_number": 2,
                    "source_audio_url": "https://naasongs.com.co/dl/mass.mp3",
                    "exists_in_catalog": False,
                },
            ],
        )

        art_called = {"count": 0}

        def fake_dl_mp3(cands, dest_path: Path, song_title: str, on_validating=None, timeout=45):
            dest_path.write_bytes(make_valid_mp3_bytes(tag=song_title))
            return {"audio_specs": ValidationService.validate_mp3_file(dest_path), "used_bitrate": "320 Kbps"}

        def fail_if_art_called(*args, **kwargs):
            art_called["count"] += 1
            raise AssertionError("Existing artwork must not be overwritten!")

        orch = MusicDataSyncOrchestrator(
            db=db,
            local_root_override=str(seed_repo),
            downloader=fake_dl_mp3,
            artwork_downloader=fail_if_art_called,
        )
        res = orch.start_sync_job(selections=[{"notification_id": nid}], run_async=False)
        job = res["job"]
        assert job["status"] == STATUS_ADDED
        assert job["result"]["songs_added"] == 1
        assert art_called["count"] == 0

        # Existing assets & metadata strictly preserved
        assert (seed_repo / "Existing Hits" / "First Track.mp3").read_bytes() == orig_track
        assert (seed_repo / "Existing Hits" / "Existing Hits.png").read_bytes() == orig_art
        assert (seed_repo / "Existing Hits" / "album_info.json").read_text(encoding="utf-8") == orig_info
        assert (seed_repo / "Existing Hits" / "Brand New Mass Song.mp3").exists()

    def test_03_duplicate_prevention_catalog_and_binary_hash(self, isolated_env):
        db: LatestReleasesDB = isolated_env["db"]
        seed_repo: Path = isolated_env["seed"]

        # Notification lists a track already matching First Track.mp3 by title and/or binary hash
        nid = seed_notification(
            db,
            album_name="Existing Hits",
            notification_type=NotificationType.NEW_SONG,
            language="Telugu",
            songs=[
                {
                    "song_title": "First Track",
                    "track_number": 1,
                    "source_audio_url": "https://naasongs.com.co/dl/first.mp3",
                    "exists_in_catalog": False,
                },
            ],
        )
        prev = build_add_to_musicdata_preview([nid], db=db, local_root_override=str(seed_repo))
        assert prev["albums"][0]["songs"][0]["already_in_catalog"] is True
        assert prev["albums"][0]["songs"][0]["selectable"] is False

        # Even if client forces selection of that song key, backend marks Skipped (Already Exists) & commits nothing
        head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=seed_repo, capture_output=True, text=True, check=True).stdout.strip()
        orch = MusicDataSyncOrchestrator(db=db, local_root_override=str(seed_repo))
        res = orch.start_sync_job(
            selections=[{"notification_id": nid, "selected_song_keys": [prev["albums"][0]["songs"][0]["song_key"]]}],
            run_async=False,
        )
        assert res["job"]["status"] == STATUS_SKIPPED
        assert res["job"]["result"]["songs_skipped"] == 1
        assert res["job"]["result"]["songs_added"] == 0
        head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=seed_repo, capture_output=True, text=True, check=True).stdout.strip()
        assert head_before == head_after

        # Binary content duplicate guard even under a different title
        nid_bin = seed_notification(
            db,
            album_name="Existing Hits",
            notification_type=NotificationType.NEW_SONG,
            language="Telugu",
            source_url="https://naasongs.com.co/existing-hits-bindup.html",
            songs=[{
                "song_title": "Renamed Twin Track",
                "track_number": 9,
                "source_audio_url": "https://naasongs.com.co/dl/twin.mp3",
                "exists_in_catalog": False,
            }],
        )
        orch_bin = MusicDataSyncOrchestrator(
            db=db,
            local_root_override=str(seed_repo),
            downloader=lambda cands, dest_path, song_title, on_validating=None, timeout=45: (
                dest_path.write_bytes(make_valid_mp3_bytes(tag="existing_first")),
                {"audio_specs": ValidationService.validate_mp3_file(dest_path), "used_bitrate": "320 Kbps"}
            )[1],
        )
        res_bin = orch_bin.start_sync_job(selections=[{"notification_id": nid_bin}], run_async=False)
        assert res_bin["job"]["status"] == STATUS_SKIPPED
        assert res_bin["job"]["result"]["songs_skipped"] == 1
        assert res_bin["job"]["result"]["songs_added"] == 0
        assert not (seed_repo / "Existing Hits" / "Renamed Twin Track.mp3").exists()

    def test_04_wrong_language_match_and_multilingual_disambiguation(self, isolated_env):
        db: LatestReleasesDB = isolated_env["db"]
        seed_repo: Path = isolated_env["seed"]

        # 1. MISSING_SONGS for OM (Tamil) must NOT match OM – Telugu
        nid_tam_missing = seed_notification(
            db,
            album_name="OM",
            notification_type=NotificationType.MISSING_SONGS,
            language="Tamil",
            source_url="https://naasongs.com.co/om-tamil-missing.html",
            songs=[{
                "song_title": "Tamil Song X",
                "track_number": 1,
                "source_audio_url": "https://naasongs.com.co/dl/tamx.mp3",
                "exists_in_catalog": False,
            }],
        )
        prev = build_add_to_musicdata_preview([nid_tam_missing], db=db, local_root_override=str(seed_repo))
        assert prev["albums"][0]["identity_verified"] is False
        assert "OM – Telugu" in (prev["albums"][0]["identity_error"] or "")

        orch = MusicDataSyncOrchestrator(db=db, local_root_override=str(seed_repo))
        with pytest.raises(UploadPipelineError):
            orch.start_sync_job(selections=[{"notification_id": nid_tam_missing}], run_async=False)

        # 2. NEW_ALBUM for OM (Tamil) safely disambiguates folder to "OM – Tamil"
        nid_tam_new = seed_notification(
            db,
            album_name="OM",
            notification_type=NotificationType.NEW_ALBUM,
            language="Tamil",
            source_url="https://naasongs.com.co/om-tamil-new.html",
            songs=[{
                "song_title": "Kollywood Beat",
                "track_number": 1,
                "source_audio_url": "https://naasongs.com.co/dl/kbeat.mp3",
                "exists_in_catalog": False,
            }],
        )
        prev_new = build_add_to_musicdata_preview([nid_tam_new], db=db, local_root_override=str(seed_repo))
        assert prev_new["albums"][0]["identity_verified"] is True
        assert prev_new["albums"][0]["destination_folder"] == "OM – Tamil"

    def test_05_zero_byte_corrupt_html_incomplete_rejection_and_ssrf_guard(self, tmp_path, monkeypatch):
        # Security checks: localhost and private IPs rejected by real validator
        assert is_public_safe_url("http://127.0.0.1/a.mp3") is False
        assert is_public_safe_url("http://192.168.1.5/a.mp3") is False
        assert is_public_safe_url("http://10.0.0.1/a.mp3") is False
        assert is_public_safe_url("file:///etc/passwd") is False
        assert is_public_safe_url("https://naasongs.com.co/song.mp3") is True

        # Start real HTTP server on 127.0.0.1 serving 0-byte, corrupt, HTML, incomplete, and valid MP3
        valid_mp3 = make_valid_mp3_bytes()
        _FixtureHTTPHandler.routes = {
            "/zero.mp3": (200, "audio/mpeg", b"", 0),
            "/corrupt.mp3": (200, "audio/mpeg", b"NOT_AN_MP3_GARBAGE_BYTES_12345", None),
            "/html.mp3": (200, "text/html", b"<!DOCTYPE html><html>404</html>", None),
            "/incomplete.mp3": (200, "audio/mpeg", valid_mp3[:200], len(valid_mp3)),
            "/valid.mp3": (200, "audio/mpeg", valid_mp3, len(valid_mp3)),
        }
        httpd = socketserver.TCPServer(("127.0.0.1", 0), _FixtureHTTPHandler)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()

        try:
            # Allow loopback only for hitting local test fixture HTTP server
            monkeypatch.setattr("backend.services.latest_releases.musicdata_uploader.is_public_safe_url", lambda u: True)
            monkeypatch.setattr("backend.services.latest_releases.album_parser.AlbumParser.is_allowed_download_domain", lambda u: True)

            for bad_path in ["/zero.mp3", "/corrupt.mp3", "/html.mp3", "/incomplete.mp3"]:
                out_f = tmp_path / f"bad_{bad_path.strip('/')}"
                with pytest.raises(UploadPipelineError):
                    download_and_validate_mp3(
                        [{"bitrate": "320 Kbps", "url": f"http://127.0.0.1:{port}{bad_path}"}],
                        out_f,
                        "BadTrack",
                    )
                assert not out_f.exists()

            # Rejects 0-byte image & corrupt artwork too
            with pytest.raises(FileValidationError):
                ValidationService.process_and_convert_to_png(b"", "empty.png")
            with pytest.raises(FileValidationError):
                ValidationService.process_and_convert_to_png(b"bad_bytes", "corrupt.jpg")

            # Valid MP3 passes
            good_f = tmp_path / "good.mp3"
            ok = download_and_validate_mp3(
                [{"bitrate": "320 Kbps", "url": f"http://127.0.0.1:{port}/valid.mp3"}],
                good_f,
                "GoodTrack",
            )
            assert ok["audio_specs"]["valid"] is True
            assert good_f.exists()
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_06_partial_download_failure_and_selective_reconciliation(self, isolated_env):
        db: LatestReleasesDB = isolated_env["db"]
        seed_repo: Path = isolated_env["seed"]

        nid = seed_notification(
            db,
            album_name="Split Release",
            notification_type=NotificationType.NEW_ALBUM,
            language="Telugu",
            songs=[
                {
                    "song_title": "Good Track One",
                    "track_number": 1,
                    "download_links": {"320 Kbps": "https://naasongs.com.co/dl/good1.mp3"},
                    "source_audio_url": "https://naasongs.com.co/dl/good1.mp3",
                    "exists_in_catalog": False,
                },
                {
                    "song_title": "Broken Track Two",
                    "track_number": 2,
                    "download_links": {"320 Kbps": "https://naasongs.com.co/dl/broken2.mp3"},
                    "source_audio_url": "https://naasongs.com.co/dl/broken2.mp3",
                    "exists_in_catalog": False,
                },
            ],
        )

        def partial_dl(cands, dest_path: Path, song_title: str, on_validating=None, timeout=45):
            if "Broken" in song_title:
                raise UploadPipelineError("HTTP 404 downloading 320 Kbps")
            dest_path.write_bytes(make_valid_mp3_bytes())
            return {"audio_specs": ValidationService.validate_mp3_file(dest_path), "used_bitrate": "320 Kbps"}

        def ok_art(url: str, dest_png: Path, album_name: str, timeout=25):
            dest_png.write_bytes(ValidationService.process_and_convert_to_png(make_valid_jpeg_bytes(), "a.jpg"))
            return dest_png

        orch = MusicDataSyncOrchestrator(
            db=db,
            local_root_override=str(seed_repo),
            downloader=partial_dl,
            artwork_downloader=ok_art,
        )
        res = orch.start_sync_job(selections=[{"notification_id": nid}], run_async=False)
        job = res["job"]

        assert job["status"] == STATUS_PARTIALLY_FAILED
        assert job["result"]["songs_added"] == 1
        assert job["result"]["songs_failed"] == 1
        assert job["result"]["retry_available"] is True

        # Only Good Track One marked added; Broken Track Two stays pending!
        notif = db.get_notification_by_id(nid)
        assert notif["resolution_status"] == NotificationResolutionStatus.PARTIALLY_COMPLETED.value
        assert notif["missing_count"] == 1
        by_title = {s["song_title"]: s for s in notif["songs"]}
        assert by_title["Good Track One"]["exists_in_catalog"] is True
        assert by_title["Broken Track Two"]["exists_in_catalog"] is False

    def test_07_and_08_partial_github_failure_and_retry_without_duplicates(self, isolated_env):
        db: LatestReleasesDB = isolated_env["db"]
        seed_repo: Path = isolated_env["seed"]

        nid_a = seed_notification(
            db,
            album_name="Alpha Album",
            notification_type=NotificationType.NEW_ALBUM,
            language="Telugu",
            songs=[{
                "song_title": "Alpha Song",
                "track_number": 1,
                "source_audio_url": "https://naasongs.com.co/dl/alpha.mp3",
                "exists_in_catalog": False,
            }],
        )
        nid_b = seed_notification(
            db,
            album_name="Beta Album",
            notification_type=NotificationType.NEW_ALBUM,
            language="Telugu",
            songs=[{
                "song_title": "Beta Song",
                "track_number": 1,
                "source_audio_url": "https://naasongs.com.co/dl/beta.mp3",
                "exists_in_catalog": False,
            }],
        )

        def ok_mp3(cands, dest_path: Path, song_title: str, on_validating=None, timeout=45):
            dest_path.write_bytes(make_valid_mp3_bytes())
            return {"audio_specs": ValidationService.validate_mp3_file(dest_path), "used_bitrate": "320 Kbps"}

        def ok_art(url: str, dest_png: Path, album_name: str, timeout=25):
            dest_png.write_bytes(ValidationService.process_and_convert_to_png(make_valid_jpeg_bytes(), "a.jpg"))
            return dest_png

        fail_beta = {"active": True}

        class FailingBetaGitSync(BaseGitSyncService):
            def stage_commit_and_push(self, album_name: str, mode: str = "add", custom_commit_msg: str = "", push_enabled: bool = True):
                if album_name == "Beta Album" and fail_beta["active"]:
                    raise GitSyncError("Simulated push failure with x-access-token:ghp_secret123456789@", stage="git_push", exit_code=1)
                return super().stage_commit_and_push(album_name, mode=mode, custom_commit_msg=custom_commit_msg, push_enabled=push_enabled)

        orch = MusicDataSyncOrchestrator(
            db=db,
            local_root_override=str(seed_repo),
            downloader=ok_mp3,
            artwork_downloader=ok_art,
            git_sync_factory=lambda repo: FailingBetaGitSync(repo),
        )

        res1 = orch.start_sync_job(
            selections=[{"notification_id": nid_a}, {"notification_id": nid_b}],
            idempotency_key="multi-1",
            run_async=False,
        )
        job1 = res1["job"]
        assert job1["status"] == STATUS_PARTIALLY_FAILED
        assert job1["result"]["songs_added"] == 1
        assert job1["result"]["songs_failed"] == 1
        # Token must NEVER leak in error message
        assert "ghp_secret" not in str(job1)
        assert "[REDACTED" in str(job1)

        assert db.get_notification_by_id(nid_a)["resolution_status"] == NotificationResolutionStatus.COMPLETED.value
        assert db.get_notification_by_id(nid_b)["resolution_status"] == NotificationResolutionStatus.PENDING.value

        # Now recover push & retry failed job ONLY
        fail_beta["active"] = False
        downloaded_on_retry: List[str] = []

        def tracking_dl(cands, dest_path: Path, song_title: str, on_validating=None, timeout=45):
            downloaded_on_retry.append(song_title)
            return ok_mp3(cands, dest_path, song_title, on_validating, timeout)

        orch.downloader = tracking_dl  # wait: attribute is _downloader
        orch._downloader = tracking_dl

        res_retry = orch.retry_failed_job(parent_job_id=job1["job_id"], idempotency_key="retry-1", run_async=False)
        retry_job = res_retry["job"]
        assert retry_job["parent_job_id"] == job1["job_id"]
        assert retry_job["status"] == STATUS_ADDED
        assert retry_job["result"]["songs_added"] == 1
        assert downloaded_on_retry == ["Beta Song"]  # Alpha Song NOT re-downloaded or re-committed!
        assert db.get_notification_by_id(nid_b)["resolution_status"] == NotificationResolutionStatus.COMPLETED.value

    def test_09_notification_not_resolved_until_repo_verification_succeeds(self, isolated_env):
        db: LatestReleasesDB = isolated_env["db"]
        seed_repo: Path = isolated_env["seed"]

        nid = seed_notification(
            db,
            album_name="Unverified Album",
            notification_type=NotificationType.NEW_ALBUM,
            language="Telugu",
            songs=[{
                "song_title": "Verify Track",
                "track_number": 1,
                "source_audio_url": "https://naasongs.com.co/dl/v.mp3",
                "exists_in_catalog": False,
            }],
        )

        def ok_mp3(cands, dest_path: Path, song_title: str, on_validating=None, timeout=45):
            dest_path.write_bytes(make_valid_mp3_bytes())
            return {"audio_specs": ValidationService.validate_mp3_file(dest_path), "used_bitrate": "320 Kbps"}

        def ok_art(url: str, dest_png: Path, album_name: str, timeout=25):
            dest_png.write_bytes(ValidationService.process_and_convert_to_png(make_valid_jpeg_bytes(), "a.jpg"))
            return dest_png

        orch = MusicDataSyncOrchestrator(
            db=db,
            local_root_override=str(seed_repo),
            downloader=ok_mp3,
            artwork_downloader=ok_art,
            verifier=lambda **kwargs: {"verified": False, "missing_files": ["Verify Track.mp3"], "error": "Remote verification failed"},
        )
        res = orch.start_sync_job(selections=[{"notification_id": nid}], run_async=False)
        assert res["job"]["status"] == STATUS_FAILED

        # Notification MUST stay PENDING and song stays unresolved!
        notif = db.get_notification_by_id(nid)
        assert notif["resolution_status"] == NotificationResolutionStatus.PENDING.value
        assert notif["missing_count"] == 1
        assert notif["songs"][0]["exists_in_catalog"] is False

    def test_10_concurrent_lock_and_idempotency_and_api_endpoints(self, isolated_env, monkeypatch):
        db: LatestReleasesDB = isolated_env["db"]
        monkeypatch.setattr("backend.api.latest_releases_routes.get_latest_releases_db", lambda: db)

        nid = seed_notification(
            db,
            album_name="Concurrent Album",
            notification_type=NotificationType.NEW_ALBUM,
            language="Telugu",
            songs=[{
                "song_title": "Concurrent Song",
                "track_number": 1,
                "source_audio_url": "https://naasongs.com.co/dl/conc.mp3",
                "exists_in_catalog": False,
            }],
        )

        client = TestClient(app)

        # Invalid notification ID rejected cleanly
        bad_prev = client.post("/api/latest-releases/add-to-musicdata/preview", json={"notification_ids": [999999]})
        assert bad_prev.status_code == 400

        # Acquire lock simulating active concurrent job
        orch = MusicDataSyncOrchestrator(db=db, local_root_override=str(isolated_env["seed"]))
        assert db.acquire_named_lock("album_sync:concurrent album", locked_by="other_job", timeout_minutes=15) is True

        try:
            with pytest.raises(UploadPipelineError, match="CONFLICT:"):
                orch.start_sync_job(selections=[{"notification_id": nid}], run_async=False)
        finally:
            db.release_named_lock("album_sync:concurrent album", locked_by="other_job")

        # Test idempotency key deduplication via Orchestrator
        created_job = orch.start_sync_job(
            selections=[{"notification_id": nid}],
            idempotency_key="idem-unique-1",
            run_async=True,
        )
        assert created_job["duplicate_request"] is False
        dup_job = orch.start_sync_job(
            selections=[{"notification_id": nid}],
            idempotency_key="idem-unique-1",
            run_async=True,
        )
        assert dup_job["duplicate_request"] is True
        assert dup_job["job"]["job_id"] == created_job["job"]["job_id"]

    def test_11_github_api_rate_limit_and_catalog_outage_safety(self, isolated_env, monkeypatch):
        # 1. Verify GitHub Contents API HTTP 429/403 rate limit handling & clean error return
        with patch("urllib.request.urlopen") as mock_open, \
             patch.object(cloud_catalog_service, "get_album_metadata", side_effect=CatalogLookupError("unavailable")):
            mock_open.side_effect = urllib.error.HTTPError(
                url="https://api.github.com/repos/x/y/contents/Album",
                code=429,
                msg="Too Many Requests",
                hdrs={"Retry-After": "0"},
                fp=None,
            )
            res = verify_album_files_in_repo("Album", ["Song.mp3"], target_repo_dir=None, max_attempts=2)
            assert res["verified"] is False
            assert "429" in (res.get("error") or "")

        # 2. Verify CloudCatalogService outage raises CatalogLookupError -> API returns 503 safely
        db: LatestReleasesDB = isolated_env["db"]
        nid = seed_notification(
            db,
            album_name="Cloud Test Album",
            notification_type=NotificationType.NEW_ALBUM,
            language="Telugu",
            songs=[{
                "song_title": "Cloud Track",
                "track_number": 1,
                "source_audio_url": "https://naasongs.com.co/dl/ct.mp3",
                "exists_in_catalog": False,
            }],
        )
        monkeypatch.setenv("CLOUD_MODE", "1")
        monkeypatch.delenv("SOURCE_REPO_OVERRIDE", raising=False)
        monkeypatch.setattr("backend.api.latest_releases_routes.get_latest_releases_db", lambda: db)
        client = TestClient(app)
        with patch.object(cloud_catalog_service, "get_albums_index", side_effect=CatalogLookupError("GitHub down", reason="timeout")):
            res_prev = client.post("/api/latest-releases/add-to-musicdata/preview", json={"notification_ids": [nid]})
            assert res_prev.status_code == 503

        # 3. Token sanitization safety
        leaked = "fatal: https://x-access-token:ghp_SuperSecretToken12345@github.com/vamshi/MusicData.git failed Bearer abcdefgh123456"
        clean = sanitize_sensitive_text(leaked)
        assert "ghp_SuperSecretToken12345" not in clean
        assert "abcdefgh123456" not in clean

        # 4. Verify UI markers exist cleanly in frontend/index.html
        html_src = (repo_root / "frontend" / "index.html").read_text(encoding="utf-8")
        assert "ADD TO MUSICDATA" in html_src
        assert "add-musicdata-preview-modal" in html_src
        assert "add-musicdata-progress-modal" in html_src
        assert "notif-selection-bar" in html_src
