import json
import re
from pathlib import Path
from typing import List, Dict, Optional, Set
from backend.core.repository import IRepositoryProvider

class MetadataService:
    def __init__(self, repo_context: IRepositoryProvider):
        self.repo = repo_context
        self._cached_directors: Optional[List[str]] = None

    @staticmethod
    def normalize_string(val: str) -> str:
        if not val:
            return ""
        text = val.lower().replace("–", "-").replace("_", " ")
        text = re.sub(r'[^a-z0-9\s]', '', text)
        return " ".join(text.split())

    def get_canonical_directors(self, force_refresh: bool = False) -> List[str]:
        if self._cached_directors and not force_refresh:
            return self._cached_directors

        directors_set: Set[str] = set()

        # Source 1: All album_info.json files
        for album_dir in self.repo.list_all_album_directories():
            info_file = album_dir / "album_info.json"
            if info_file.exists():
                try:
                    with open(info_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        md = data.get("musicDirector", "").strip() if data.get("musicDirector") else ""
                        if md and md != "Unknown":
                            directors_set.add(md)
                except Exception:
                    pass

        # Source 2: MusicDirectorImages folder
        images_dir = self.repo.root / "MusicDirectorImages"
        if images_dir.exists():
            for img in images_dir.glob("*.png"):
                name = img.stem.strip()
                if name and name != "Unknown":
                    directors_set.add(name)

        # Source 3: Artists index
        artists_index = self.repo.indexes_dir / "artists.json"
        if artists_index.exists():
            try:
                with open(artists_index, "r", encoding="utf-8") as f:
                    artists_data = json.load(f)
                    for item in artists_data:
                        name = item.get("name", "").strip() if item.get("name") else ""
                        if name and name != "Unknown":
                            directors_set.add(name)
            except Exception:
                pass

        sorted_directors = sorted(list(directors_set), key=lambda s: s.lower())
        self._cached_directors = sorted_directors
        return sorted_directors

    def match_music_director(self, query: str) -> Dict[str, any]:
        if not query:
            return {"exact_match": None, "suggestions": []}

        canonical_list = self.get_canonical_directors()
        norm_query = self.normalize_string(query)

        exact_match = None
        matches = []

        for director in canonical_list:
            norm_dir = self.normalize_string(director)
            if norm_dir == norm_query:
                exact_match = director
                matches.insert(0, director)
            elif norm_query in norm_dir or norm_dir.startswith(norm_query):
                matches.append(director)

        seen = set()
        unique_suggestions = []
        for m in matches:
            if m not in seen:
                seen.add(m)
                unique_suggestions.append(m)

        return {
            "query": query,
            "exact_match": exact_match or (unique_suggestions[0] if unique_suggestions else None),
            "suggestions": unique_suggestions[:10]
        }

    def load_album_info(self, album_name: str) -> Dict[str, any]:
        info_file = self.repo.get_album_info_path(album_name)
        if info_file.exists():
            try:
                with open(info_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"Error reading {info_file}: {e}")

        return {
            "album": album_name,
            "year": 2026,
            "musicDirector": "Unknown",
            "genre": "Tollywood Soundtrack",
            "language": "Telugu",
            "country": "India",
            "releaseDate": "2026-01-01",
            "director": "Unknown",
            "producer": "Unknown",
            "banner": "Unknown"
        }

    def save_album_info(self, album_name: str, data: Dict[str, any]) -> Path:
        album_dir = self.repo.get_album_path(album_name)
        album_dir.mkdir(parents=True, exist_ok=True)
        info_file = self.repo.get_album_info_path(album_name)

        year_val = int(data.get("year", 2026)) if data.get("year") else 2026
        md_val = data.get("musicDirector", "Unknown")
        md_str = md_val.strip() if isinstance(md_val, str) and md_val.strip() else "Unknown"

        rel_date = data.get("releaseDate")
        rel_date_str = rel_date.strip() if isinstance(rel_date, str) and rel_date.strip() else f"{year_val}-01-01"

        payload = {
            "album": album_name,
            "year": year_val,
            "musicDirector": md_str,
            "genre": (data.get("genre") or "Tollywood Soundtrack").strip(),
            "language": (data.get("language") or "Telugu").strip(),
            "country": (data.get("country") or "India").strip(),
            "releaseDate": rel_date_str,
            "director": (data.get("director") or "Unknown").strip(),
            "producer": (data.get("producer") or "Unknown").strip(),
            "banner": (data.get("banner") or "Unknown").strip()
        }

        with open(info_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=4, ensure_ascii=False)

        self._cached_directors = None
        return info_file
