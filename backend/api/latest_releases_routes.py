import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Header, Query
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

@router.get("/notifications")
def get_notifications(
    category: Optional[str] = Query("all", description="Filter category: all, new_album, new_song, missing_songs, album_updated, scan_failed"),
    unread_only: bool = Query(False, description="Filter only unread notifications"),
    limit: int = Query(50, description="Max notifications to retrieve")
):
    """Retrieve discovered notifications with optional category filtering."""
    db = get_latest_releases_db()
    notifs = db.get_notifications(category=category, unread_only=unread_only, limit=limit)
    return {
        "success": True,
        "count": len(notifs),
        "notifications": notifs
    }

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
