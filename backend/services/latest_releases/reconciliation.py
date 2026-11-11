import logging
from typing import Optional, Dict, Any, List
from backend.services.fuzzy_search import normalize_text, calculate_similarity_ratio
from backend.services.latest_releases.models import (
    NotificationResolutionStatus,
    MatchConfidence,
    TrackingStatus
)
from backend.services.latest_releases.db import LatestReleasesDB, get_latest_releases_db
from backend.services.latest_releases.song_comparator import SongComparator

logger = logging.getLogger("musicdata.latest_releases.reconciliation")

def reconcile_active_notifications(
    db: Optional[LatestReleasesDB] = None,
    comparator: Optional[SongComparator] = None
) -> Dict[str, Any]:
    """
    Live reconciliation engine:
    Scans active notifications ('PENDING' and 'PARTIALLY_COMPLETED') against the current MusicData catalog.
    If songs/albums have been added to MusicData (via manual Add/Edit or sync), automatically updates:
      - exists_in_catalog = True
      - missing_count
      - resolution_status -> COMPLETED (when all tracks exist) or PARTIALLY_COMPLETED
      - unread counts and badge statuses
    """
    if db is None:
        db = get_latest_releases_db()
    if comparator is None:
        comparator = SongComparator()

    try:
        active_notifications = db.get_active_notifications(limit=200)
    except Exception as e:
        logger.warning(f"Failed to fetch active notifications for reconciliation: {e}")
        return {"reconciled_count": 0, "resolved_notifications": 0, "partially_resolved": 0}

    if not active_notifications:
        return {"reconciled_count": 0, "resolved_notifications": 0, "partially_resolved": 0}

    resolved_count = 0
    partially_resolved_count = 0

    for notif in active_notifications:
        notif_id = notif.get("id")
        album_name = notif.get("album_name") or ""
        language = notif.get("language")
        songs: List[Dict[str, Any]] = notif.get("songs") or []
        orig_missing_count = notif.get("missing_count", 0)

        if not album_name or not notif_id:
            continue

        catalog_match = comparator.find_matching_catalog_album(album_name, language)
        if not catalog_match:
            # Album still not in MusicData catalog
            continue

        catalog_songs = comparator.fetch_catalog_album_songs(catalog_match)
        if not catalog_songs:
            continue

        # Re-evaluate each song in notification
        changed = False
        for song in songs:
            if song.get("exists_in_catalog"):
                continue

            disc_title = song.get("song_title") or song.get("normalized_title") or ""
            clean_disc = comparator.normalize_song_title(disc_title)

            best_cat_song = None
            best_score = 0.0

            for cat_song in catalog_songs:
                cat_title = cat_song.get("title") or cat_song.get("normalizedTitle") or ""
                clean_cat = comparator.normalize_song_title(cat_title)
                score1 = calculate_similarity_ratio(clean_disc, clean_cat)
                score2 = calculate_similarity_ratio(normalize_text(disc_title), normalize_text(cat_title))
                score = max(score1, score2)
                if score > best_score:
                    best_score = score
                    best_cat_song = cat_song

            if best_score >= comparator.HIGH_CONFIDENCE_THRESHOLD:
                song["exists_in_catalog"] = True
                song["catalog_song_title"] = best_cat_song.get("title") if best_cat_song else None
                song["catalog_song_id"] = best_cat_song.get("id") if best_cat_song else None
                song["match_confidence"] = MatchConfidence.HIGH.value
                song["match_score"] = round(best_score, 3)
                song["tracking_status"] = TrackingStatus.ADDED_TO_MUSICDATA.value
                changed = True

        new_missing_count = sum(1 for s in songs if not s.get("exists_in_catalog"))

        if new_missing_count == 0:
            # All songs now exist in MusicData!
            db.update_notification_resolution(
                notification_id=notif_id,
                status=NotificationResolutionStatus.COMPLETED.value,
                updated_songs=songs,
                missing_count=0
            )
            # Mark read so it doesn't inflate unread badge
            db.mark_notification_read(notif_id)
            resolved_count += 1
        elif changed or new_missing_count < orig_missing_count:
            # Some songs were added
            db.update_notification_resolution(
                notification_id=notif_id,
                status=NotificationResolutionStatus.PARTIALLY_COMPLETED.value,
                updated_songs=songs,
                missing_count=new_missing_count
            )
            partially_resolved_count += 1

    return {
        "reconciled_count": len(active_notifications),
        "resolved_notifications": resolved_count,
        "partially_resolved": partially_resolved_count
    }
