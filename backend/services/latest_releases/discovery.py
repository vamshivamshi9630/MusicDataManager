import time
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, Optional, List, Tuple
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
    Supports 20-page paginated discovery, language differentiation, and live reconciliation.
    """

    def __init__(self, db: Optional[LatestReleasesDB] = None, source_client: Optional[SourceClient] = None):
        self.db = db or get_latest_releases_db()
        self.source_client = source_client or SourceClient()

    def _is_real_source_client(self) -> bool:
        return (
            isinstance(self.source_client, SourceClient)
            and type(self.source_client) is SourceClient
            and "fetch_page" not in self.source_client.__dict__
            and "fetch_album_page" not in self.source_client.__dict__
            and "fetch_html" not in self.source_client.__dict__
        )

    def _fetch_and_parse_album(self, cand: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[DiscoveredAlbum], Optional[Exception]]:
        album_url = cand.get("url")
        if not album_url:
            return cand, None, None
        try:
            album_html = self.source_client.fetch_album_page(album_url)
            if not album_html:
                return cand, None, None
            discovered = AlbumParser.parse_album_page(album_html, album_url)
            if not discovered or not discovered.album_name:
                return cand, None, None
            if not discovered.artwork_url and cand.get("thumbnail"):
                discovered.artwork_url = cand.get("thumbnail")
            return cand, discovered, None
        except Exception as err:
            return cand, None, err

    def run_scan(self, locked_by: str = "cron", max_pages: Optional[int] = None, limit_albums: Optional[int] = None) -> Dict[str, Any]:
        """
        Executes a complete discovery scan against naasongs.com.co across paginated indexes (up to max_pages=20).
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
        albums_discovered = 0
        albums_inspected = 0
        new_albums_count = 0
        new_songs_count = 0
        pages_requested = 0
        pages_successfully_checked = 0
        pages_unavailable = 0
        resolved_count = 0
        failed_pages: List[str] = []
        scan_error = None

        pages_limit = max_pages if (max_pages is not None and max_pages > 0) else getattr(settings, "LATEST_RELEASES_MAX_PAGES", 20)
        is_real_client = self._is_real_source_client()

        try:
            logger.info(f"[DISCOVERY] Scan #{scan_id} initiated by '{locked_by}' (max_pages={pages_limit}, limit_albums={limit_albums})...")

            # 2. Paginated crawl of candidate album links
            seen_urls = set()
            all_candidates: List[Dict[str, Any]] = []
            next_page_url: Optional[str] = None
            known_last_page: Optional[int] = None
            consecutive_failures = 0

            for page_num in range(1, pages_limit + 1):
                pages_requested += 1
                try:
                    target_url = next_page_url if (is_real_client and page_num > 1 and next_page_url) else None
                    if is_real_client and target_url:
                        page_html = self.source_client.fetch_page(page_num, page_url=target_url)
                    else:
                        page_html = self.source_client.fetch_page(page_num)

                    if not page_html:
                        pages_unavailable += 1
                        failed_pages.append(f"page/{page_num}")
                        consecutive_failures += 1
                        if page_num == 1:
                            scan_error = "Could not fetch naasongs.com.co homepage (timeout or unreachable)."
                            logger.error(f"[DISCOVERY] {scan_error}")
                            return {
                                "success": False,
                                "status": "FAILED",
                                "error": scan_error,
                                "scan_id": scan_id,
                                "pages_requested": pages_requested,
                                "pages_successfully_checked": pages_successfully_checked,
                                "pages_unavailable": pages_unavailable,
                                "pages_checked": pages_successfully_checked,
                                "albums_discovered": albums_discovered,
                                "albums_inspected": albums_inspected,
                                "albums_found": albums_discovered,
                            }

                        fetch_meta = getattr(self.source_client, "last_fetch_meta", None)
                        http_status = fetch_meta.get("status_code") if isinstance(fetch_meta, dict) else None
                        if http_status in (404, 410):
                            logger.info(f"[DISCOVERY] Reached end of pagination (HTTP {http_status}) at page {page_num}.")
                            break
                        if known_last_page and page_num < known_last_page and consecutive_failures < 3:
                            logger.warning(f"[DISCOVERY] Page {page_num} unavailable (known last page: {known_last_page}); advancing to next page.")
                            next_page_url = None
                            continue
                        logger.info(f"[DISCOVERY] Pagination stopped at unavailable page {page_num}.")
                        break

                    used_url = target_url or (f"{SourceClient.BASE_URL}/" if page_num == 1 else f"{SourceClient.BASE_URL}/page/{page_num}")
                    nav_info = AlbumParser.parse_pagination_info(page_html, current_url=used_url)

                    # WordPress redirects out-of-range /page/N back to homepage (page 1)
                    if page_num > 1 and nav_info.get("pagination_present") and nav_info.get("current_page") == 1:
                        pages_unavailable += 1
                        failed_pages.append(f"page/{page_num}")
                        logger.info(f"[DISCOVERY] Page {page_num} redirected to page 1; reached end of pagination.")
                        break

                    page_candidates = AlbumParser.parse_homepage_albums(page_html)
                    if not page_candidates:
                        pages_unavailable += 1
                        failed_pages.append(f"page/{page_num}")
                        logger.info(f"[DISCOVERY] No album links found on page {page_num}; stopping pagination.")
                        break

                    consecutive_failures = 0
                    pages_successfully_checked += 1
                    known_last_page = nav_info.get("last_page_number") or known_last_page
                    next_page_url = nav_info.get("next_page_url")

                    new_on_page = 0
                    for cand in page_candidates:
                        u = cand.get("url")
                        if u and u not in seen_urls:
                            seen_urls.add(u)
                            all_candidates.append(cand)
                            new_on_page += 1

                    logger.info(
                        f"[DISCOVERY] Page {page_num}: found {len(page_candidates)} links ({new_on_page} new). "
                        f"Total discovered: {len(all_candidates)}"
                    )

                    if limit_albums and limit_albums > 0 and len(all_candidates) >= limit_albums:
                        all_candidates = all_candidates[:limit_albums]
                        logger.info(f"[DISCOVERY] Reached explicit limit_albums={limit_albums}; stopping page crawl.")
                        break

                    if nav_info.get("pagination_present") and nav_info.get("has_next") is False:
                        logger.info(f"[DISCOVERY] Reached final source page ({page_num}); no next-page link.")
                        break

                except Exception as page_err:
                    pages_unavailable += 1
                    failed_pages.append(f"page/{page_num}")
                    consecutive_failures += 1
                    next_page_url = None
                    logger.warning(f"[DISCOVERY] Error scraping page {page_num}: {page_err}")
                    if page_num == 1:
                        scan_error = f"Error scraping homepage: {page_err}"
                        return {
                            "success": False,
                            "status": "FAILED",
                            "error": scan_error,
                            "scan_id": scan_id,
                            "pages_requested": pages_requested,
                            "pages_successfully_checked": pages_successfully_checked,
                            "pages_unavailable": pages_unavailable,
                            "pages_checked": pages_successfully_checked,
                            "albums_discovered": albums_discovered,
                            "albums_inspected": albums_inspected,
                            "albums_found": albums_discovered,
                        }
                    if not (known_last_page and page_num < known_last_page and consecutive_failures < 3):
                        break

            albums_discovered = len(all_candidates)
            comparator = SongComparator()
            album_error_count = 0

            # 3. Fetch & parse candidate albums (bounded concurrency for real HTTP client, sequential for mocks)
            if is_real_client and albums_discovered > 1:
                with ThreadPoolExecutor(max_workers=4) as executor:
                    parsed_entries = list(executor.map(self._fetch_and_parse_album, all_candidates))
            else:
                parsed_entries = [self._fetch_and_parse_album(cand) for cand in all_candidates]

            for cand, discovered, fetch_err in parsed_entries:
                album_url = cand.get("url")
                if fetch_err is not None:
                    album_error_count += 1
                    logger.warning(f"Error processing album {album_url}: {fetch_err}")
                    continue
                if not discovered:
                    continue

                try:
                    # Run comparison against MusicData catalog (incorporates album language)
                    notif_type, analyzed = comparator.compare_album(discovered)
                    albums_inspected += 1

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

            if albums_discovered > 0 and albums_inspected == 0 and album_error_count > 0:
                scan_error = f"All {album_error_count} candidate album(s) failed during catalog comparison."
                logger.error(f"[DISCOVERY] {scan_error}")
                return {
                    "success": False,
                    "status": "FAILED",
                    "scan_id": scan_id,
                    "error": scan_error,
                    "pages_requested": pages_requested,
                    "pages_successfully_checked": pages_successfully_checked,
                    "pages_unavailable": pages_unavailable,
                    "pages_checked": pages_successfully_checked,
                    "albums_discovered": albums_discovered,
                    "albums_inspected": albums_inspected,
                    "albums_found": albums_discovered,
                    "failed_pages": failed_pages,
                }

            # 4. Run Live Reconciliation post-scan
            try:
                recon_res = reconcile_active_notifications(self.db, comparator)
                resolved_count = recon_res.get("resolved_notifications", 0)
                logger.info(f"[DISCOVERY] Reconciliation resolved {resolved_count} notifications.")
            except Exception as recon_err:
                logger.warning(f"[DISCOVERY] Post-scan reconciliation warning: {recon_err}")

            duration = round(time.time() - start_time, 2)
            logger.info(
                f"[DISCOVERY] Scan completed in {duration}s: "
                f"pages(requested={pages_requested}, ok={pages_successfully_checked}, unavail={pages_unavailable}), "
                f"albums(discovered={albums_discovered}, inspected={albums_inspected}), "
                f"new_albums={new_albums_count}, new_songs={new_songs_count}, resolved={resolved_count}."
            )
            return {
                "success": True,
                "status": "COMPLETED",
                "scan_id": scan_id,
                "duration_seconds": duration,
                "pages_requested": pages_requested,
                "pages_successfully_checked": pages_successfully_checked,
                "pages_unavailable": pages_unavailable,
                "pages_checked": pages_successfully_checked,
                "albums_discovered": albums_discovered,
                "albums_inspected": albums_inspected,
                "albums_found": albums_discovered,
                "new_albums_count": new_albums_count,
                "new_songs_count": new_songs_count,
                "resolved_notifications_count": resolved_count,
                "failed_pages": failed_pages,
            }

        except Exception as e:
            scan_error = str(e)
            logger.error(f"[DISCOVERY] Fatal error during scan: {e}")
            return {
                "success": False,
                "status": "FAILED",
                "scan_id": scan_id,
                "error": scan_error,
                "pages_requested": pages_requested,
                "pages_successfully_checked": pages_successfully_checked,
                "pages_unavailable": pages_unavailable,
                "pages_checked": pages_successfully_checked,
                "albums_discovered": albums_discovered,
                "albums_inspected": albums_inspected,
                "albums_found": albums_discovered,
                "failed_pages": failed_pages,
            }

        finally:
            self.db.record_scan_complete(
                scan_id=scan_id,
                albums_found=albums_discovered,
                new_albums=new_albums_count,
                new_songs=new_songs_count,
                pages_checked=pages_successfully_checked,
                pages_requested=pages_requested,
                pages_successfully_checked=pages_successfully_checked,
                pages_unavailable=pages_unavailable,
                albums_discovered=albums_discovered,
                albums_inspected=albums_inspected,
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
