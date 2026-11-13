import os
import re
import inspect
import logging
import urllib.parse
from typing import Optional, Dict, Any, List, Tuple
from backend.services.fuzzy_search import normalize_text, calculate_similarity_ratio
from backend.services.latest_releases.models import (
    NotificationResolutionStatus,
    NotificationType,
    MatchConfidence,
    TrackingStatus
)
from backend.services.catalog import CatalogLookupError, CloudCatalogService
from backend.services.latest_releases.db import (
    LatestReleasesDB,
    get_latest_releases_db,
    compute_canonical_album_key,
    deduplicate_and_merge_song_dicts,
    build_notification_summary_text,
)
from backend.services.latest_releases.song_comparator import SongComparator

logger = logging.getLogger("musicdata.latest_releases.reconciliation")


def _extract_source_filename(song: Dict[str, Any]) -> Optional[str]:
    audio_url = song.get("source_audio_url") or ""
    if not audio_url:
        return None
    tail = urllib.parse.unquote(str(audio_url).split("?")[0].rstrip("/").split("/")[-1])
    return tail or None


def _safe_update_resolution(db: Any, **kwargs) -> None:
    """Calls db.update_notification_resolution while remaining compatible with minimal mock DBs."""
    fn = db.update_notification_resolution
    try:
        params = inspect.signature(fn).parameters
        has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        filtered = kwargs if has_var_kw else {k: v for k, v in kwargs.items() if k in params and v is not None}
        if "updated_songs" in kwargs and "updated_songs" in params:
            filtered["updated_songs"] = kwargs["updated_songs"]
        if "missing_count" in kwargs and "missing_count" in params:
            filtered["missing_count"] = kwargs["missing_count"]
        fn(**filtered)
    except Exception:
        fn(
            notification_id=kwargs["notification_id"],
            status=kwargs["status"],
            updated_songs=kwargs.get("updated_songs"),
            missing_count=kwargs.get("missing_count"),
        )


