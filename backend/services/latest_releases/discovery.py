import time
import logging
from typing import Dict, Any, Optional, List
from backend.core.config import settings
from backend.services.latest_releases.models import (
    NotificationType,
    TrackingStatus,
    MatchConfidence,
    DiscoveredAlbum,
    DiscoveredSong,
    NotificationResolutionStatus
)
from backend.services.latest_releases.db import get_latest_releases_db, LatestReleasesDB
from backend.services.latest_releases.source_client import SourceClient
from backend.services.latest_releases.album_parser import AlbumParser
from backend.services.latest_releases.song_comparator import SongComparator
from backend.services.latest_releases.reconciliation import reconcile_active_notifications

logger = logging.getLogger("musicdata.latest_releases.discovery")

class LatestReleasesDiscoveryEngine:
    """
    Main orchestration engine for Latest Releases Discovery.
    Safe, rate-limited, distributed-lock protected, and database backed.
    Supports 15-20 page paginated discovery, language differentiation, and live reconciliation.
    """

    def __init__(self, db: Optional[LatestReleasesDB] = None, source_client: Optional[SourceClient] = None):
        self.db = db or get_latest_releases_db()
        self.source_client = source_client or SourceClient()

    def run_scan(self, locked_by: str = "cron", max_pages: Optional[int] = None, limit_albums: Optional[int] = None) -> Dict[str, Any]:
        """
        Executes a complete discovery scan against naasongs.com.co across paginated indexes (pages 1 to 20).
        Protected by database-backed distributed lock.
        """
        # 1. Acquire Distributed Scan Lock
        if not self.db.acquire_scan_lock(locked_by=locked_by, timeout_minutes=25):
            lock_info = self.db.get_lock_status()
            logger.info(f"Scan rejected: another scan is active (locked by {lock_info.get('locked_by')}).")
            return {
                "success": False,
                "status": "LOCKED",
                "message": f"Another scan is already in progress (locked by {lock_info.get('locked_by')}).",
                "lock_info": lock_info
            }

        scan_id = self.db.record_scan_start(locked_by=locked_by)
        start_time = time.time()
        albums_found_count = 0
        new_albums_count = 0
        new_songs_count = 0
        pages_checked = 0
        resolved_count = 0
        failed_pages: List[str] = []
        scan_error = None

        pages_limit = max_pages or getattr(settings, "LATEST_RELEASES_MAX_PAGES", 20)

        try:
            logger.info(f"[DISCOVERY] Scan #{scan_id} initiated by '{locked_by}' (target up to {pages_limit} pages)...")

            # 2. Paginated crawl of candidate album links
            seen_urls = set()
            all_candidates: List[Dict[str, Any]] = []

            for page_num in range(1, pages_limit + 1):
                pages_checked += 1
                try:
                    page_html = self.source_client.fetch_page(page_num)
                    if not page_html:
                        if page_num == 1:
                            scan_error = "Could not fetch naasongs.com.co homepage (timeout or unreachable)."
                            logger.error(f"[DISCOVERY] {scan_error}")
                            return {
                                "success": False,
                                "status": "FAILED",
                                "error": scan_error,
                                "scan_id": scan_id
                            }
                        else:
                            # Reached end of pagination (e.g. 404 on page 18)
                            logger.info(f"[DISCOVERY] Reached end of pagination at page {page_num}.")
                            break

                    page_candidates = AlbumParser.parse_homepage_albums(page_html)
                    if not page_candidates:
                        logger.info(f"[DISCOVERY] No album links found on page {page_num}; stopping pagination.")
                        break

                    new_on_page = 0
                    for cand in page_candidates:
                        u = cand.get("url")
                        if u and u not in seen_urls:
                            seen_urls.add(u)
                            all_candidates.append(cand)
                            new_on_page += 1

                    logger.info(f"[DISCOVERY] Page {page_num}: found {len(page_candidates)} links ({new_on_page} new). Total candidates: {len(all_candidates)}")

                    if limit_albums and len(all_candidates) >= limit_albums:
                        all_candidates = all_candidates[:limit_albums]
                        break

                except Exception as page_err:
                    logger.warning(f"[DISCOVERY] Error scraping page {page_num}: {page_err}")
                    failed_pages.append(f"page/{page_num}")

            albums_found_count = len(all_candidates)
            comparator = SongComparator()
            processed_albums_count = 0
            album_error_count = 0

            # 3. Process candidate albums
            for cand in all_candidates:
                album_url = cand.get("url")
                if not album_url:
                    continue

                try:
                    album_html = self.source_client.fetch_album_page(album_url)
                    if not album_html:
                        continue

                    discovered = AlbumParser.parse_album_page(album_html, album_url)
                    if not discovered or not discovered.album_name:
                        continue

                    # If candidate has a thumbnail and discovered doesn't, inherit it
                    if not discovered.artwork_url and cand.get("thumbnail"):
                        discovered.artwork_url = cand.get("thumbnail")

                    # Run comparison against MusicData catalog (incorporates album language)
                    notif_type, analyzed = comparator.compare_album(discovered)
                    processed_albums_count += 1

                    # Save album and songs in DB
                    album_id = self.db.upsert_discovered_album(analyzed)
                    analyzed.id = album_id

                    for song in analyzed.songs:
                        song_id = self.db.upsert_discovered_song(album_id, song)
                        song.id = song_id

                    # Create Notification if applicable
                    if notif_type:
                        missing_songs = [s for s in analyzed.songs if not s.exists_in_catalog]
                        missing_count = len(missing_songs)
                        version_verify_songs = [
                            s for s in missing_songs
                            if getattr(s.match_classification, "value", s.match_classification) == "POSSIBLE_VERSION_MATCH"
                            or s.match_confidence == MatchConfidence.UNCERTAIN
                        ]
                        genuine_missing_songs = [s for s in missing_songs if s not in version_verify_songs]

                        # Format songs details list for UI rendering
                        songs_payload = []
                        for s in analyzed.songs:
                            cls_val = s.match_classification.value if hasattr(s.match_classification, "value") else str(s.match_classification)
                            songs_payload.append({
                                "id": s.id,
                                "song_title": s.song_title,
                                "track_number": s.track_number,
                                "singer": getattr(s, "singer", None),
                                "available_bitrate": getattr(s, "available_bitrate", None),
                                "available_bitrates": getattr(s, "available_bitrates", []),
                                "download_links": getattr(s, "download_links", {}),
                                "source_page_url": getattr(s, "source_page_url", None) or analyzed.source_url,
                                "link_available": bool(getattr(s, "link_available", False) or s.source_audio_url),
                                "download_authorized": bool(getattr(s, "download_authorized", False)),
                                "exists_in_catalog": s.exists_in_catalog,
                                "match_confidence": s.match_confidence.value,
                                "match_classification": cls_val,
                                "match_score": s.match_score,
                                "match_reason": getattr(s, "match_reason", None),
                                "catalog_song_title": s.catalog_song_title,
                                "catalog_song_id": s.catalog_song_id,
                                "catalog_version_candidates": getattr(s, "catalog_version_candidates", []),
                                "source_audio_url": s.source_audio_url,
                                "tracking_status": s.tracking_status.value
                            })

                        # Construct clean notification title & message
                        lang_badge = f"[{analyzed.language}] " if analyzed.language else ""
                        if notif_type == NotificationType.NEW_ALBUM:
                            title = f"New Album: {analyzed.album_name}"
                            message = f"{lang_badge}{len(analyzed.songs)} songs available • Music by {analyzed.music_director} ({analyzed.year})"
                            new_albums_count += 1
                        elif notif_type == NotificationType.NEW_SONG:
                            missing_title = missing_songs[0].song_title if missing_songs else "New Track"
                            title = f"New Song: {analyzed.album_name}"
                            if version_verify_songs:
                                message = f"{lang_badge}'{missing_title}' needs version verification against MusicData"
                            else:
                                message = f"{lang_badge}'{missing_title}' released • Missing from MusicData"
                            new_songs_count += 1
                        elif notif_type == NotificationType.MISSING_SONGS:
                            title = f"Missing Songs: {analyzed.album_name}"
                            if version_verify_songs and genuine_missing_songs:
                                names_snippet = ", ".join([s.song_title for s in genuine_missing_songs[:2]])
                                if len(genuine_missing_songs) > 2:
                                    names_snippet += f" and {len(genuine_missing_songs) - 2} more"
                                message = f"{lang_badge}{len(genuine_missing_songs)} missing ({names_snippet}) • {len(version_verify_songs)} version match to verify"
                            elif version_verify_songs and not genuine_missing_songs:
                                message = f"{lang_badge}{len(version_verify_songs)} track(s) need version verification"
                            else:
                                names_snippet = ", ".join([s.song_title for s in missing_songs[:2]])
                                if len(missing_songs) > 2:
                                    names_snippet += f" and {len(missing_songs) - 2} more"
                                message = f"{lang_badge}{missing_count} songs missing ({names_snippet})"
                            new_songs_count += missing_count
                        elif notif_type == NotificationType.ALBUM_UPDATED:
                            title = f"Album Updated: {analyzed.album_name}"
                            message = f"{lang_badge}New artwork or updated tracklist discovered."
                        else:
                            title = f"Catalog Update: {analyzed.album_name}"
                            message = "New details discovered."

                        self.db.create_or_update_notification(
                            album_id=album_id,
                            notification_type=notif_type,
                            title=title,
                            message=message,
                            album_name=analyzed.album_name,
                            artwork_url=analyzed.artwork_url,
                            music_director=analyzed.music_director,
                            year=analyzed.year,
                            source_url=analyzed.source_url,
                            songs_list=songs_payload,
                            missing_count=missing_count,
                            language=analyzed.language,
                            resolution_status=NotificationResolutionStatus.PENDING.value
                        )
                except Exception as album_err:
                    album_error_count += 1
                    logger.warning(f"Error processing album {album_url}: {album_err}")

            if albums_found_count > 0 and processed_albums_count == 0 and album_error_count > 0:
                scan_error = f"All {album_error_count} candidate album(s) failed during catalog comparison."
                logger.error(f"[DISCOVERY] {scan_error}")
                return {
                    "success": False,
                    "status": "FAILED",
                    "scan_id": scan_id,
                    "error": scan_error
                }

            # 4. Run Live Reconciliation post-scan
            try:
                recon_res = reconcile_active_notifications(self.db, comparator)
                resolved_count = recon_res.get("resolved_notifications", 0)
                logger.info(f"[DISCOVERY] Reconciliation resolved {resolved_count} notifications.")
            except Exception as recon_err:
                logger.warning(f"[DISCOVERY] Post-scan reconciliation warning: {recon_err}")

            duration = round(time.time() - start_time, 2)
            logger.info(f"[DISCOVERY] Scan completed in {duration}s: {albums_found_count} checked across {pages_checked} pages, {new_albums_count} new albums, {new_songs_count} new songs, {resolved_count} resolved.")
            return {
                "success": True,
                "status": "COMPLETED",
                "scan_id": scan_id,
                "duration_seconds": duration,
                "pages_checked": pages_checked,
                "albums_found": albums_found_count,
                "new_albums_count": new_albums_count,
                "new_songs_count": new_songs_count,
                "resolved_notifications_count": resolved_count
            }

        except Exception as e:
            scan_error = str(e)
            logger.error(f"[DISCOVERY] Fatal error during scan: {e}")
            return {
                "success": False,
                "status": "FAILED",
                "scan_id": scan_id,
                "error": scan_error
            }

        finally:
            self.db.record_scan_complete(
                scan_id=scan_id,
                albums_found=albums_found_count,
                new_albums=new_albums_count,
                new_songs=new_songs_count,
                pages_checked=pages_checked,
                resolved_notifications=resolved_count,
                failed_pages=failed_pages,
                error_message=scan_error
            )
            self.db.release_scan_lock(locked_by=locked_by)

    def _record_scan_failed_notification(self, error_message: str):
        try:
            self.db.create_or_update_notification(
                album_id=0,
                notification_type=NotificationType.SCAN_FAILED,
                title="Scan Failed",
                message=error_message,
                album_name="Discovery Engine",
                artwork_url=None,
                music_director="System",
                year=2026,
                source_url="https://naasongs.com.co",
                songs_list=[],
                missing_count=0,
                language="Unknown",
                resolution_status=NotificationResolutionStatus.PENDING.value
            )
        except Exception:
            pass
