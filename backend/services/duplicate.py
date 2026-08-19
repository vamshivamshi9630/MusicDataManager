import hashlib
from pathlib import Path
from typing import List, Dict, Any, Optional
from backend.core.repository import IRepositoryProvider
from backend.services.metadata import MetadataService

class DuplicateDetectionService:
    def __init__(self, repo_context: IRepositoryProvider):
        self.repo = repo_context

    @staticmethod
    def compute_file_hash(file_path: Path) -> str:
        hasher = hashlib.sha256()
        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        return hasher.hexdigest()[:16]

    def analyze_uploaded_song(self, album_name: str, song_filename: str, file_size: int, duration_seconds: int, temp_file_path: Path = None) -> Dict[str, Any]:
        song_title = Path(song_filename).stem
        norm_title = MetadataService.normalize_string(song_title)
        candidate_song_id = self.repo.get_stable_id(f"{album_name}{song_filename}")

        album_path = self.repo.get_album_path(album_name)
        
        # Signal 1: Exact target file existence
        target_song_file = self.repo.get_song_path(album_name, song_filename)
        if target_song_file.exists():
            return {
                "status": "EXACT_DUPLICATE",
                "reason": f"File '{song_filename}' already exists in album '{album_name}'.",
                "song_id": candidate_song_id,
                "title": song_title
            }

        # Signal 2: Content Hash Match
        if temp_file_path and temp_file_path.exists() and album_path.exists():
            uploaded_hash = self.compute_file_hash(temp_file_path)
            for existing_mp3 in album_path.glob("*.mp3"):
                if self.compute_file_hash(existing_mp3) == uploaded_hash:
                    return {
                        "status": "EXACT_DUPLICATE",
                        "reason": f"Binary content matches existing file '{existing_mp3.name}'.",
                        "song_id": candidate_song_id,
                        "existing_file": existing_mp3.name,
                        "title": song_title
                    }

        # Signal 3: Title / Stem match in target album
        if album_path.exists():
            for existing_mp3 in album_path.glob("*.mp3"):
                if MetadataService.normalize_string(existing_mp3.stem) == norm_title:
                    return {
                        "status": "POTENTIAL_DUPLICATE",
                        "reason": f"A song with title '{existing_mp3.stem}' already exists in album '{album_name}'.",
                        "song_id": candidate_song_id,
                        "existing_file": existing_mp3.name,
                        "title": song_title
                    }

        return {
            "status": "NEW",
            "reason": "No duplicate signals detected.",
            "song_id": candidate_song_id,
            "title": song_title
        }

    def scan_entire_repository_for_duplicates(self) -> Dict[str, Any]:
        filenames_map = {}
        duplicates = []

        for album_dir in self.repo.list_all_album_directories():
            album_name = album_dir.name
            for mp3_file in album_dir.glob("*.mp3"):
                norm_stem = MetadataService.normalize_string(mp3_file.stem)
                key = (norm_stem, mp3_file.stat().st_size)
                if key in filenames_map:
                    duplicates.append({
                        "song_title": mp3_file.stem,
                        "file_1": filenames_map[key],
                        "file_2": f"{album_name}/{mp3_file.name}",
                        "size": mp3_file.stat().st_size
                    })
                else:
                    filenames_map[key] = f"{album_name}/{mp3_file.name}"

        return {
            "duplicate_count": len(duplicates),
            "duplicates": duplicates
        }
