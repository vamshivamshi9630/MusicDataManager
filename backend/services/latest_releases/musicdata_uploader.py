import hashlib
import ipaddress
import logging
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.core.config import settings
from backend.core.repository import CloudRepository, IRepositoryProvider, LocalRepository, PathTraversalError
from backend.services.catalog import CatalogLookupError, cloud_catalog_service
from backend.services.duplicate import DuplicateDetectionService
from backend.services.fuzzy_search import normalize_text
from backend.services.generator import GeneratorService
from backend.services.git_sync import BaseGitSyncService, GitSyncError
from backend.services.latest_releases.album_parser import AlbumParser
from backend.services.latest_releases.db import (
    LatestReleasesDB,
    get_latest_releases_db,
    compute_canonical_album_key,
    song_identity_key,
    build_notification_summary_text,
)
from backend.services.latest_releases.models import (
    MatchConfidence,
    NotificationResolutionStatus,
    NotificationType,
    TrackingStatus,
)
from backend.services.metadata import MetadataService
from backend.services.validation import (
    FileValidationError,
    MAX_FILE_SIZE_BYTES,
    MAX_AUDIO_FILE_SIZE_BYTES,
    MAX_IMAGE_FILE_SIZE_BYTES,
    ValidationService,
)

logger = logging.getLogger("musicdata.latest_releases.uploader")

# Canonical UI & backend status labels
STATUS_WAITING = "Waiting"
STATUS_DOWNLOADING = "Downloading"
STATUS_VALIDATING = "Validating"
STATUS_UPLOADING = "Uploading"
STATUS_ADDED = "Added Successfully"
STATUS_PARTIALLY_FAILED = "Partially Failed"
STATUS_FAILED = "Failed"
STATUS_SKIPPED = "Skipped (Already Exists)"

TERMINAL_STATUSES = {
    STATUS_ADDED,
    STATUS_PARTIALLY_FAILED,
    STATUS_FAILED,
    STATUS_SKIPPED,
}

ELIGIBLE_NOTIFICATION_TYPES = {
    NotificationType.NEW_ALBUM.value,
    NotificationType.NEW_SONG.value,
    NotificationType.MISSING_SONGS.value,
}

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://naasongs.com.co/",
}


class UploadPipelineError(Exception):
    """Raised on recoverable validation, catalog, download, or GitHub upload failures."""


def is_cloud_mode() -> bool:
    return os.environ.get("CLOUD_MODE", "").strip().lower() in ("1", "true")


def sanitize_sensitive_text(value: Any) -> str:
    """Redacts GitHub credentials, access tokens, and bearer secrets from logs and messages."""
    text = str(value or "")
    text = re.sub(r"x-access-token:[^@\s/]+@", "x-access-token:[REDACTED]@", text, flags=re.IGNORECASE)
    text = re.sub(r"(https?://)[^:@\s/]+:[^@\s/]+@", r"\1[REDACTED]@", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{8,})\b", "[REDACTED_TOKEN]", text)
    text = re.sub(r"\bBearer\s+[A-Za-z0-9._\-]{8,}\b", "Bearer [REDACTED]", text, flags=re.IGNORECASE)
    return text


