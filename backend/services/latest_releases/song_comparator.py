import re
import logging
from typing import List, Dict, Any, Optional, Tuple
from backend.services.fuzzy_search import normalize_text, calculate_similarity_ratio
from backend.services.catalog import cloud_catalog_service
from backend.services.latest_releases.models import (
    NotificationType,
    TrackingStatus,
    MatchConfidence,
    DiscoveredAlbum,
    DiscoveredSong
)

logger = logging.getLogger("musicdata.latest_releases.song_comparator")

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

    def find_matching_catalog_album(self, discovered_album_name: str, discovered_language: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Finds matching album in MusicData catalog using exact and high-confidence normalized match.
        If discovered_language is provided and catalog specifies language, requires language compatibility.
        """
        norm_disc = normalize_text(discovered_album_name)
        if not norm_disc:
            return None

        catalog = self.get_catalog_albums()

        def language_compatible(cat_alb: Dict[str, Any]) -> bool:
            if not discovered_language:
                return True
            cat_lang = cat_alb.get("language")
            if not cat_lang:
                return True
            return str(cat_lang).strip().lower() == str(discovered_language).strip().lower()

        # 1. Exact or exact-normalized match
        for alb in catalog:
            cat_name = alb.get("name", "")
            if normalize_text(cat_name) == norm_disc and language_compatible(alb):
                return alb

        # 2. High similarity match (e.g. >= 0.90)
        best_match = None
        best_score = 0.0
        for alb in catalog:
            if not language_compatible(alb):
                continue
            cat_name = alb.get("name", "")
            score = calculate_similarity_ratio(norm_disc, normalize_text(cat_name))
            if score > best_score:
                best_score = score
                best_match = alb

        if best_score >= 0.90:
            return best_match

        return None

    def fetch_catalog_album_songs(self, catalog_album: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Fetches the individual songs for a catalog album.
        In Cloud Mode, loads metadata/albums/{partition}/{albumName}.json.
        """
        album_name = catalog_album.get("name", "")
        partition = catalog_album.get("partition", "")
        if not partition and album_name:
            from generator.utils import get_album_partition
            partition = get_album_partition(album_name)

        if not partition or not album_name:
            return []

        try:
            path = f"metadata/albums/{partition}/{album_name}.json"
            album_data = cloud_catalog_service._fetch_json(path)
            if isinstance(album_data, dict):
                return album_data.get("songs", [])
        except Exception as e:
            logger.debug(f"Could not fetch songs JSON for {album_name}: {e}")

        return []

    @staticmethod
    def normalize_song_title(title: str) -> str:
        norm = normalize_text(title)
        norm = re.sub(r'\b(song|songs|audio|video|full song|lyrical)\b', '', norm).strip()
        norm = re.sub(r'\s+', ' ', norm).strip()
        return norm

    def compare_album(
        self,
        discovered: DiscoveredAlbum
    ) -> Tuple[Optional[NotificationType], DiscoveredAlbum]:
        """
        Performs album-level and incremental song-level comparison.
        Updates discovered album and songs with match data and returns appropriate NotificationType (or None).
        """
        catalog_match = self.find_matching_catalog_album(discovered.album_name, discovered.language)

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
            best_cat_song = None
            best_score = 0.0
            clean_disc = self.normalize_song_title(disc_song.song_title or disc_song.normalized_title)

            for cat_song in catalog_songs:
                cat_title = cat_song.get("title") or cat_song.get("normalizedTitle") or ""
                clean_cat = self.normalize_song_title(cat_title)
                score1 = calculate_similarity_ratio(clean_disc, clean_cat)
                score2 = calculate_similarity_ratio(disc_song.normalized_title, normalize_text(cat_title))
                score = max(score1, score2)
                if score > best_score:
                    best_score = score
                    best_cat_song = cat_song

            disc_song.match_score = round(best_score, 3)

            if best_score >= self.HIGH_CONFIDENCE_THRESHOLD:
                # High-confidence match: Song already exists in catalog
                disc_song.exists_in_catalog = True
                disc_song.catalog_song_title = best_cat_song.get("title") if best_cat_song else None
                disc_song.catalog_song_id = best_cat_song.get("id") if best_cat_song else None
                disc_song.match_confidence = MatchConfidence.EXACT if best_score == 1.0 else MatchConfidence.HIGH
                disc_song.tracking_status = TrackingStatus.ADDED_TO_MUSICDATA
            elif best_score >= self.UNCERTAIN_CONFIDENCE_THRESHOLD:
                # Uncertain match: Flagged for user review
                disc_song.exists_in_catalog = False
                disc_song.catalog_song_title = best_cat_song.get("title") if best_cat_song else None
                disc_song.catalog_song_id = best_cat_song.get("id") if best_cat_song else None
                disc_song.match_confidence = MatchConfidence.UNCERTAIN
                disc_song.tracking_status = TrackingStatus.DOWNLOAD_SUGGESTED
                missing_songs.append(disc_song)
            else:
                # Missing song
                disc_song.exists_in_catalog = False
                disc_song.catalog_song_title = None
                disc_song.catalog_song_id = None
                disc_song.match_confidence = MatchConfidence.NONE
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
