import time
import logging
from typing import Dict, Any, Optional, List
from backend.services.latest_releases.models import (
    NotificationType,
    TrackingStatus,
    MatchConfidence,
    DiscoveredAlbum,
    DiscoveredSong
)
from backend.services.latest_releases.db import get_latest_releases_db, LatestReleasesDB
from backend.services.latest_releases.source_client import SourceClient
from backend.services.latest_releases.album_parser import AlbumParser
from backend.services.latest_releases.song_comparator import SongComparator

logger = logging.getLogger("musicdata.latest_releases.discovery")

class LatestReleasesDiscoveryEngine:
    """
    Main orchestration engine for Latest Releases Discovery.
    Safe, rate-limited, distributed-lock protected, and database backed.
    """

    def __init__(self, db: Optional[LatestReleasesDB] = None, source_client: Optional[SourceClient] = None):
        self.db = db or get_latest_releases_db()
        self.source_client = source_client or SourceClient()

    def run_scan(self, locked_by: str = "cron", limit_albums: int = 15) -> Dict[str, Any]:
        """
        Executes a complete discovery scan against naasongs.com.co.
        Protected by database-backed distributed lock.
        """
        # 1. Acquire Distributed Scan Lock
        if not self.db.acquire_scan_lock(locked_by=locked_by, timeout_minutes=15):
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
        scan_error = None

        try:
            logger.info(f"[DISCOVERY] Scan #{scan_id} initiated by '{locked_by}'...")
            
            # 2. Fetch Homepage
            hp_html = self.source_client.fetch_homepage()
            if not hp_html:
                scan_error = "Could not fetch naasongs.com.co homepage (timeout or unreachable)."
                logger.error(f"[DISCOVERY] {scan_error}")
                self._record_scan_failed_notification(scan_error)
                return {
                    "success": False,
                    "status": "FAILED",
                    "error": scan_error,
                    "scan_id": scan_id
                }

            # 3. Parse candidate albums from homepage
            candidates = AlbumParser.parse_homepage_albums(hp_html)
            albums_found_count = len(candidates)
            logger.info(f"[DISCOVERY] Found {albums_found_count} candidate album links on homepage.")

            comparator = SongComparator()

            # 4. Process each candidate album up to limit
            for cand in candidates[:limit_albums]:
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

                    # Run comparison against MusicData catalog
                    notif_type, analyzed = comparator.compare_album(discovered)

                    # Save album and songs in DB
                    album_id = self.db.upsert_discovered_album(analyzed)
                    analyzed.id = album_id

                    for song in analyzed.songs:
                        self.db.upsert_discovered_song(album_id, song)

                    # Create Notification if applicable
                    if notif_type:
                        missing_songs = [s for s in analyzed.songs if not s.exists_in_catalog]
                        missing_count = len(missing_songs)

                        # Format songs details list for UI rendering
                        songs_payload = []
                        for s in analyzed.songs:
                            songs_payload.append({
                                "song_title": s.song_title,
                                "exists_in_catalog": s.exists_in_catalog,
                                "match_confidence": s.match_confidence.value,
                                "match_score": s.match_score,
                                "source_audio_url": s.source_audio_url,
                                "tracking_status": s.tracking_status.value
                            })

                        # Construct clean notification title & message
                        if notif_type == NotificationType.NEW_ALBUM:
                            title = f"New Album: {analyzed.album_name}"
                            message = f"{len(analyzed.songs)} songs available • Music by {analyzed.music_director} ({analyzed.year})"
                            new_albums_count += 1
                        elif notif_type == NotificationType.NEW_SONG:
                            missing_title = missing_songs[0].song_title if missing_songs else "New Track"
                            title = f"New Song: {analyzed.album_name}"
                            message = f"'{missing_title}' released • Missing from MusicData"
                            new_songs_count += 1
                        elif notif_type == NotificationType.MISSING_SONGS:
                            title = f"Missing Songs: {analyzed.album_name}"
                            names_snippet = ", ".join([s.song_title for s in missing_songs[:2]])
                            if len(missing_songs) > 2:
                                names_snippet += f" and {len(missing_songs) - 2} more"
                            message = f"{missing_count} songs missing ({names_snippet})"
                            new_songs_count += missing_count
                        elif notif_type == NotificationType.ALBUM_UPDATED:
                            title = f"Album Updated: {analyzed.album_name}"
                            message = f"New artwork or updated tracklist discovered."
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
                            missing_count=missing_count
                        )
                except Exception as album_err:
                    logger.warning(f"Error processing album {album_url}: {album_err}")

            duration = round(time.time() - start_time, 2)
            logger.info(f"[DISCOVERY] Scan completed in {duration}s: {albums_found_count} checked, {new_albums_count} new albums, {new_songs_count} new songs.")
            return {
                "success": True,
                "status": "COMPLETED",
                "scan_id": scan_id,
                "duration_seconds": duration,
                "albums_found": albums_found_count,
                "new_albums_count": new_albums_count,
                "new_songs_count": new_songs_count
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
                missing_count=0
            )
        except Exception:
            pass