def _consolidate_duplicate_active_notifications(
    db: LatestReleasesDB,
    active_notifications: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Merges duplicate active notifications that share the same canonical `(clean_album, normalized_language)`
    identity (keeping earliest page/position row, preserving read state, marking duplicate rows COMPLETED).
    Returns `(deduped_active_list, consolidated_duplicates_count)`.
    """
    groups: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    for notif in active_notifications:
        if not notif.get("id") or not notif.get("album_name"):
            continue
        c_key, _, _ = compute_canonical_album_key(notif.get("album_name"), notif.get("language"))
        if c_key not in groups:
            groups[c_key] = []
            order.append(c_key)
        groups[c_key].append(notif)

    survivors: List[Dict[str, Any]] = []
    consolidated_dups = 0

    for c_key in order:
        group = groups[c_key]
        if len(group) == 1:
            survivors.append(group[0])
            continue

        group.sort(
            key=lambda n: (
                n.get("source_page") if n.get("source_page") is not None else 999999,
                n.get("source_position") if n.get("source_position") is not None else 999999,
                0 if (n.get("resolution_status") or "").upper() == "PARTIALLY_COMPLETED" else 1,
                0 if n.get("notification_type") in ("NEW_SONG", "MISSING_SONGS") else 1,
                n.get("id") or 999999,
            )
        )
        survivor = dict(group[0])
        merged_songs: List[Dict[str, Any]] = []
        any_partial = False
        any_song_level = False
        best_url = survivor.get("source_url") or ""

        for n in group:
            if (n.get("resolution_status") or "").upper() == "PARTIALLY_COMPLETED":
                any_partial = True
            if n.get("notification_type") in ("NEW_SONG", "MISSING_SONGS"):
                any_song_level = True
            u = n.get("source_url") or ""
            if u and (not best_url or len(u) > len(best_url)):
                best_url = u
            merged_songs = deduplicate_and_merge_song_dicts(merged_songs, n.get("songs") or [])

        for dup in group[1:]:
            dup_id = dup.get("id")
            if dup_id:
                try:
                    _safe_update_resolution(
                        db,
                        notification_id=dup_id,
                        status=NotificationResolutionStatus.COMPLETED.value,
                        updated_songs=dup.get("songs") or [],
                        missing_count=0,
                        is_read=True,
                    )
                    if hasattr(db, "mark_notification_read"):
                        db.mark_notification_read(dup_id)
                    consolidated_dups += 1
                except Exception as exc:
                    logger.warning(f"Failed closing duplicate active notification #{dup_id}: {exc}")

        _, clean_display, norm_lang = compute_canonical_album_key(survivor.get("album_name"), survivor.get("language"))
        missing_count = sum(1 for s in merged_songs if not s.get("exists_in_catalog")) if merged_songs else (survivor.get("missing_count") or 0)
        any_catalog_song = any(bool(s.get("exists_in_catalog")) for s in merged_songs)

        if any_song_level or any_catalog_song:
            eff_type_enum = NotificationType.NEW_SONG if missing_count <= 1 else NotificationType.MISSING_SONGS
        else:
            try:
                eff_type_enum = NotificationType(survivor.get("notification_type") or NotificationType.NEW_ALBUM.value)
            except Exception:
                eff_type_enum = NotificationType.NEW_ALBUM

        eff_status = (
            NotificationResolutionStatus.PARTIALLY_COMPLETED.value
            if any_partial
            else (survivor.get("resolution_status") or NotificationResolutionStatus.PENDING.value)
        )
        new_title, new_message = build_notification_summary_text(
            eff_type_enum,
            clean_display,
            norm_lang,
            merged_songs,
            music_director=survivor.get("music_director") or "Unknown",
            year=survivor.get("year") or 2026,
        )
        _safe_update_resolution(
            db,
            notification_id=survivor["id"],
            status=eff_status,
            updated_songs=merged_songs,
            missing_count=missing_count,
            notification_type=eff_type_enum.value,
            title=new_title,
            message=new_message,
            language=norm_lang,
            canonical_album_key=c_key,
        )
        survivor["songs"] = merged_songs
        survivor["missing_count"] = missing_count
        survivor["notification_type"] = eff_type_enum.value
        survivor["title"] = new_title
        survivor["message"] = new_message
        survivor["language"] = norm_lang
        survivor["resolution_status"] = eff_status
        survivor["album_name"] = clean_display
        survivors.append(survivor)

    return survivors, consolidated_dups


def reconcile_active_notifications(
    db: Optional[LatestReleasesDB] = None,
    comparator: Optional[SongComparator] = None
) -> Dict[str, Any]:
    """
    Live reconciliation engine:
    1. Consolidates duplicate active notifications sharing canonical `(album, language)` identity.
    2. Scans active notifications ('PENDING' and 'PARTIALLY_COMPLETED') against the current MusicData catalog.
    3. Automatically resolves false positives / uploaded albums & songs:
       - exists_in_catalog = True
       - upgrades stale NEW_ALBUM -> NEW_SONG / MISSING_SONGS when catalog match exists
       - resolution_status -> COMPLETED (when all tracks exist) or PARTIALLY_COMPLETED
       - marks COMPLETED rows read so unread counts remain accurate
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

    active_notifications, consolidated_duplicates = _consolidate_duplicate_active_notifications(db, active_notifications)

    resolved_count = 0
    partially_resolved_count = 0
    deferred_on_catalog_error = 0
    catalog_album_cache: Dict[Tuple[str, Optional[int]], Optional[Dict[str, Any]]] = {}
    catalog_songs_cache: Dict[str, List[Dict[str, Any]]] = {}

    for notif in active_notifications:
        notif_id = notif.get("id")
        album_name = notif.get("album_name") or ""
        raw_language = notif.get("language")
        canon_key, clean_display, norm_lang = compute_canonical_album_key(album_name, raw_language)
        songs: List[Dict[str, Any]] = deduplicate_and_merge_song_dicts([], notif.get("songs") or [])
        orig_missing_count = notif.get("missing_count", 0)
        orig_type = notif.get("notification_type") or ""

        if not album_name or not notif_id:
            continue

        # Auto-resolve zero-song orphan notifications (e.g. from dead redirects or empty pages)
        if not songs or len(songs) == 0:
            _safe_update_resolution(
                db,
                notification_id=notif_id,
                status=NotificationResolutionStatus.COMPLETED.value,
                updated_songs=[],
                missing_count=0,
                language=norm_lang,
                canonical_album_key=canon_key,
                is_read=True,
            )
            if hasattr(db, "mark_notification_read"):
                db.mark_notification_read(notif_id)
            resolved_count += 1
            continue

        # Heal legacy language typo (e.g. Taelugu -> Telugu) even before catalog check
        if raw_language and raw_language != norm_lang:
            _safe_update_resolution(
                db,
                notification_id=notif_id,
                status=notif.get("resolution_status") or NotificationResolutionStatus.PENDING.value,
                language=norm_lang,
                canonical_album_key=canon_key,
            )

        year_val = notif.get("year")
        cache_lookup_key = (canon_key, year_val)

        try:
            if cache_lookup_key in catalog_album_cache:
                catalog_match = catalog_album_cache[cache_lookup_key]
            else:
                catalog_match = comparator.find_matching_catalog_album(clean_display, norm_lang, year_val)
                if not catalog_match:
                    # Disambiguate single-song posts whose source URL identifies parent movie album
                    src_u = (notif.get("source_url") or "").lower()
                    m_slug = re.search(r'[a-z0-9\-]+-song-([a-z0-9\-]+)\.html', src_u)
                    if m_slug:
                        parent_candidate = m_slug.group(1).replace("-", " ").strip()
                        parent_match = comparator.find_matching_catalog_album(parent_candidate, norm_lang, year_val)
                        if parent_match:
                            catalog_match = parent_match
                catalog_album_cache[cache_lookup_key] = catalog_match

            if not catalog_match:
                continue

            cat_album_name = catalog_match.get("name") or ""
            if cat_album_name and cat_album_name in catalog_songs_cache:
                catalog_songs = catalog_songs_cache[cat_album_name]
            else:
                catalog_songs = comparator.fetch_catalog_album_songs(catalog_match)
                if cat_album_name:
                    catalog_songs_cache[cat_album_name] = catalog_songs

            if not catalog_songs:
                continue
        except CatalogLookupError as ce:
            deferred_on_catalog_error += 1
            logger.warning(f"Catalog lookup error verifying '{album_name}' ({ce.reason}): {ce}. Preserving pending notification.")
            continue
        except Exception as e:
            deferred_on_catalog_error += 1
            logger.warning(f"Could not verify catalog for '{album_name}': {e}. Preserving pending notification.")
            continue

        # Re-evaluate each song in notification
        from backend.services.catalog import cloud_catalog_service
        changed = False
        for song in songs:
            if song.get("exists_in_catalog"):
                continue

            disc_title = song.get("song_title") or song.get("normalized_title") or ""
            src_filename = _extract_source_filename(song)
            res = cloud_catalog_service.match_song_detailed(
                disc_title,
                catalog_songs,
                source_singer=song.get("singer"),
                source_filename=src_filename,
            )
            best_cat_song = res["matched_song"]

            if res["exists_in_catalog"]:
                song["exists_in_catalog"] = True
                song["catalog_song_title"] = best_cat_song.get("title") if best_cat_song else None
                song["catalog_song_id"] = best_cat_song.get("id") if best_cat_song else None
                song["match_confidence"] = res["confidence"]
                song["match_classification"] = res["classification"]
                song["match_score"] = round(res["score"], 3)
                song["match_reason"] = res["reason"]
                song["tracking_status"] = TrackingStatus.ADDED_TO_MUSICDATA.value
                changed = True
            elif res["classification"] == "POSSIBLE_VERSION_MATCH":
                song["match_confidence"] = MatchConfidence.UNCERTAIN.value
                song["match_classification"] = "POSSIBLE_VERSION_MATCH"
                song["catalog_song_title"] = best_cat_song.get("title") if best_cat_song else None
                song["match_score"] = round(res["score"], 3)
                song["match_reason"] = res["reason"]

        new_missing_count = sum(1 for s in songs if not s.get("exists_in_catalog"))

        if songs and new_missing_count == 0:
            eff_type = (
                orig_type
                if orig_type in (NotificationType.NEW_SONG.value, NotificationType.MISSING_SONGS.value, NotificationType.NEW_ALBUM.value)
                else NotificationType.MISSING_SONGS.value
            )
            new_title, new_message = build_notification_summary_text(
                NotificationType(eff_type) if eff_type in [e.value for e in NotificationType] else NotificationType.MISSING_SONGS,
                clean_display,
                norm_lang,
                songs,
                music_director=notif.get("music_director") or "Unknown",
                year=year_val or 2026,
            )
            _safe_update_resolution(
                db,
                notification_id=notif_id,
                status=NotificationResolutionStatus.COMPLETED.value,
                updated_songs=songs,
                missing_count=0,
                title=new_title,
                message=new_message,
                language=norm_lang,
                canonical_album_key=canon_key,
                is_read=True,
            )
            if hasattr(db, "mark_notification_read"):
                db.mark_notification_read(notif_id)
            resolved_count += 1
        elif changed or new_missing_count < orig_missing_count or (catalog_match and orig_type == NotificationType.NEW_ALBUM.value):
            eff_type_enum = NotificationType.NEW_SONG if new_missing_count <= 1 else NotificationType.MISSING_SONGS
            new_title, new_message = build_notification_summary_text(
                eff_type_enum,
                clean_display,
                norm_lang,
                songs,
                music_director=notif.get("music_director") or "Unknown",
                year=year_val or 2026,
            )
            next_status = (
                NotificationResolutionStatus.PARTIALLY_COMPLETED.value
                if (changed or new_missing_count < orig_missing_count or (notif.get("resolution_status") or "").upper() == "PARTIALLY_COMPLETED")
                else NotificationResolutionStatus.PENDING.value
            )
            _safe_update_resolution(
                db,
                notification_id=notif_id,
                status=next_status,
                updated_songs=songs,
                missing_count=new_missing_count,
                notification_type=eff_type_enum.value,
                title=new_title,
                message=new_message,
                language=norm_lang,
                canonical_album_key=canon_key,
            )
            if changed or new_missing_count < orig_missing_count:
                partially_resolved_count += 1

    return {
        "reconciled_count": len(active_notifications),
        "resolved_notifications": resolved_count,
        "partially_resolved": partially_resolved_count,
        "consolidated_duplicates": consolidated_duplicates,
        "deferred_on_catalog_error": deferred_on_catalog_error,
    }
