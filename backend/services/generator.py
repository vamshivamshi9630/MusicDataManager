import os
import sys
import json
import subprocess
from pathlib import Path
from typing import Dict, Tuple, Any
from backend.core.repository import IRepositoryProvider, CloudRepository

class GeneratorValidationError(Exception):
    pass

class GeneratorService:
    def __init__(self, repo_context: IRepositoryProvider):
        self.repo = repo_context

    def get_current_metrics(self) -> Dict[str, int]:
        manifest_file = self.repo.metadata_dir / "manifest.json"
        stats_file = self.repo.metadata_dir / "statistics.json"

        metrics = {"total_albums": 0, "total_songs": 0}

        if stats_file.exists():
            try:
                with open(stats_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    metrics["total_albums"] = data.get("totalAlbums", 0)
                    metrics["total_songs"] = data.get("totalSongs", 0)
                    return metrics
            except Exception:
                pass

        albums = self.repo.list_all_album_directories()
        metrics["total_albums"] = len(albums)
        metrics["total_songs"] = sum(len(list(a.glob("*.mp3"))) for a in albums)
        return metrics

    def run_generator_pipeline(self) -> Dict[str, Any]:
        before_metrics = self.get_current_metrics()

        v2_script = self.repo.root / "generate_metadata.py"
        if not v2_script.exists():
            raise GeneratorValidationError(f"Authoritative generator script '{v2_script}' not found.")

        # Environment configuration
        env = os.environ.copy()
        if isinstance(self.repo, CloudRepository):
            env["GENERATOR_CACHE_MODE"] = "1"
            env["STRICT_CLOUD_SAFETY"] = "1"
            env["CLOUD_MODE"] = "1"

        try:
            res = subprocess.run(
                [sys.executable, str(v2_script)],
                cwd=self.repo.root,
                env=env,
                capture_output=True,
                text=True,
                check=True
            )
            v2_output_log = res.stdout
        except subprocess.CalledProcessError as e:
            raise GeneratorValidationError(f"v2 Metadata Generator failed with code {e.returncode}:\n{e.stderr or e.stdout}")

        self._validate_generated_catalogs()

        after_metrics = self.get_current_metrics()

        if after_metrics["total_songs"] < before_metrics["total_songs"]:
            raise GeneratorValidationError(
                f"UNEXPECTED DELETION SHIELD TRIGGERED: Total song count dropped from {before_metrics['total_songs']} to {after_metrics['total_songs']}. Halting sync for safety."
            )

        legacy_script = self.repo.root / "generate_or_update_songs_with_details.py"
        legacy_output_log = "Legacy generator not executed."
        if legacy_script.exists():
            try:
                legacy_res = subprocess.run(
                    [sys.executable, str(legacy_script)],
                    cwd=self.repo.root,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False
                )
                legacy_output_log = legacy_res.stdout
            except Exception as e:
                legacy_output_log = f"Legacy generator warning: {e}"

        return {
            "success": True,
            "before_metrics": before_metrics,
            "after_metrics": after_metrics,
            "songs_added": after_metrics["total_songs"] - before_metrics["total_songs"],
            "albums_count": after_metrics["total_albums"],
            "v2_log": v2_output_log,
            "legacy_log": legacy_output_log
        }

    def _validate_generated_catalogs(self):
        required_files = [
            self.repo.metadata_dir / "manifest.json",
            self.repo.metadata_dir / "statistics.json",
            self.repo.indexes_dir / "albums.json",
            self.repo.indexes_dir / "search_index.json"
        ]

        for file_path in required_files:
            if not file_path.exists():
                raise GeneratorValidationError(f"Required generated catalog file missing: {file_path.relative_to(self.repo.root)}")

            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    json.load(f)
            except Exception as e:
                raise GeneratorValidationError(f"Generated catalog file '{file_path.name}' contains invalid JSON: {e}")
