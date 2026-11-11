import logging
import re
import urllib.parse
import urllib.request
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Header, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from backend.core.config import settings
from backend.services.latest_releases.models import TrackingStatus
from backend.services.latest_releases.db import get_latest_releases_db
from backend.services.latest_releases.discovery import LatestReleasesDiscoveryEngine

logger = logging.getLogger("musicdata.api.latest_releases")

router = APIRouter(prefix="/api/latest-releases", tags=["Latest Releases Discovery Engine"])

class TrackingUpdateRequest(BaseModel):
    song_id: int
    status: str

ALLOWED_DOWNLOAD_DOMAINS = [
    "naasongs.com.co",
    "mp3teluguwap.net",
    "sensongsmp3.tv",
    "sensongsmp3.co"
]

def is_domain_allowed(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
        hostname = (parsed.hostname or "").lower()
        if not hostname or hostname in ("localhost", "127.0.0.1", "::1"):
            return False
        # Disallow private IPs
        if hostname.startswith("192.168.") or hostname.startswith("10.") or hostname.startswith("172."):
            return False
        for allowed in ALLOWED_DOWNLOAD_DOMAINS:
            if hostname == allowed or hostname.endswith("." + allowed):
                return True
        return False
    except Exception:
        return False

def sanitize_filename(filename: str) -> str:
    clean = re.sub(r'[\\/*?:"<>|]', "", filename)
    clean = re.sub(r'\s+', " ", clean).strip()
    return clean or "song.mp3"

@router.get("/notifications")
def get_notifications(
    category: Optional[str] = Query("all", description="Filter category: all, new_album, new_song, missing_songs, uncertain, album_updated, scan_failed"),
    language: Optional[str] = Query("all", description="Filter language: all, Telugu, Tamil, Hindi, Kannada, Malayalam, etc."),
    status: Optional[str] = Query("active", description="Filter resolution status: active, completed, all"),
    unread_only: bool = Query(False, description="Filter only unread notifications"),
    limit: int = Query(50, description="Max notifications to retrieve")
):
    """Retrieve discovered notifications with language, status, and category filtering."""
    db = get_latest_releases_db()
    
    # Run lightweight reconciliation on active queries
    if status == "active":
        try:
            from backend.services.latest_releases.reconciliation import reconcile_active_notifications
            reconcile_active_notifications(db)
        except Exception as e:
            logger.debug(f"Reconciliation notice on get_notifications: {e}")

    notifs = db.get_notifications(
        category=category,
        language=language,
        status=status,
        unread_only=unread_only,
        limit=limit
    )
    return {
        "success": True,
        "count": len(notifs),
        "notifications": notifs
    }

@router.post("/reconcile")
def trigger_reconciliation():
    """Explicitly trigger live reconciliation pass against current MusicData catalog."""
    from backend.services.latest_releases.reconciliation import reconcile_active_notifications
    db = get_latest_releases_db()
    result = reconcile_active_notifications(db)
    return {
        "success": True,
        "reconciled": result
    }

@router.get("/download-track")
def download_track(
    url: str = Query(..., description="Source MP3 audio URL"),
    album_name: Optional[str] = Query(None, description="Album name for tagging"),
    song_title: Optional[str] = Query(None, description="Song title for filename"),
    track_number: int = Query(1, description="Track number")
):
    """
    Authorized proxy endpoint for downloading MP3 track directly to user browser.
    Sanitizes filename as `<track_num:02d> - <song_title>.mp3` and verifies source domain.
    """
    if not is_domain_allowed(url):
        raise HTTPException(
            status_code=400,
            detail=f"Unauthorized download source. Domain must be an approved provider: {ALLOWED_DOWNLOAD_DOMAINS}"
        )

    # Mandatory Duplicate-Download Protection (Backend Verification)
    if album_name and song_title:
        from backend.services.catalog import cloud_catalog_service
        try:
            cat_match = cloud_catalog_service.find_canonical_album(album_name)
            if cat_match:
                cat_meta = cloud_catalog_service.get_album_metadata(cat_match)
                cat_songs = cat_meta.get("songs", [])
                is_matched, conf, score, matched = cloud_catalog_service.match_song_to_catalog(song_title, cat_songs)
                if is_matched:
                    matched_title = matched.get("title") if matched else song_title
                    raise HTTPException(
                        status_code=400,
                        detail=f"Duplicate Download Blocked: Song '{matched_title}' already exists in MusicData catalog for album '{cat_match.get('name')}'."
                    )
        except HTTPException:
            raise
        except Exception as e:
            logger.debug(f"Catalog duplicate verification bypassed due to lookup error: {e}")

    base_name = song_title or url.split("/")[-1].replace(".mp3", "")
    safe_name = sanitize_filename(base_name)
    if not safe_name.lower().endswith(".mp3"):
        filename = f"{track_number:02d} - {safe_name}.mp3"
    else:
        filename = safe_name

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "Referer": "https://naasongs.com.co/"
            }
        )
        response = urllib.request.urlopen(req, timeout=30)

        def iterfile():
            try:
                while chunk := response.read(64 * 1024):
                    yield chunk
            finally:
                response.close()

        quoted_filename = urllib.parse.quote(filename)
        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"; filename*=UTF-8\'\'{quoted_filename}',
            "Content-Type": "audio/mpeg"
        }
        return StreamingResponse(iterfile(), headers=headers, media_type="audio/mpeg")

    except Exception as e:
        logger.error(f"Download proxy error for {url}: {e}")
        raise HTTPException(status_code=502, detail=f"Failed to fetch audio stream: {str(e)}")

