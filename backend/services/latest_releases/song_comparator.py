import os
import re
import logging
import urllib.parse
from typing import List, Dict, Any, Optional, Tuple
from backend.services.fuzzy_search import normalize_text, calculate_similarity_ratio
from backend.services.catalog import cloud_catalog_service, CatalogLookupError
from backend.services.latest_releases.models import (
    NotificationType,
    TrackingStatus,
    MatchConfidence,
    MatchClassification,
    DiscoveredAlbum,
    DiscoveredSong
)

logger = logging.getLogger("musicdata.latest_releases.song_comparator")

def is_cloud_mode() -> bool:
    return os.environ.get("CLOUD_MODE", "").strip().lower() in ("1", "true")

class SongComparator:
    """
    Compares discovered albums and individual songs against the authoritative MusicData catalog.
    Evaluates incremental song releases with confidence scores:
      - EXACT (1.0) / HIGH (>= 0.88): Automatically matched to existing catalog track.
      - UNCERTAIN (0.65 - 0.87): Flagged for user verification.
      - NONE (< 0.65): Missing track.
    """

    HIGH_CONFIDENCE_THRESHOLD = 0.88
    UNCERTAIN_CONFIDENCE_THRESHOLD = 0.65

    def __init__(self, catalog_albums: Optional[List[Dict[str, Any]]] = None):
        self._catalog_albums = catalog_albums

    def get_catalog_albums(self) -> List[Dict[str, Any]]:
        if self._catalog_albums is not None:
            return self._catalog_albums
        try:
            return cloud_catalog_service.get_albums_index()
        except Exception as e:
            logger.warning(f"Failed to fetch catalog albums: {e}")
            return []

    def find_matching_catalog_album(
        self,
        discovered_album_name: str,
        discovered_language: Optional[str] = None,
        discovered_year: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Finds matching album in MusicData catalog using canonical album matching.
        Strictly enforces language verification and release year sanity.
        """
        return cloud_catalog_service.find_canonical_album(
            query_name=discovered_album_name,
            query_language=discovered_language,
            query_year=discovered_year,
            candidate_albums=self._catalog_albums
        )

    @staticmethod
    def normalize_song_title(title: str) -> str:
        return cloud_catalog_service.normalize_song_title(title)

    def fetch_catalog_album_songs(self, catalog_album: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Fetches the authoritative individual songs for a catalog album.
        In Cloud Mode, loads metadata/albums/{partition}/{albumName}.json directly.
        Raises CatalogLookupError if fetch fails (never silently returns empty list).
        """
        album_name = catalog_album.get("name", "")
        if not album_name:
            return []

        # Use local repository only when explicitly configured via MUSICDATA_REPOSITORY_ROOT in Local Mode
        explicit_local_root = os.environ.get("MUSICDATA_REPOSITORY_ROOT", "").strip()
        if not is_cloud_mode() and explicit_local_root:
            try:
                from backend.core.repository import RepositoryContext
                rc = RepositoryContext(root_override=explicit_local_root)
                album_dir = rc.get_album_path(album_name)
                if album_dir.exists():
                    songs = []
                    for mp3 in sorted(list(album_dir.glob("*.mp3"))):
                        songs.append({
                            "title": mp3.stem,
                            "filename": mp3.name,
                            "audio": mp3.name,
                            "id": rc.get_stable_id(f"{album_name}{mp3.name}")
                        })
                    if songs:
                        return songs
            except Exception:
                pass

        # In Cloud Mode or fallback: fetch authoritative JSON from GitHub
        data = cloud_catalog_service.get_album_metadata(catalog_album)
        return data.get("songs", [])

    def compare_album(
        self,
        discovered: DiscoveredAlbum
    ) -> Tuple[Optional[NotificationType], DiscoveredAlbum]:
        """
        Performs album-level and incremental song-level comparison.
        Updates discovered album and songs with match data and returns appropriate NotificationType (or None).
        Raises CatalogLookupError if authoritative catalog cannot be verified.
        """
        catalog_match = self.find_matching_catalog_album(
            discovered.album_name,
            discovered.language,
            discovered.year
        )

        if not catalog_match:
            # =================================================================
            # CASE A: Album does NOT exist in MusicData Catalog
            # =================================================================
            discovered.exists_in_catalog = False
            discovered.catalog_album_name = None
            discovered.tracking_status = TrackingStatus.DISCOVERED

            for song in discovered.songs:
                song.exists_in_catalog = False
                song.catalog_song_title = None
                song.catalog_song_id = None
                song.match_confidence = MatchConfidence.NONE
                song.match_classification = MatchClassification.MISSING
                song.match_score = 0.0
                song.tracking_status = TrackingStatus.MISSING_FROM_MUSICDATA

            return NotificationType.NEW_ALBUM, discovered

        # =====================================================================
        # CASE B: Album ALREADY exists in MusicData Catalog
        # =====================================================================
        discovered.exists_in_catalog = True
        discovered.catalog_album_name = catalog_match.get("name")
        discovered.tracking_status = TrackingStatus.ADDED_TO_MUSICDATA

        catalog_songs = self.fetch_catalog_album_songs(catalog_match)
        missing_songs: List[DiscoveredSong] = []

        for disc_song in discovered.songs:
            src_filename = None
            if disc_song.source_audio_url:
                src_filename = urllib.parse.unquote(disc_song.source_audio_url.split("/")[-1].split("?")[0])

            res = cloud_catalog_service.match_song_detailed(
                source_title=disc_song.song_title or disc_song.normalized_title,
                catalog_songs=catalog_songs,
                source_singer=getattr(disc_song, "singer", None),
                source_filename=src_filename
            )
            best_cat_song = res["matched_song"]
            classification = res["classification"]
            conf_str = res["confidence"]
            disc_song.match_score = round(res["score"], 3)
            disc_song.match_reason = res["reason"]
            disc_song.catalog_version_candidates = [
                c.get("title") for c in res.get("version_candidates", []) if c.get("title")
            ]

            if classification == "EXACT_MATCH":
                disc_song.exists_in_catalog = True
                disc_song.catalog_song_title = best_cat_song.get("title") if best_cat_song else None
                disc_song.catalog_song_id = best_cat_song.get("id") if best_cat_song else None
                disc_song.match_confidence = MatchConfidence.EXACT if conf_str == "EXACT" else MatchConfidence.HIGH
                disc_song.match_classification = MatchClassification.EXACT_MATCH
                disc_song.tracking_status = TrackingStatus.ADDED_TO_MUSICDATA
            elif classification == "POSSIBLE_VERSION_MATCH":
                disc_song.exists_in_catalog = False
                disc_song.catalog_song_title = best_cat_song.get("title") if best_cat_song else None
                disc_song.catalog_song_id = best_cat_song.get("id") if best_cat_song else None
                disc_song.match_confidence = MatchConfidence.UNCERTAIN
                disc_song.match_classification = MatchClassification.POSSIBLE_VERSION_MATCH
                disc_song.tracking_status = TrackingStatus.DOWNLOAD_SUGGESTED
                missing_songs.append(disc_song)
            elif classification == "UNCERTAIN":
                disc_song.exists_in_catalog = False
                disc_song.catalog_song_title = best_cat_song.get("title") if best_cat_song else None
                disc_song.catalog_song_id = best_cat_song.get("id") if best_cat_song else None
                disc_song.match_confidence = MatchConfidence.UNCERTAIN
                disc_song.match_classification = MatchClassification.UNCERTAIN
                disc_song.tracking_status = TrackingStatus.DOWNLOAD_SUGGESTED
                missing_songs.append(disc_song)
            else:
                disc_song.exists_in_catalog = False
                disc_song.catalog_song_title = None
                disc_song.catalog_song_id = None
                disc_song.match_confidence = MatchConfidence.NONE
                disc_song.match_classification = MatchClassification.MISSING
                disc_song.tracking_status = TrackingStatus.MISSING_FROM_MUSICDATA
                missing_songs.append(disc_song)

        # Categorize notification based on missing songs
        num_missing = len(missing_songs)

        if num_missing == 1:
            return NotificationType.NEW_SONG, discovered
        elif num_missing > 1:
            return NotificationType.MISSING_SONGS, discovered
        else:
            # All discovered songs are already present in catalog
            # Check if artwork was updated
            has_cat_art = bool(catalog_match.get("image"))
            if discovered.artwork_url and not has_cat_art:
                return NotificationType.ALBUM_UPDATED, discovered
            return None, discovered
