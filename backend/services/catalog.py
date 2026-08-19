import os
import json
import urllib.request
from typing import List, Dict, Any, Optional
from backend.core.config import settings
from backend.services.fuzzy_search import search_albums_fuzzy, check_near_duplicate_album

class CloudCatalogService:
    """Fetches and caches dynamic catalog metadata directly from GitHub in Cloud Mode."""

    def __init__(self, owner: Optional[str] = None, repo: Optional[str] = None, branch: Optional[str] = None):
        self.owner = owner or settings.GITHUB_OWNER
        self.repo = repo or settings.GITHUB_REPOSITORY
        self.branch = branch or settings.GITHUB_BRANCH
        self.raw_base_url = f"https://raw.githubusercontent.com/{self.owner}/{self.repo}/{self.branch}"
        self._stats_cache: Optional[Dict[str, Any]] = None
        self._albums_index_cache: Optional[List[Dict[str, Any]]] = None
        self._artists_index_cache: Optional[List[Dict[str, Any]]] = None

    def _fetch_json(self, path: str) -> Any:
        url = f"{self.raw_base_url}/{path}"
        req = urllib.request.Request(url, headers={"User-Agent": "MusicData-Manager-Cloud/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get_statistics(self) -> Dict[str, Any]:
        try:
            raw_stats = self._fetch_json("metadata/statistics.json")
            albums_idx = self.get_albums_index()
            png_count = sum(1 for a in albums_idx if a.get("image"))
            return {
                "albums": raw_stats.get("totalAlbums", len(albums_idx)),
                "songs": raw_stats.get("totalSongs", 0),
                "png_artwork": png_count,
                "zero_byte_shield": "Active",
                "mode": "CLOUD"
            }
        except Exception:
            albums_idx = self.get_albums_index()
            total_songs = sum(a.get("songCount", 0) for a in albums_idx)
            png_count = sum(1 for a in albums_idx if a.get("image"))
            return {
                "albums": len(albums_idx),
                "songs": total_songs,
                "png_artwork": png_count,
                "zero_byte_shield": "Active",
                "mode": "CLOUD"
            }

    def get_albums_index(self) -> List[Dict[str, Any]]:
        if self._albums_index_cache is not None:
            return self._albums_index_cache
        try:
            idx = self._fetch_json("metadata/indexes/albums.json")
            if isinstance(idx, list):
                self._albums_index_cache = idx
                return idx
        except Exception as e:
            print(f"[CloudCatalogService] Failed to fetch albums index: {e}")
        return []

    def get_albums_list(self) -> Dict[str, Any]:
        idx = self.get_albums_index()
        result = []
        for item in idx:
            result.append({
                "name": item.get("name", "Unknown"),
                "musicDirector": item.get("artist") or item.get("musicDirector") or "Unknown",
                "year": item.get("year", 2026),
                "songCount": item.get("songCount", 0),
                "hasArtwork": bool(item.get("image"))
            })
        return {"total_albums": len(result), "albums": result}

    def search_albums(self, query: str) -> Dict[str, Any]:
        """Performs ranked generic fuzzy search over all catalog albums."""
        idx = self.get_albums_index()
        matches = search_albums_fuzzy(query, idx, limit=10)
        formatted = []
        for item in matches:
            formatted.append({
                "name": item.get("name", "Unknown"),
                "musicDirector": item.get("artist") or item.get("musicDirector") or "Unknown",
                "year": item.get("year", 2026),
                "songCount": item.get("songCount", 0),
                "hasArtwork": bool(item.get("image")),
                "match_score": item.get("match_score", 0.0)
            })
        return {"query": query, "total_matches": len(formatted), "suggestions": formatted}

    def check_duplicate_album(self, album_name: str) -> Dict[str, Any]:
        """Backend near-duplicate check against cloud catalog."""
        idx = self.get_albums_index()
        existing_names = [a.get("name", "") for a in idx if a.get("name")]
        return check_near_duplicate_album(album_name, existing_names)

    def match_music_director(self, query: str) -> Dict[str, List[str]]:
        if not query or not query.strip():
            return {"suggestions": []}
        
        q_lower = query.strip().lower()
        if self._artists_index_cache is None:
            try:
                artists_data = self._fetch_json("metadata/indexes/artists.json")
                if isinstance(artists_data, list):
                    self._artists_index_cache = artists_data
                else:
                    self._artists_index_cache = []
            except Exception:
                self._artists_index_cache = []

        matches = []
        for artist_entry in self._artists_index_cache:
            name = artist_entry.get("name", "")
            if name and q_lower in name.lower():
                matches.append(name)

        matches = sorted(list(set(matches)), key=lambda s: s.lower())[:10]
        return {"suggestions": matches}

cloud_catalog_service = CloudCatalogService()