@router.get("/unread-count")
def get_unread_count():
    """Lightweight endpoint for polling notification badge counter."""
    db = get_latest_releases_db()
    count = db.get_unread_count()
    return {
        "success": True,
        "unread_count": count
    }

@router.get("/history")
def get_scan_history(limit: int = Query(15, description="Max scan records")):
    """Get telemetry history of recent discovery scans."""
    db = get_latest_releases_db()
    scans = db.get_recent_scans(limit=limit)
    return {
        "success": True,
        "scans": scans
    }

@router.get("/status")
def get_engine_status():
    """Get current discovery engine state and lock status."""
    db = get_latest_releases_db()
    lock_info = db.get_lock_status()
    recent_scans = db.get_recent_scans(limit=1)
    last_scan = recent_scans[0] if recent_scans else None
    return {
        "success": True,
        "is_locked": lock_info.get("is_locked", False),
        "lock_info": lock_info,
        "last_scan": last_scan
    }

@router.post("/check-now")
def trigger_manual_check(limit: int = Query(15, description="Number of albums to scan")):
    """Trigger an immediate manual scan from the Web UI."""
    engine = LatestReleasesDiscoveryEngine()
    result = engine.run_scan(locked_by="manual_web_ui", limit_albums=limit)
    if not result.get("success") and result.get("status") == "LOCKED":
        raise HTTPException(status_code=409, detail=result.get("message"))
    return result

@router.post("/notifications/{notification_id}/read")
def mark_notification_read(notification_id: int):
    """Mark a specific notification as read."""
    db = get_latest_releases_db()
    db.mark_notification_read(notification_id)
    return {"success": True, "message": f"Notification #{notification_id} marked as read."}

@router.post("/notifications/mark-all-read")
def mark_all_notifications_read():
    """Mark all notifications as read."""
    db = get_latest_releases_db()
    db.mark_all_notifications_read()
    return {"success": True, "message": "All notifications marked as read."}

@router.post("/tracking/update")
def update_song_tracking(req: TrackingUpdateRequest):
    """Explicitly update the tracking status of a discovered song (e.g. DOWNLOADED_BY_USER, IGNORED)."""
    try:
        status_enum = TrackingStatus(req.status.upper())
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status '{req.status}'. Must be one of: {[s.value for s in TrackingStatus]}"
        )

    db = get_latest_releases_db()
    db.update_song_tracking_status(req.song_id, status_enum)
    return {
        "success": True,
        "song_id": req.song_id,
        "new_status": status_enum.value
    }

@router.post("/cron-trigger")
def cron_trigger(x_cron_secret: Optional[str] = Header(None)):
    """
    Webhook trigger endpoint for external schedulers or Render Cron jobs.
    Protected by secret header verification.
    """
    if settings.CRON_SECRET and x_cron_secret != settings.CRON_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized: invalid or missing X-Cron-Secret header.")

    engine = LatestReleasesDiscoveryEngine()
    result = engine.run_scan(locked_by="external_cron_webhook", limit_albums=20)
    return result