def sanitize_filename_component(raw_name: str, default: str = "item") -> str:
    """Removes forbidden path characters and ensures safe single-segment naming."""
    cleaned = re.sub(r'[\\/*?:"<>|]', "", (raw_name or "").replace("\x00", ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(".")
    if not cleaned or ".." in cleaned or "/" in cleaned or "\\" in cleaned or ":" in cleaned:
        raise PathTraversalError(f"Unsafe or invalid filename component: '{raw_name}'")
    return cleaned or default


def sanitize_song_filename(song_title: str) -> str:
    """Produces a safe `<Song Title>.mp3` filename matching MusicData conventions."""
    base = (song_title or "").strip()
    if base.lower().endswith(".mp3"):
        base = base[:-4].strip()
    safe_stem = sanitize_filename_component(base, default="Track")
    return f"{safe_stem}.mp3"


def compute_song_key(notification_id: int, track_number: int, song_title: str) -> str:
    """Deterministic server-side key uniquely identifying a song entry inside a notification."""
    norm = normalize_text(song_title) or f"track_{track_number}"
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:10]
    return f"n{int(notification_id)}_t{int(track_number or 1)}_{digest}"


def is_public_safe_url(url: str) -> bool:
    """Validates HTTP/HTTPS scheme and rejects localhost, loopback, or private network hosts."""
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urllib.parse.urlparse(url.strip())
    except Exception:
        return False
    if parsed.scheme.lower() not in ("http", "https"):
        return False
    hostname = (parsed.hostname or "").lower().strip(".")
    if not hostname or hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return False
    try:
        ip_obj = ipaddress.ip_address(hostname)
        if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local or ip_obj.is_reserved or ip_obj.is_multicast:
            return False
    except ValueError:
        if hostname.startswith("192.168.") or hostname.startswith("10.") or hostname.startswith("172."):
            return False
    return True


def _extract_server_github_token() -> str:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("AGENT_AUTH_TOKEN") or ""
    if not token:
        try:
            from backend.services.github_auth import GitHubTokenManager
            candidate = GitHubTokenManager().get_installation_access_token()
            if candidate and not str(candidate).startswith("ghs_mock"):
                token = candidate
        except Exception:
            token = ""
    return str(token or "").strip()


def _extract_candidate_audio_urls(song: Dict[str, Any]) -> List[Tuple[str, str]]:
    """
    Returns ordered list of `(bitrate_label, url)` from highest quality to fallback,
    filtered strictly to authorized download domains.
    """
    candidates: List[Tuple[str, str]] = []
    seen_urls = set()

    dl_links = song.get("download_links")
    if isinstance(dl_links, dict) and dl_links:
        def _rank(label: str) -> int:
            m = re.match(r"^\s*(\d+)", str(label or ""))
            return int(m.group(1)) if m else 1

        for bitrate_lbl, raw_u in sorted(dl_links.items(), key=lambda kv: _rank(kv[0]), reverse=True):
            u = str(raw_u or "").strip()
            if u and u not in seen_urls and AlbumParser.is_allowed_download_domain(u) and is_public_safe_url(u):
                seen_urls.add(u)
                candidates.append((str(bitrate_lbl), u))

    primary = str(song.get("source_audio_url") or "").strip()
    if primary and primary not in seen_urls and AlbumParser.is_allowed_download_domain(primary) and is_public_safe_url(primary):
        seen_urls.add(primary)
        candidates.append((str(song.get("available_bitrate") or "Standard"), primary))

    return candidates


def _resolve_album_catalog_language(cat_album: Dict[str, Any]) -> Optional[str]:
    _, emb_lang = cloud_catalog_service.strip_language_and_meta(cat_album.get("name", ""))
    raw_lang = emb_lang or cat_album.get("language")
    if not raw_lang or str(raw_lang).strip().lower() in ("unknown", ""):
        return None
    return str(raw_lang).strip()


def _load_authoritative_catalog_state(
    local_root_override: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Callable[[Dict[str, Any]], List[Dict[str, Any]]]]:
    """
    Loads current authoritative MusicData albums and returns `(albums_index, songs_fetcher)`.
    In Cloud Mode, fetches fresh index from GitHub (never falls back to empty catalog).
    In Local Mode (without cloud override), reads local repository directories directly.
    """
    source_override = local_root_override or os.environ.get("SOURCE_REPO_OVERRIDE")
    if is_cloud_mode() and not source_override:
        albums_idx = cloud_catalog_service.get_albums_index(force_refresh=True, allow_stale_fallback=False)

        def _fetch_cloud_songs(cat_alb: Dict[str, Any]) -> List[Dict[str, Any]]:
            meta = cloud_catalog_service.get_album_metadata(cat_alb, bypass_cache=True)
            return meta.get("songs", [])

        return albums_idx, _fetch_cloud_songs

    repo = LocalRepository(root_override=source_override) if source_override else LocalRepository()
    ms = MetadataService(repo)
    albums_idx: List[Dict[str, Any]] = []
    for d in repo.list_all_album_directories():
        info = ms.load_album_info(d.name)
        has_img = (d / f"{d.name}.png").exists()
        mp3_count = len(list(d.glob("*.mp3")))
        albums_idx.append({
            "name": d.name,
            "artist": info.get("musicDirector", "Unknown"),
            "musicDirector": info.get("musicDirector", "Unknown"),
            "year": info.get("year", 2026),
            "language": info.get("language", "Telugu"),
            "image": f"{d.name}.png" if has_img else None,
            "songCount": mp3_count,
            "partition": repo.get_partition(d.name),
        })

    def _fetch_local_songs(cat_alb: Dict[str, Any]) -> List[Dict[str, Any]]:
        alb_name = cat_alb.get("name", "")
        alb_dir = repo.get_album_path(alb_name)
        songs = []
        if alb_dir.exists():
            for mp3 in sorted(alb_dir.glob("*.mp3"), key=lambda p: p.name.lower()):
                songs.append({
                    "title": mp3.stem,
                    "audio": mp3.name,
                    "filename": mp3.name,
                    "id": repo.get_stable_id(f"{alb_name}{mp3.name}"),
                })
        return songs

    return albums_idx, _fetch_local_songs


def _resolve_album_destination_plan(
    notif: Dict[str, Any],
    catalog_albums: List[Dict[str, Any]],
    songs_fetcher: Callable[[Dict[str, Any]], List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """
    Server-side resolution of canonical album identity, language compatibility,
    safe destination folder name, existing songs, and artwork disposition.
    """
    notif_id = int(notif["id"])
    notif_type = str(notif.get("notification_type") or "")
    raw_album_name = str(notif.get("album_name") or "").strip()
    notif_lang = str(notif.get("language") or "Telugu").strip()
    notif_year = notif.get("year") or 2026
    known_lang = notif_lang if notif_lang.lower() not in ("", "unknown") else None

    clean_base, emb_lang = cloud_catalog_service.strip_language_and_meta(raw_album_name)
    effective_lang = known_lang or emb_lang

    catalog_match = cloud_catalog_service.find_canonical_album(
        query_name=raw_album_name,
        query_language=effective_lang,
        query_year=notif_year,
        candidate_albums=catalog_albums,
    )

    # Fallback exact base-title scan across catalog to detect language conflicts / ambiguities
    norm_clean_target = re.sub(r"[^a-z0-9]", "", (clean_base or raw_album_name).lower())
    same_base_catalog = []
    for alb in catalog_albums:
        c_base, _ = cloud_catalog_service.strip_language_and_meta(alb.get("name", ""))
        if re.sub(r"[^a-z0-9]", "", c_base.lower()) == norm_clean_target and norm_clean_target:
            same_base_catalog.append(alb)

    identity_verified = True
    identity_error: Optional[str] = None
    action_mode = "NEW_ALBUM"
    destination_folder = ""
    catalog_songs: List[Dict[str, Any]] = []
    has_existing_artwork = False

    if catalog_match is not None:
        cat_lang = _resolve_album_catalog_language(catalog_match)
        if effective_lang and cat_lang and effective_lang.lower() != cat_lang.lower():
            identity_verified = False
            identity_error = (
                f"Language mismatch: matched catalog folder '{catalog_match.get('name')}' is '{cat_lang}', "
                f"but notification '{raw_album_name}' is '{effective_lang}'."
            )
        else:
            destination_folder = sanitize_filename_component(catalog_match["name"])
            action_mode = "EXISTING_ALBUM"
            has_existing_artwork = bool(catalog_match.get("image"))
            catalog_songs = songs_fetcher(catalog_match)
    else:
        # Notification expects an existing album, yet canonical match failed
        if notif_type in (NotificationType.NEW_SONG.value, NotificationType.MISSING_SONGS.value):
            identity_verified = False
            if same_base_catalog:
                avail_langs = ", ".join(
                    f"'{a.get('name')}' ({_resolve_album_catalog_language(a) or 'Unknown'})"
                    for a in same_base_catalog
                )
                identity_error = (
                    f"Wrong language or ambiguous target for '{raw_album_name}' ({effective_lang or 'Unknown'}): "
                    f"existing MusicData entries are {avail_langs}."
                )
            else:
                identity_error = (
                    f"Existing MusicData album folder not found for '{raw_album_name}' ({effective_lang or 'Unknown'})."
                )
        else:
            # NEW_ALBUM path
            if same_base_catalog:
                if not effective_lang:
                    identity_verified = False
                    identity_error = (
                        f"Ambiguous language for '{raw_album_name}': an album with the same title already exists "
                        "in MusicData. Confirm language before adding."
                    )
                else:
                    # Disambiguate multilingual title using MusicData convention "<CleanTitle> – <Language>"
                    candidate_folder = sanitize_filename_component(f"{clean_base} – {effective_lang}")
                    if any(a.get("name", "").strip().lower() == candidate_folder.lower() for a in catalog_albums):
                        identity_verified = False
                        identity_error = f"Destination folder '{candidate_folder}' already exists in MusicData."
                    else:
                        destination_folder = candidate_folder
            else:
                try:
                    destination_folder = sanitize_filename_component(raw_album_name)
                except PathTraversalError as pe:
                    identity_verified = False
                    identity_error = str(pe)

    if destination_folder and action_mode == "NEW_ALBUM":
        # Guard against directory name collision ignoring case
        for a in catalog_albums:
            if a.get("name", "").strip().lower() == destination_folder.lower():
                destination_folder = sanitize_filename_component(a["name"])
                action_mode = "EXISTING_ALBUM"
                has_existing_artwork = bool(a.get("image"))
                catalog_songs = songs_fetcher(a)
                break

    existing_filenames_lower = {
        str(s.get("audio") or s.get("filename") or f"{s.get('title', '')}.mp3").strip().lower()
        for s in catalog_songs
        if s.get("audio") or s.get("filename") or s.get("title")
    }

    song_entries = []
    raw_songs = notif.get("songs") or []
    for idx, s in enumerate(raw_songs):
        song_title = str(s.get("song_title") or "").strip()
        track_num = int(s.get("track_number") or (idx + 1))
        s_key = compute_song_key(notif_id, track_num, song_title)
        try:
            target_filename = sanitize_song_filename(song_title)
        except PathTraversalError:
            target_filename = f"Track_{track_num:02d}.mp3"

        candidate_urls = _extract_candidate_audio_urls(s)
        already_in_catalog = False
        matched_cat_title = s.get("catalog_song_title")
        match_cls = s.get("match_classification") or "MISSING"

        if catalog_songs and song_title:
            match_res = cloud_catalog_service.match_song_detailed(
                source_title=song_title,
                catalog_songs=catalog_songs,
                source_singer=s.get("singer"),
                source_filename=target_filename,
            )
            best_cat = match_res.get("matched_song")
            match_cls = match_res.get("classification") or match_cls
            if match_res.get("exists_in_catalog") or target_filename.lower() in existing_filenames_lower:
                already_in_catalog = True
                matched_cat_title = best_cat.get("title") if best_cat else target_filename
        elif target_filename.lower() in existing_filenames_lower:
            already_in_catalog = True

        has_valid_source = len(candidate_urls) > 0
        selectable = identity_verified and not already_in_catalog and has_valid_source
        is_uncertain = match_cls in ("POSSIBLE_VERSION_MATCH", "UNCERTAIN")

        source_hosts = []
        bitrates_found = []
        for b_lbl, u in candidate_urls:
            bitrates_found.append(b_lbl)
            host = urllib.parse.urlparse(u).hostname or "source"
            if host not in source_hosts:
                source_hosts.append(host)

        song_entries.append({
            "song_key": s_key,
            "song_id": s.get("id"),
            "song_title": song_title,
            "track_number": track_num,
            "singer": s.get("singer"),
            "target_filename": target_filename,
            "available_bitrate": s.get("available_bitrate") or (", ".join(bitrates_found) if bitrates_found else None),
            "available_bitrates": bitrates_found or list(s.get("available_bitrates") or []),
            "candidate_urls": [{"bitrate": b, "url": u} for b, u in candidate_urls],
            "download_source": ", ".join(source_hosts) if source_hosts else (urllib.parse.urlparse(str(notif.get("source_url") or "")).hostname or "Unavailable"),
            "already_in_catalog": already_in_catalog,
            "catalog_song_title": matched_cat_title,
            "match_classification": match_cls,
            "selectable": selectable,
            "default_selected": bool(selectable and not is_uncertain),
            "warning": (
                f"Version/Uncertain match against catalog track '{matched_cat_title}'"
                if is_uncertain and not already_in_catalog and matched_cat_title
                else (None if has_valid_source or already_in_catalog else "No authorized MP3 download URL available")
            ),
        })

    art_url = str(notif.get("artwork_url") or "").strip() or None
    art_valid = bool(art_url and is_public_safe_url(art_url))
    artwork_will_download = bool(
        identity_verified and art_valid and (action_mode == "NEW_ALBUM" or not has_existing_artwork)
    )

    return {
        "notification_id": notif_id,
        "album_name": raw_album_name,
        "language": effective_lang or "Telugu",
        "notification_type": notif_type,
        "music_director": notif.get("music_director") or "Unknown",
        "year": notif_year,
        "source_url": notif.get("source_url") or "",
        "artwork_url": art_url,
        "artwork_will_download": artwork_will_download,
        "has_existing_artwork": has_existing_artwork,
        "action_mode": action_mode,
        "is_new_album": action_mode == "NEW_ALBUM",
        "destination_folder": destination_folder,
        "identity_verified": identity_verified,
        "identity_error": identity_error,
        "songs": song_entries,
        "downloadable_count": sum(1 for s in song_entries if s["default_selected"]),
        "selectable_count": sum(1 for s in song_entries if s["selectable"]),
        "already_existing_count": sum(1 for s in song_entries if s["already_in_catalog"]),
    }


def build_add_to_musicdata_preview(
    notification_ids: List[int],
    db: Optional[LatestReleasesDB] = None,
    local_root_override: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Validates requested notification IDs, inspects the live authoritative MusicData catalog,
    verifies language/album identity, and returns structured preview details for user confirmation.
    """
    db = db or get_latest_releases_db()
    if not notification_ids:
        raise UploadPipelineError("Please select at least one notification to add to MusicData.")

    clean_ids = []
    seen = set()
    for nid in notification_ids:
        try:
            int_id = int(nid)
        except (TypeError, ValueError):
            raise UploadPipelineError(f"Invalid notification ID: {nid!r}")
        if int_id <= 0:
            raise UploadPipelineError(f"Invalid notification ID: {int_id}")
        if int_id not in seen:
            seen.add(int_id)
            clean_ids.append(int_id)

    notifications = db.get_notifications_by_ids(clean_ids)
    found_ids = {int(n["id"]) for n in notifications}
    missing_ids = [nid for nid in clean_ids if nid not in found_ids]
    if missing_ids:
        raise UploadPipelineError(f"Notification IDs not found: {missing_ids}")

    # Verify eligibility BEFORE proceeding
    for n in notifications:
        n_type = str(n.get("notification_type") or "")
        if n_type not in ELIGIBLE_NOTIFICATION_TYPES:
            raise UploadPipelineError(
                f"Notification #{n['id']} ('{n.get('album_name')}') has type '{n_type}', which cannot be added directly."
            )

    catalog_albums, songs_fetcher = _load_authoritative_catalog_state(local_root_override=local_root_override)

    albums_preview: List[Dict[str, Any]] = []
    total_songs_to_download = 0
    total_already_existing = 0
    total_uncertain_tracks = 0
    total_unavailable_tracks = 0
    estimated_bytes = 0

    for notif in notifications:
        plan = _resolve_album_destination_plan(notif, catalog_albums, songs_fetcher)
        albums_preview.append(plan)
        total_songs_to_download += plan["downloadable_count"]
        total_already_existing += plan["already_existing_count"]

        if plan.get("artwork_will_download"):
            estimated_bytes += 1_500_000  # ~1.5 MB for converted PNG artwork

        for s in plan.get("songs", []):
            if s.get("already_in_catalog"):
                continue
            if s.get("match_classification") in ("POSSIBLE_VERSION_MATCH", "UNCERTAIN"):
                total_uncertain_tracks += 1
            if not s.get("candidate_urls"):
                total_unavailable_tracks += 1
            if s.get("selectable"):
                c_urls = s.get("candidate_urls", [])
                bitrates = [str(c.get("bitrate", "")).lower() for c in c_urls]
                if any("320" in b for b in bitrates):
                    estimated_bytes += 9_500_000
                elif any("256" in b for b in bitrates):
                    estimated_bytes += 7_500_000
                elif any("128" in b for b in bitrates):
                    estimated_bytes += 4_000_000
                else:
                    estimated_bytes += 6_000_000

    estimated_upload_size_mb = round(estimated_bytes / (1024 * 1024), 1)

    return {
        "success": True,
        "target_repository": settings.musicdata_repo_slug,
        "target_branch": settings.GITHUB_BRANCH,
        "albums": albums_preview,
        "summary": {
            "total_albums": len(albums_preview),
            "eligible_albums": sum(1 for a in albums_preview if a["identity_verified"] and a["selectable_count"] > 0),
            "total_songs_to_download": total_songs_to_download,
            "total_already_existing": total_already_existing,
            "total_uncertain_tracks": total_uncertain_tracks,
            "total_unavailable_tracks": total_unavailable_tracks,
            "estimated_upload_size_mb": estimated_upload_size_mb,
            "blocked_albums": sum(1 for a in albums_preview if not a["identity_verified"]),
        },
    }


def download_and_validate_mp3(
    candidate_urls: List[Dict[str, str]],
    dest_path: Path,
    song_title: str,
    on_validating: Optional[Callable[[], None]] = None,
    timeout: int = 45,
) -> Dict[str, Any]:
    """
    Downloads an MP3 from approved candidate URLs into staging `dest_path`, validates
    completeness, non-HTML payload, 0-byte shield, and MPEG audio integrity via ValidationService.
    Uses standard TLS CA verification (never disables SSL verification).
    """
    if not candidate_urls:
        raise UploadPipelineError(f"No authorized download link found for '{song_title}'.")

    last_error = "Download failed"
    for cand in candidate_urls:
        bitrate = str(cand.get("bitrate") or "Standard")
        url = str(cand.get("url") or "").strip()

        if not AlbumParser.is_allowed_download_domain(url) or not is_public_safe_url(url):
            last_error = f"Blocked non-approved source domain for '{song_title}'."
            continue

        if dest_path.exists():
            dest_path.unlink(missing_ok=True)

        req = urllib.request.Request(url, headers=DEFAULT_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                final_url = resp.geturl() or url
                if not is_public_safe_url(final_url):
                    raise UploadPipelineError(f"Redirect target rejected as unsafe: {final_url}")

                ctype = (resp.headers.get("Content-Type") or "").lower()
                if "text/html" in ctype or "application/xhtml" in ctype:
                    raise UploadPipelineError(f"Source returned HTML instead of MP3 audio ({bitrate}).")

                content_len_hdr = resp.headers.get("Content-Length")
                expected_len = int(content_len_hdr) if content_len_hdr and content_len_hdr.isdigit() else None
                if expected_len == 0:
                    raise FileValidationError(f"Zero-Byte Shield Triggered: remote file reported 0 bytes ({bitrate}).")
                if expected_len is not None and expected_len > MAX_AUDIO_FILE_SIZE_BYTES:
                    raise FileValidationError(f"Remote file exceeds 95 MB limit ({expected_len} bytes).")

                bytes_written = 0
                first_chunk_checked = False
                with open(dest_path, "wb") as out_f:
                    while True:
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        if not first_chunk_checked:
                            first_chunk_checked = True
                            lead = chunk[:64].lstrip().lower()
                            if lead.startswith(b"<!doctype html") or lead.startswith(b"<html"):
                                raise UploadPipelineError(f"Source returned HTML error document instead of MP3 audio ({bitrate}).")
                        bytes_written += len(chunk)
                        if bytes_written > MAX_AUDIO_FILE_SIZE_BYTES:
                            raise FileValidationError(f"Downloaded stream exceeds 95 MB safety limit for '{song_title}'.")
                        out_f.write(chunk)

                if expected_len is not None and bytes_written < expected_len:
                    raise UploadPipelineError(
                        f"Incomplete download for '{song_title}': received {bytes_written} of {expected_len} bytes."
                    )

            if on_validating:
                on_validating()

            audio_specs = ValidationService.validate_mp3_file(dest_path)
            return {
                "audio_specs": audio_specs,
                "used_bitrate": bitrate,
                "source_host": urllib.parse.urlparse(url).hostname or "source",
            }
        except (FileValidationError, UploadPipelineError) as ve:
            last_error = sanitize_sensitive_text(ve)
            dest_path.unlink(missing_ok=True)
        except urllib.error.HTTPError as he:
            last_error = f"HTTP {he.code} downloading {bitrate} for '{song_title}'"
            dest_path.unlink(missing_ok=True)
        except Exception as e:
            last_error = f"{bitrate} download error: {sanitize_sensitive_text(e)}"
            dest_path.unlink(missing_ok=True)

    raise UploadPipelineError(last_error)


def download_and_validate_artwork(artwork_url: str, dest_png_path: Path, album_name: str, timeout: int = 25) -> Path:
    """Downloads album artwork securely and converts/validates PNG output."""
    if not is_public_safe_url(artwork_url):
        raise UploadPipelineError(f"Unsafe or invalid artwork URL for '{album_name}'.")

    req = urllib.request.Request(artwork_url, headers=DEFAULT_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            final_url = resp.geturl() or artwork_url
            if not is_public_safe_url(final_url):
                raise UploadPipelineError("Artwork redirect target rejected as unsafe.")
            raw = resp.read(MAX_IMAGE_FILE_SIZE_BYTES + 1)
    except Exception as e:
        raise UploadPipelineError(f"Artwork download failed: {sanitize_sensitive_text(e)}")

    if len(raw) > MAX_IMAGE_FILE_SIZE_BYTES:
        raise FileValidationError(f"Artwork for '{album_name}' exceeds 15 MB size limit.")

    png_bytes = ValidationService.process_and_convert_to_png(raw, f"{album_name}.png")
    dest_png_path.write_bytes(png_bytes)
    return dest_png_path


def verify_album_files_in_repo(
    album_folder: str,
    expected_filenames: List[str],
    commit_sha: Optional[str] = None,
    target_repo_dir: Optional[Path] = None,
    max_attempts: int = 3,
) -> Dict[str, Any]:
    """
    Verifies that every uploaded file actually exists (and is non-empty) in the MusicData
    repository before notification entries are marked resolved.
    Handles GitHub API rate limits and transient errors safely.
    """
    if not expected_filenames:
        return {"verified": True, "verified_files": [], "missing_files": []}

    # Local or test-override repo verification
    if target_repo_dir is not None and target_repo_dir.exists():
        alb_dir = target_repo_dir / album_folder
        verified = []
        missing = []
        for fname in expected_filenames:
            fpath = alb_dir / fname
            if fpath.exists() and fpath.is_file() and fpath.stat().st_size > 0:
                verified.append(fname)
            else:
                missing.append(fname)
        return {
            "verified": len(missing) == 0,
            "verified_files": verified,
            "missing_files": missing,
        }

    # Cloud Mode: Query GitHub Contents API for authoritative repository state
    owner = settings.GITHUB_OWNER
    repo = settings.MUSICDATA_GITHUB_REPOSITORY
    ref = commit_sha or settings.GITHUB_BRANCH
    quoted_folder = urllib.parse.quote(album_folder, safe="")
    api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{quoted_folder}?ref={urllib.parse.quote(str(ref), safe='')}"

    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "MusicData-Manager-Cloud",
    }
    token = _extract_server_github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    last_err = None
    for attempt in range(max_attempts):
        try:
            req = urllib.request.Request(api_url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                import json
                entries = json.loads(resp.read().decode("utf-8"))
                if not isinstance(entries, list):
                    last_err = "Unexpected GitHub Contents API response format."
                    continue
                remote_sizes = {
                    str(item.get("name") or ""): int(item.get("size") or 0)
                    for item in entries
                    if isinstance(item, dict)
                }
                verified = [f for f in expected_filenames if remote_sizes.get(f, 0) > 0]
                missing = [f for f in expected_filenames if remote_sizes.get(f, 0) <= 0]
                return {
                    "verified": len(missing) == 0,
                    "verified_files": verified,
                    "missing_files": missing,
                }
        except urllib.error.HTTPError as he:
            if he.code in (403, 429):
                retry_after = he.headers.get("Retry-After") if he.headers else None
                wait_s = min(5.0, float(retry_after)) if retry_after and str(retry_after).isdigit() else (1.0 * (attempt + 1))
                last_err = f"GitHub API rate limit / authorization response (HTTP {he.code})."
                if attempt < max_attempts - 1:
                    time.sleep(wait_s)
                    continue
            elif he.code in (500, 502, 503, 504):
                last_err = f"GitHub API server error (HTTP {he.code})."
                if attempt < max_attempts - 1:
                    time.sleep(1.0 * (attempt + 1))
                    continue
            else:
                last_err = f"GitHub API verification failed (HTTP {he.code})."
                break
        except Exception as e:
            last_err = sanitize_sensitive_text(e)
            if attempt < max_attempts - 1:
                time.sleep(0.5 * (attempt + 1))

    # Fallback check against authoritative catalog album JSON if Contents API hit unauthenticated rate limit
    try:
        cat_meta = cloud_catalog_service.get_album_metadata({"name": album_folder}, bypass_cache=True)
        cat_files = {
            str(s.get("audio") or f"{s.get('title', '')}.mp3"): int(s.get("fileSize") or 1)
            for s in cat_meta.get("songs", [])
        }
        img_name = (cat_meta.get("album") or {}).get("image")
        if img_name:
            cat_files[str(img_name)] = 1
        verified = [f for f in expected_filenames if cat_files.get(f, 0) > 0]
        missing = [f for f in expected_filenames if cat_files.get(f, 0) <= 0]
        if verified and not missing:
            return {"verified": True, "verified_files": verified, "missing_files": []}
    except Exception:
        pass

    return {
        "verified": False,
        "verified_files": [],
        "missing_files": list(expected_filenames),
        "error": last_err or "Could not verify uploaded files on GitHub.",
    }


def selective_reconcile_notification(
    db: LatestReleasesDB,
    notification_id: int,
    verified_added_song_keys: List[str],
    verified_added_filenames: List[str],
) -> Dict[str, Any]:
    """
    Marks ONLY successfully uploaded and verified songs as resolved (`exists_in_catalog=True`),
    upgrades stale NEW_ALBUM types to NEW_SONG/MISSING_SONGS when remaining tracks exist,
    auto-completes when 0 missing remain, and reconciles duplicate active sibling notifications
    for the same canonical `(album, language)` identity.
    """
    notif = db.get_notification_by_id(notification_id)
    if not notif:
        return {"updated": False, "reason": "Notification not found"}

    songs = list(notif.get("songs") or [])
    verified_key_set = set(verified_added_song_keys or [])
    verified_file_set = {f.lower() for f in (verified_added_filenames or [])}
    canon_key, clean_display, norm_lang = compute_canonical_album_key(
        notif.get("album_name"), notif.get("language")
    )

    updated_count = 0
    verified_identities = set()
    for idx, song in enumerate(songs):
        title = str(song.get("song_title") or "").strip()
        if song.get("exists_in_catalog"):
            if title:
                verified_identities.add(song_identity_key(title))
            continue
        track_num = int(song.get("track_number") or (idx + 1))
        s_key = compute_song_key(notification_id, track_num, title)
        try:
            target_file = sanitize_song_filename(title).lower()
        except PathTraversalError:
            target_file = ""

        if s_key in verified_key_set or (target_file and target_file in verified_file_set):
            song["exists_in_catalog"] = True
            song["catalog_song_title"] = title
            song["match_confidence"] = MatchConfidence.EXACT.value
            song["match_classification"] = "EXACT_MATCH"
            song["match_score"] = 1.0
            song["match_reason"] = "Added directly to MusicData and verified"
            song["tracking_status"] = TrackingStatus.ADDED_TO_MUSICDATA.value
            updated_count += 1
            if title:
                verified_identities.add(song_identity_key(title))
            if song.get("id"):
                try:
                    db.update_song_tracking_status(int(song["id"]), TrackingStatus.ADDED_TO_MUSICDATA)
                except Exception:
                    pass

    remaining_missing = sum(1 for s in songs if not s.get("exists_in_catalog"))
    if remaining_missing == 0 and len(songs) > 0:
        new_status = NotificationResolutionStatus.COMPLETED.value
    elif updated_count > 0 or any(s.get("exists_in_catalog") for s in songs):
        new_status = NotificationResolutionStatus.PARTIALLY_COMPLETED.value
    else:
        new_status = notif.get("resolution_status") or NotificationResolutionStatus.PENDING.value

    orig_type_str = notif.get("notification_type") or NotificationType.MISSING_SONGS.value
    if remaining_missing == 1:
        eff_type = NotificationType.NEW_SONG
    elif remaining_missing > 1 and (updated_count > 0 or any(s.get("exists_in_catalog") for s in songs)):
        eff_type = NotificationType.MISSING_SONGS
    else:
        try:
            eff_type = NotificationType(orig_type_str)
        except Exception:
            eff_type = NotificationType.MISSING_SONGS

    new_title, new_message = build_notification_summary_text(
        eff_type,
        clean_display,
        norm_lang,
        songs,
        music_director=notif.get("music_director") or "Unknown",
        year=notif.get("year") or 2026,
    )

    db.update_notification_resolution(
        notification_id=notification_id,
        status=new_status,
        updated_songs=songs,
        missing_count=remaining_missing,
        notification_type=eff_type.value if (updated_count > 0 or remaining_missing > 0 and any(s.get("exists_in_catalog") for s in songs)) else None,
        title=new_title if (updated_count > 0 or remaining_missing == 0 or any(s.get("exists_in_catalog") for s in songs)) else None,
        message=new_message if (updated_count > 0 or remaining_missing == 0 or any(s.get("exists_in_catalog") for s in songs)) else None,
        language=norm_lang,
        canonical_album_key=canon_key,
        is_read=True if new_status == NotificationResolutionStatus.COMPLETED.value else None,
    )
    if new_status == NotificationResolutionStatus.COMPLETED.value:
        db.mark_notification_read(notification_id)

    # Reconcile any duplicate active notifications referencing the same canonical album & songs
    sibling_reconciled = 0
    if updated_count > 0 or verified_file_set:
        primary_missing_identities = {
            song_identity_key(s.get("song_title"))
            for s in songs
            if not s.get("exists_in_catalog") and s.get("song_title")
        }
        try:
            active_siblings = db.get_active_notifications(limit=200)
        except Exception:
            active_siblings = []

        for sib in active_siblings:
            sib_id = sib.get("id")
            if not sib_id or sib_id == notification_id:
                continue
            sib_key, sib_display, sib_lang = compute_canonical_album_key(
                sib.get("album_name"), sib.get("language")
            )
            if sib_key != canon_key:
                continue

            sib_songs = list(sib.get("songs") or [])
            sib_updated = 0
            for s in sib_songs:
                if s.get("exists_in_catalog"):
                    continue
                s_title = str(s.get("song_title") or "").strip()
                try:
                    t_file = sanitize_song_filename(s_title).lower()
                except PathTraversalError:
                    t_file = ""
                if (t_file and t_file in verified_file_set) or (
                    s_title and song_identity_key(s_title) in verified_identities
                ):
                    s["exists_in_catalog"] = True
                    s["catalog_song_title"] = s_title
                    s["match_confidence"] = MatchConfidence.EXACT.value
                    s["match_classification"] = "EXACT_MATCH"
                    s["match_score"] = 1.0
                    s["match_reason"] = "Added directly to MusicData and verified"
                    s["tracking_status"] = TrackingStatus.ADDED_TO_MUSICDATA.value
                    sib_updated += 1

            sib_missing = [s for s in sib_songs if not s.get("exists_in_catalog")]
            sib_missing_ids = {
                song_identity_key(s.get("song_title"))
                for s in sib_missing
                if s.get("song_title")
            }
            all_sib_missing_covered_by_primary = (
                new_status != NotificationResolutionStatus.COMPLETED.value
                and bool(sib_missing_ids)
                and sib_missing_ids.issubset(primary_missing_identities)
            )
            if (len(sib_missing) == 0 and len(sib_songs) > 0) or all_sib_missing_covered_by_primary:
                db.update_notification_resolution(
                    notification_id=sib_id,
                    status=NotificationResolutionStatus.COMPLETED.value,
                    updated_songs=sib_songs,
                    missing_count=0,
                    language=sib_lang,
                    canonical_album_key=canon_key,
                    is_read=True,
                )
                db.mark_notification_read(sib_id)
                sibling_reconciled += 1
            elif sib_updated > 0:
                sib_eff_type = NotificationType.NEW_SONG if len(sib_missing) <= 1 else NotificationType.MISSING_SONGS
                sib_title, sib_msg = build_notification_summary_text(
                    sib_eff_type,
                    sib_display,
                    sib_lang,
                    sib_songs,
                    music_director=sib.get("music_director") or "Unknown",
                    year=sib.get("year") or 2026,
                )
                db.update_notification_resolution(
                    notification_id=sib_id,
                    status=NotificationResolutionStatus.PARTIALLY_COMPLETED.value,
                    updated_songs=sib_songs,
                    missing_count=len(sib_missing),
                    notification_type=sib_eff_type.value,
                    title=sib_title,
                    message=sib_msg,
                    language=sib_lang,
                    canonical_album_key=canon_key,
                )
                sibling_reconciled += 1

    return {
        "updated": True,
        "resolved_songs_count": updated_count,
        "remaining_missing_count": remaining_missing,
        "resolution_status": new_status,
        "sibling_reconciled_count": sibling_reconciled,
    }


class MusicDataSyncOrchestrator:
    """
    Coordinates end-to-end "Add Selected Notifications to MusicData" workflow:
    validation, idempotency, distributed album locks, staged downloads, validation,
    GitHub repository sync, verification, and selective notification reconciliation.
    """

    def __init__(
        self,
        db: Optional[LatestReleasesDB] = None,
        local_root_override: Optional[str] = None,
        downloader: Optional[Callable[..., Dict[str, Any]]] = None,
        artwork_downloader: Optional[Callable[..., Path]] = None,
        verifier: Optional[Callable[..., Dict[str, Any]]] = None,
        repo_factory: Optional[Callable[..., IRepositoryProvider]] = None,
        git_sync_factory: Optional[Callable[[IRepositoryProvider], BaseGitSyncService]] = None,
        generator_factory: Optional[Callable[[IRepositoryProvider], GeneratorService]] = None,
        push_enabled: Optional[bool] = None,
    ):
        self.db = db or get_latest_releases_db()
        self.local_root_override = local_root_override
        self._downloader = downloader or download_and_validate_mp3
        self._artwork_downloader = artwork_downloader or download_and_validate_artwork
        self._verifier = verifier or verify_album_files_in_repo
        self._repo_factory = repo_factory
        self._git_sync_factory = git_sync_factory or (lambda repo: BaseGitSyncService(repo))
        self._generator_factory = generator_factory or (lambda repo: GeneratorService(repo))
        self.push_enabled = push_enabled

    @staticmethod
    def _album_lock_key(dest_folder: str) -> str:
        return f"album_sync:{dest_folder.strip().lower()}"

    def _compute_auto_idempotency_key(self, normalized_selections: List[Dict[str, Any]], parent_job_id: Optional[str]) -> str:
        canonical_items = []
        for sel in sorted(normalized_selections, key=lambda x: int(x["notification_id"])):
            keys = sorted(str(k) for k in sel.get("selected_song_keys", []))
            canonical_items.append({"notification_id": int(sel["notification_id"]), "songs": keys})
        import json
        payload_str = json.dumps({"parent": parent_job_id, "items": canonical_items}, sort_keys=True)
        # Include 5-second bucket? No: explicit idempotency key handles retries cleanly; without one, generate unique id!
        return hashlib.sha256(payload_str.encode("utf-8")).hexdigest()[:24]

    def _build_validated_execution_plan(
        self,
        selections: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        if not selections:
            raise UploadPipelineError("No notifications or songs selected.")

        notif_ids = [int(s["notification_id"]) for s in selections]
        preview = build_add_to_musicdata_preview(
            notification_ids=notif_ids,
            db=self.db,
            local_root_override=self.local_root_override,
        )

        sel_map = {int(s["notification_id"]): s for s in selections}
        planned_albums: List[Dict[str, Any]] = []

        for album_plan in preview["albums"]:
            nid = album_plan["notification_id"]
            if not album_plan["identity_verified"]:
                raise UploadPipelineError(album_plan.get("identity_error") or f"Identity check failed for '{album_plan['album_name']}'.")

            req_sel = sel_map.get(nid, {})
            req_keys = req_sel.get("selected_song_keys")
            req_ids = req_sel.get("selected_song_ids")
            req_titles = req_sel.get("selected_song_titles")

            explicit_filter = (req_keys is not None) or (req_ids is not None) or (req_titles is not None)
            wanted_keys = {str(k) for k in (req_keys or [])}
            wanted_ids = {int(i) for i in (req_ids or []) if i is not None}
            wanted_titles = {str(t).strip().lower() for t in (req_titles or []) if str(t).strip()}

            if explicit_filter:
                valid_keys_for_notif = {s["song_key"] for s in album_plan["songs"]}
                valid_ids_for_notif = {s["song_id"] for s in album_plan["songs"] if s["song_id"] is not None}
                valid_titles_for_notif = {s["song_title"].strip().lower() for s in album_plan["songs"]}
                unknown_keys = [k for k in wanted_keys if k not in valid_keys_for_notif]
                unknown_ids = [i for i in wanted_ids if i not in valid_ids_for_notif]
                unknown_titles = [t for t in wanted_titles if t not in valid_titles_for_notif]
                if unknown_keys or unknown_ids or unknown_titles:
                    raise UploadPipelineError(
                        f"Invalid song selection for notification #{nid} ('{album_plan['album_name']}')."
                    )

            chosen_songs = []
            for s in album_plan["songs"]:
                matched_selection = (
                    (s["song_key"] in wanted_keys)
                    or (s["song_id"] is not None and s["song_id"] in wanted_ids)
                    or (s["song_title"].strip().lower() in wanted_titles)
                ) if explicit_filter else s["default_selected"]

                if matched_selection:
                    if s["already_in_catalog"]:
                        # Include as pre-marked skipped song so user gets explicit feedback without re-uploading
                        chosen_songs.append({
                            **s,
                            "status": STATUS_SKIPPED,
                            "error": None,
                            "used_bitrate": None,
                        })
                    elif not s["candidate_urls"]:
                        raise UploadPipelineError(
                            f"Selected song '{s['song_title']}' in '{album_plan['album_name']}' has no authorized download link."
                        )
                    else:
                        chosen_songs.append({
                            **s,
                            "status": STATUS_WAITING,
                            "error": None,
                            "used_bitrate": None,
                        })

            if not chosen_songs:
                continue

            planned_albums.append({
                "notification_id": nid,
                "album_name": album_plan["album_name"],
                "destination_folder": album_plan["destination_folder"],
                "language": album_plan["language"],
                "notification_type": album_plan["notification_type"],
                "action_mode": album_plan["action_mode"],
                "music_director": album_plan["music_director"],
                "year": album_plan["year"],
                "artwork_url": album_plan["artwork_url"],
                "artwork_will_download": album_plan["artwork_will_download"],
                "has_existing_artwork": album_plan["has_existing_artwork"],
                "status": STATUS_WAITING,
                "commit_sha": None,
                "commit_url": None,
                "commit_message": None,
                "error": None,
                "songs": chosen_songs,
            })

        if not planned_albums:
            raise UploadPipelineError("No eligible songs selected to add to MusicData.")

        return planned_albums

    def start_sync_job(
        self,
        selections: List[Dict[str, Any]],
        idempotency_key: Optional[str] = None,
        parent_job_id: Optional[str] = None,
        run_async: bool = True,
    ) -> Dict[str, Any]:
        """
        Validates selections, checks idempotency, acquires per-album distributed locks,
        creates job record in DB, and dispatches background processing.
        """
        if idempotency_key:
            existing = self.db.get_sync_job_by_idempotency(idempotency_key)
            if existing:
                return {
                    "success": True,
                    "duplicate_request": True,
                    "job": existing,
                }

        planned_albums = self._build_validated_execution_plan(selections)
        job_id = f"mdsync_{int(time.time() * 1000)}_{hashlib.sha256(os.urandom(8)).hexdigest()[:8]}"

        acquired_locks: List[str] = []
        try:
            for alb in planned_albums:
                # Only lock albums that actually have downloadable/pending songs
                if any(s["status"] == STATUS_WAITING for s in alb["songs"]):
                    l_key = self._album_lock_key(alb["destination_folder"])
                    if not self.db.acquire_named_lock(l_key, locked_by=job_id, timeout_minutes=15):
                        raise UploadPipelineError(
                            f"CONFLICT: Album '{alb['destination_folder']}' is currently being added/synced by another job."
                        )
                    acquired_locks.append(l_key)
        except Exception:
            for l_key in acquired_locks:
                self.db.release_named_lock(l_key, locked_by=job_id)
            raise

        initial_progress = {
            "status": STATUS_WAITING,
            "current_step": "Queued for processing",
            "total_albums": len(planned_albums),
            "completed_albums": 0,
            "total_songs": sum(len(a["songs"]) for a in planned_albums),
            "songs_added": 0,
            "songs_skipped": sum(1 for a in planned_albums for s in a["songs"] if s["status"] == STATUS_SKIPPED),
            "songs_failed": 0,
            "albums": planned_albums,
        }

        created_new, job_row = self.db.create_sync_job(
            job_id=job_id,
            idempotency_key=idempotency_key,
            status=STATUS_WAITING,
            request_data={"selections": selections},
            progress_data=initial_progress,
            parent_job_id=parent_job_id,
        )
        if not created_new:
            for l_key in acquired_locks:
                self.db.release_named_lock(l_key, locked_by=job_id)
            return {
                "success": True,
                "duplicate_request": True,
                "job": job_row,
            }

        if run_async:
            t = threading.Thread(
                target=self._execute_job,
                args=(job_id, planned_albums, acquired_locks),
                daemon=True,
            )
            t.start()
        else:
            self._execute_job(job_id, planned_albums, acquired_locks)
            job_row = self.db.get_sync_job(job_id) or job_row

        return {
            "success": True,
            "duplicate_request": False,
            "job": job_row,
        }

    def retry_failed_job(
        self,
        parent_job_id: str,
        idempotency_key: Optional[str] = None,
        run_async: bool = True,
    ) -> Dict[str, Any]:
        """Retries ONLY failed songs from an earlier job without re-uploading already-added songs."""
        parent_job = self.db.get_sync_job(parent_job_id)
        if not parent_job:
            raise UploadPipelineError(f"NOT_FOUND: Sync job '{parent_job_id}' not found.")

        parent_status = parent_job.get("status")
        if parent_status not in TERMINAL_STATUSES:
            raise UploadPipelineError(f"Job '{parent_job_id}' is still in progress ({parent_status}).")

        progress = parent_job.get("progress") or {}
        retry_selections: List[Dict[str, Any]] = []
        for alb in progress.get("albums", []):
            failed_keys = [
                s["song_key"]
                for s in alb.get("songs", [])
                if s.get("status") == STATUS_FAILED
            ]
            if failed_keys:
                retry_selections.append({
                    "notification_id": int(alb["notification_id"]),
                    "selected_song_keys": failed_keys,
                })

        if not retry_selections:
            raise UploadPipelineError("No failed songs remain to retry for this job.")

        return self.start_sync_job(
            selections=retry_selections,
            idempotency_key=idempotency_key,
            parent_job_id=parent_job_id,
            run_async=run_async,
        )

    def _provision_workspace_repo(
        self,
        job_id: str,
        album_names: List[str],
    ) -> IRepositoryProvider:
        if self._repo_factory:
            return self._repo_factory(job_id=job_id, album_names=album_names)

        source_override = self.local_root_override or os.environ.get("SOURCE_REPO_OVERRIDE")
        if is_cloud_mode():
            cloud_repo = CloudRepository(job_id=job_id)
            cloud_repo.provision_blobless_workspace(
                album_name=album_names[0] if album_names else None,
                source_repo_override=Path(source_override) if source_override else None,
                album_names=album_names,
            )
            return cloud_repo

        return LocalRepository(root_override=source_override)

    def _commit_and_push_with_retries(
        self,
        git_svc: BaseGitSyncService,
        dest_folder: str,
        mode: str,
        commit_msg: str,
        should_push: bool,
        max_attempts: int = 3,
    ) -> Dict[str, Any]:
        last_err: Optional[Exception] = None
        for attempt in range(max_attempts):
            try:
                return git_svc.stage_commit_and_push(
                    album_name=dest_folder,
                    mode=mode,
                    custom_commit_msg=commit_msg,
                    push_enabled=should_push,
                )
            except GitSyncError as ge:
                msg_low = (ge.message or "").lower()
                last_err = ge
                # Handle remote conflict (non-fast-forward) or transient 429/502/503 push errors safely
                if attempt < max_attempts - 1 and ge.stage == "git_push":
                    if any(hint in msg_low for hint in ("non-fast-forward", "rejected", "fetch first")):
                        git_svc._run_git(["pull", "--rebase", "origin", settings.GITHUB_BRANCH], check=False, timeout=25)
                        time.sleep(0.5)
                        continue
                    if any(hint in msg_low for hint in ("429", "502", "503", "504", "rate limit", "temporarily unavailable")):
                        time.sleep(1.5 * (attempt + 1))
                        continue
                raise GitSyncError(
                    message=sanitize_sensitive_text(ge.message),
                    stage=ge.stage,
                    exit_code=ge.exit_code,
                    stdout=sanitize_sensitive_text(ge.stdout),
                    stderr=sanitize_sensitive_text(ge.stderr),
                ) from ge
        raise GitSyncError(message=sanitize_sensitive_text(last_err))

    def _refresh_progress(self, job_id: str, albums: List[Dict[str, Any]], overall_status: str, current_step: str):
        songs_added = sum(1 for a in albums for s in a["songs"] if s["status"] == STATUS_ADDED)
        songs_skipped = sum(1 for a in albums for s in a["songs"] if s["status"] == STATUS_SKIPPED)
        songs_failed = sum(1 for a in albums for s in a["songs"] if s["status"] == STATUS_FAILED)
        completed_albums = sum(1 for a in albums if a["status"] in TERMINAL_STATUSES)
        total_songs = sum(len(a["songs"]) for a in albums)
        done_songs = songs_added + songs_skipped + songs_failed
        percent = int((done_songs / total_songs) * 100) if total_songs > 0 else 0
        progress = {
            "status": overall_status,
            "current_step": current_step,
            "total_albums": len(albums),
            "completed_albums": completed_albums,
            "total_songs": total_songs,
            "songs_added": songs_added,
            "songs_skipped": songs_skipped,
            "songs_failed": songs_failed,
            "percent": percent,
            "albums": albums,
        }
        self.db.update_sync_job(job_id=job_id, status=overall_status, progress_data=progress)
        return progress

    def _execute_job(
        self,
        job_id: str,
        albums: List[Dict[str, Any]],
        acquired_locks: List[str],
    ):
        staging_root = Path(tempfile.gettempdir()) / "musicdata_notif_staging" / job_id
        repo: Optional[IRepositoryProvider] = None

        try:
            staging_root.mkdir(parents=True, exist_ok=True)
            albums_to_stage: List[Dict[str, Any]] = []

            # =================================================================
            # PHASE 1: Download & Validate Artwork + MP3s into Isolated Staging
            # =================================================================
            for alb in albums:
                dest_folder = alb["destination_folder"]
                alb_stage_dir = staging_root / hashlib.sha256(dest_folder.encode("utf-8")).hexdigest()[:12]
                alb_stage_dir.mkdir(parents=True, exist_ok=True)

                # If all songs in this album were already marked Skipped during plan resolution
                if all(s["status"] == STATUS_SKIPPED for s in alb["songs"]):
                    alb["status"] = STATUS_SKIPPED
                    self._refresh_progress(job_id, albums, STATUS_DOWNLOADING, f"Skipped '{dest_folder}' (already in catalog)")
                    continue

                alb["status"] = STATUS_DOWNLOADING
                self._refresh_progress(job_id, albums, STATUS_DOWNLOADING, f"Downloading files for '{dest_folder}'...")

                # Download & validate artwork when needed
                staged_artwork_path: Optional[Path] = None
                if alb.get("artwork_will_download") and alb.get("artwork_url"):
                    try:
                        art_target = alb_stage_dir / f"{dest_folder}.png"
                        staged_artwork_path = self._artwork_downloader(
                            alb["artwork_url"],
                            art_target,
                            dest_folder,
                        )
                    except Exception as art_err:
                        err_msg = sanitize_sensitive_text(art_err)
                        logger.warning(f"[{job_id}] Artwork validation warning for '{dest_folder}': {err_msg}")
                        if alb["action_mode"] == "NEW_ALBUM":
                            for song in alb["songs"]:
                                if song["status"] != STATUS_SKIPPED:
                                    song["status"] = STATUS_FAILED
                                    song["error"] = f"Album artwork invalid/unavailable: {err_msg}"
                            alb["status"] = STATUS_FAILED
                            alb["error"] = f"Album artwork validation failed: {err_msg}"
                            self._refresh_progress(job_id, albums, STATUS_DOWNLOADING, f"Artwork failed for '{dest_folder}'")
                            continue

                staged_songs: List[Dict[str, Any]] = []
                for song in alb["songs"]:
                    if song["status"] == STATUS_SKIPPED:
                        continue
                    song["status"] = STATUS_DOWNLOADING
                    self._refresh_progress(job_id, albums, STATUS_DOWNLOADING, f"Downloading '{song['song_title']}'...")

                    mp3_stage_path = alb_stage_dir / song["target_filename"]
                    try:
                        dl_res = self._downloader(
                            song["candidate_urls"],
                            mp3_stage_path,
                            song["song_title"],
                            on_validating=lambda s_ref=song: (
                                s_ref.__setitem__("status", STATUS_VALIDATING),
                                self._refresh_progress(job_id, albums, STATUS_VALIDATING, f"Validating '{s_ref['song_title']}'...")
                            ),
                        )
                        song["used_bitrate"] = dl_res.get("used_bitrate")
                        staged_songs.append({"song": song, "staged_path": mp3_stage_path})
                    except Exception as dl_err:
                        song["status"] = STATUS_FAILED
                        song["error"] = sanitize_sensitive_text(dl_err)
                        mp3_stage_path.unlink(missing_ok=True)
                        self._refresh_progress(job_id, albums, STATUS_DOWNLOADING, f"Failed '{song['song_title']}'")

                if not staged_songs:
                    skipped_cnt = sum(1 for s in alb["songs"] if s["status"] == STATUS_SKIPPED)
                    failed_cnt = sum(1 for s in alb["songs"] if s["status"] == STATUS_FAILED)
                    if failed_cnt > 0:
                        alb["status"] = STATUS_FAILED
                        alb["error"] = alb.get("error") or "No valid songs could be downloaded/validated for this album."
                    elif skipped_cnt > 0:
                        alb["status"] = STATUS_SKIPPED
                    else:
                        alb["status"] = STATUS_FAILED
                        alb["error"] = alb.get("error") or "No valid songs available."
                    for s in alb["songs"]:
                        if s["status"] not in TERMINAL_STATUSES:
                            s["status"] = STATUS_FAILED
                            s["error"] = alb.get("error") or "Download/validation not completed."
                    self._refresh_progress(job_id, albums, STATUS_DOWNLOADING, f"Finished preparing '{dest_folder}'")
                    continue

                albums_to_stage.append({
                    "album": alb,
                    "stage_dir": alb_stage_dir,
                    "staged_artwork_path": staged_artwork_path,
                    "staged_songs": staged_songs,
                })

            # =================================================================
            # PHASE 2: Provision Repository Once, Commit & Verify Per Album
            # =================================================================
            if albums_to_stage:
                target_folders = [entry["album"]["destination_folder"] for entry in albums_to_stage]
                self._refresh_progress(job_id, albums, STATUS_UPLOADING, "Provisioning MusicData repository workspace...")
                repo = self._provision_workspace_repo(job_id=job_id, album_names=target_folders)
                meta_svc = MetadataService(repo)
                dup_svc = DuplicateDetectionService(repo)
                git_svc = self._git_sync_factory(repo)

                should_push = (
                    self.push_enabled
                    if self.push_enabled is not None
                    else (isinstance(repo, CloudRepository) or bool(os.environ.get("GIT_PUSH_ENABLED", "1") == "1"))
                )

                for entry in albums_to_stage:
                    alb = entry["album"]
                    dest_folder = alb["destination_folder"]
                    alb["status"] = STATUS_UPLOADING
                    for st_item in entry["staged_songs"]:
                        st_item["song"]["status"] = STATUS_UPLOADING
                    self._refresh_progress(job_id, albums, STATUS_UPLOADING, f"Uploading '{dest_folder}' to MusicData...")

                    pre_album_head = ""
                    try:
                        if (repo.root / ".git").exists():
                            pre_album_head = git_svc._run_git(["rev-parse", "HEAD"], check=False, timeout=10)
                        # Ensure sparse checkout covers folder if using CloudRepository
                        if isinstance(repo, CloudRepository) and (repo.root / ".git").exists():
                            git_svc._run_git(["sparse-checkout", "add", dest_folder], check=False, timeout=15)

                        album_dir = repo.get_album_path(dest_folder)
                        if alb["action_mode"] == "EXISTING_ALBUM" and not album_dir.exists():
                            # Existing album must never be silently recreated under wrong folder if missing in repo
                            raise UploadPipelineError(
                                f"Target existing album folder '{dest_folder}' not present in repository workspace."
                            )
                        album_dir.mkdir(parents=True, exist_ok=True)

                        # Create album_info.json ONLY when missing (never overwrite existing album metadata!)
                        info_path = repo.get_album_info_path(dest_folder)
                        if not info_path.exists():
                            meta_svc.save_album_info(dest_folder, {
                                "album": dest_folder,
                                "year": alb.get("year") or 2026,
                                "musicDirector": alb.get("music_director") or "Unknown",
                                "language": alb.get("language") or "Telugu",
                                "genre": f"{alb.get('language') or 'Telugu'} Soundtrack",
                                "country": "India",
                                "releaseDate": f"{alb.get('year') or 2026}-01-01",
                            })

                        # Copy artwork ONLY when no existing PNG/artwork file exists in album directory
                        added_filenames: List[str] = []
                        staged_art = entry.get("staged_artwork_path")
                        has_any_art = any(
                            p.is_file()
                            for ext in (".png", ".jpg", ".jpeg")
                            for p in album_dir.glob(f"*{ext}")
                        )
                        if staged_art and staged_art.exists() and not has_any_art:
                            dest_art = repo.get_image_path(dest_folder)
                            shutil.copy2(str(staged_art), str(dest_art))
                            added_filenames.append(dest_art.name)

                        # Copy ONLY non-duplicate MP3s (never overwrite existing MP3 files!)
                        added_songs_entries: List[Dict[str, Any]] = []
                        for st_item in entry["staged_songs"]:
                            song = st_item["song"]
                            src_mp3: Path = st_item["staged_path"]
                            dest_mp3 = repo.get_song_path(dest_folder, song["target_filename"])

                            if dest_mp3.exists():
                                song["status"] = STATUS_SKIPPED
                                song["error"] = None
                                continue

                            dup_res = dup_svc.analyze_uploaded_song(
                                album_name=dest_folder,
                                song_filename=song["target_filename"],
                                file_size=src_mp3.stat().st_size,
                                duration_seconds=1,
                                temp_file_path=src_mp3,
                            )
                            if dup_res.get("status") == "EXACT_DUPLICATE":
                                song["status"] = STATUS_SKIPPED
                                song["error"] = None
                                continue

                            shutil.copy2(str(src_mp3), str(dest_mp3))
                            added_filenames.append(dest_mp3.name)
                            added_songs_entries.append(song)

                        if not added_songs_entries and not any(f.endswith(".png") for f in added_filenames):
                            alb["status"] = STATUS_SKIPPED if any(s["status"] == STATUS_SKIPPED for s in alb["songs"]) else STATUS_FAILED
                            continue

                        # Run authoritative Metadata Generators #1 and #2
                        self._generator_factory(repo).run_generator_pipeline()

                        song_titles_preview = ", ".join(s["song_title"] for s in added_songs_entries[:3])
                        if len(added_songs_entries) > 3:
                            song_titles_preview += f" (+{len(added_songs_entries) - 3} more)"

                        if alb["action_mode"] == "NEW_ALBUM":
                            commit_msg = f"Add new album {dest_folder} ({len(added_songs_entries)} songs)"
                            git_mode = "add"
                        else:
                            commit_msg = f"Add {len(added_songs_entries)} missing song(s) to {dest_folder}: {song_titles_preview}"
                            git_mode = "edit"

                        git_res = self._commit_and_push_with_retries(
                            git_svc=git_svc,
                            dest_folder=dest_folder,
                            mode=git_mode,
                            commit_msg=commit_msg,
                            should_push=should_push,
                        )
                        commit_sha = git_res.get("commit_sha")
                        alb["commit_sha"] = commit_sha
                        alb["commit_message"] = git_res.get("commit_message") or commit_msg
                        if commit_sha and commit_sha not in ("clean_tree_sha", "dry_run_sha"):
                            alb["commit_url"] = f"https://github.com/{settings.GITHUB_OWNER}/{settings.MUSICDATA_GITHUB_REPOSITORY}/commit/{commit_sha}"

                        # Refresh authoritative catalog cache BEFORE verifying & reconciling
                        cloud_catalog_service.refresh_catalog()

                        # Mandatory Post-Commit Verification before marking songs/notification resolved
                        verify_target_dir = None
                        if isinstance(repo, LocalRepository) or not should_push or os.environ.get("SOURCE_REPO_OVERRIDE"):
                            verify_target_dir = repo.root

                        ver_res = self._verifier(
                            album_folder=dest_folder,
                            expected_filenames=added_filenames,
                            commit_sha=commit_sha,
                            target_repo_dir=verify_target_dir,
                        )
                        if not ver_res.get("verified"):
                            raise UploadPipelineError(
                                ver_res.get("error")
                                or f"Verification failed: files not confirmed in MusicData repository ({ver_res.get('missing_files')})."
                            )

                        # Mark uploaded songs Added Successfully & reconcile ONLY verified songs
                        for s_ok in added_songs_entries:
                            s_ok["status"] = STATUS_ADDED
                            s_ok["error"] = None

                        selective_reconcile_notification(
                            db=self.db,
                            notification_id=int(alb["notification_id"]),
                            verified_added_song_keys=[s["song_key"] for s in added_songs_entries],
                            verified_added_filenames=[s["target_filename"] for s in added_songs_entries],
                        )

                        has_failed = any(s["status"] == STATUS_FAILED for s in alb["songs"])
                        alb["status"] = STATUS_PARTIALLY_FAILED if has_failed else STATUS_ADDED
                        self._refresh_progress(job_id, albums, STATUS_UPLOADING, f"Verified '{dest_folder}' commit in MusicData")

                    except Exception as upload_err:
                        err_clean = sanitize_sensitive_text(upload_err)
                        logger.error(f"[{job_id}] Upload/verification failed for '{dest_folder}': {err_clean}")
                        # Roll back any local commit/working-tree edits for this failed album so subsequent albums stay isolated
                        try:
                            if (repo.root / ".git").exists():
                                reset_target = pre_album_head or "HEAD"
                                git_svc._run_git(["reset", "--hard", reset_target], check=False, timeout=15)
                                git_svc._run_git(["clean", "-fd"], check=False, timeout=15)
                        except Exception:
                            pass

                        for s in alb["songs"]:
                            if s["status"] not in (STATUS_ADDED, STATUS_SKIPPED):
                                s["status"] = STATUS_FAILED
                                s["error"] = err_clean
                        alb["status"] = STATUS_FAILED
                        alb["error"] = err_clean
                        self._refresh_progress(job_id, albums, STATUS_UPLOADING, f"Upload failed for '{dest_folder}'")
                    finally:
                        alb_dir_tmp = entry.get("stage_dir")
                        if alb_dir_tmp and Path(alb_dir_tmp).exists():
                            shutil.rmtree(alb_dir_tmp, ignore_errors=True)

            # =================================================================
            # PHASE 3: Final Summary & Cleanup
            # =================================================================
            # Ensure every single album and song is transitioned to a terminal status
            for a in albums:
                for s in a["songs"]:
                    if s["status"] not in TERMINAL_STATUSES:
                        s["status"] = STATUS_FAILED
                        s["error"] = s.get("error") or "Sync aborted before song could be processed."
                if a["status"] not in TERMINAL_STATUSES:
                    a["status"] = STATUS_FAILED
                    a["error"] = a.get("error") or "Sync aborted before album could be committed."

            songs_added = sum(1 for a in albums for s in a["songs"] if s["status"] == STATUS_ADDED)
            songs_skipped = sum(1 for a in albums for s in a["songs"] if s["status"] == STATUS_SKIPPED)
            songs_failed = sum(1 for a in albums for s in a["songs"] if s["status"] == STATUS_FAILED)
            any_album_failed = any(a["status"] in (STATUS_FAILED, STATUS_PARTIALLY_FAILED) for a in albums)

            if songs_failed == 0 and not any_album_failed:
                final_status = STATUS_ADDED if songs_added > 0 else STATUS_SKIPPED
            elif songs_added > 0 or songs_skipped > 0:
                final_status = STATUS_PARTIALLY_FAILED
            else:
                final_status = STATUS_FAILED

            commit_links = [
                {
                    "album_name": a["destination_folder"],
                    "commit_sha": a["commit_sha"],
                    "commit_url": a["commit_url"],
                    "commit_message": a["commit_message"],
                }
                for a in albums
                if a.get("commit_sha")
            ]
            dest_folders = [a["destination_folder"] for a in albums if a.get("destination_folder")]

            final_progress = {
                "status": final_status,
                "current_step": "Completed" if final_status in (STATUS_ADDED, STATUS_SKIPPED) else f"Finished: {final_status}",
                "total_albums": len(albums),
                "completed_albums": len(albums),
                "total_songs": sum(len(a["songs"]) for a in albums),
                "songs_added": songs_added,
                "songs_skipped": songs_skipped,
                "songs_failed": songs_failed,
                "percent": 100,
                "albums": albums,
            }
            final_result = {
                "success": final_status in (STATUS_ADDED, STATUS_SKIPPED),
                "status": final_status,
                "albums_processed": len(albums),
                "songs_added": songs_added,
                "songs_skipped": songs_skipped,
                "songs_failed": songs_failed,
                "commit_links": commit_links,
                "destination_folders": dest_folders,
                "retry_available": songs_failed > 0,
            }

            self.db.update_sync_job(
                job_id=job_id,
                status=final_status,
                progress_data=final_progress,
                result_data=final_result,
                error_message=None if final_status in (STATUS_ADDED, STATUS_SKIPPED) else f"{songs_failed} song(s) failed.",
                completed_at=time.time(),
            )

        except Exception as fatal_err:
            err_msg = sanitize_sensitive_text(fatal_err)
            logger.error(f"[{job_id}] Fatal Add-to-MusicData error: {err_msg}")
            for a in albums:
                if a["status"] not in TERMINAL_STATUSES:
                    a["status"] = STATUS_FAILED
                    a["error"] = err_msg
                for s in a["songs"]:
                    if s["status"] not in TERMINAL_STATUSES:
                        s["status"] = STATUS_FAILED
                        s["error"] = err_msg
            final_progress = self._refresh_progress(job_id, albums, STATUS_FAILED, "Job failed")
            self.db.update_sync_job(
                job_id=job_id,
                status=STATUS_FAILED,
                progress_data=final_progress,
                result_data={
                    "success": False,
                    "status": STATUS_FAILED,
                    "albums_processed": len(albums),
                    "songs_added": final_progress["songs_added"],
                    "songs_skipped": final_progress["songs_skipped"],
                    "songs_failed": final_progress["songs_failed"],
                    "commit_links": [],
                    "destination_folders": [a["destination_folder"] for a in albums if a.get("destination_folder")],
                    "retry_available": final_progress["songs_failed"] > 0,
                },
                error_message=err_msg,
                completed_at=time.time(),
            )
        finally:
            if staging_root.exists():
                shutil.rmtree(staging_root, ignore_errors=True)
            if isinstance(repo, CloudRepository):
                try:
                    repo.cleanup_workspace()
                except Exception:
                    pass
            for l_key in acquired_locks:
                try:
                    self.db.release_named_lock(l_key, locked_by=job_id)
                except Exception:
                    pass
