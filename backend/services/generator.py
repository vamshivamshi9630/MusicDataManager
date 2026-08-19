import os
import sys
import time
import json
import subprocess
from pathlib import Path
from typing import Dict, Tuple, Any, List
from backend.core.repository import IRepositoryProvider, CloudRepository

class GeneratorValidationError(Exception):
    def __init__(self, message: str, stage: str = "generator", exit_code: int = 1, stdout: str = "", stderr: str = ""):
        super().__init__(message)
        self.message = message
        self.stage = stage
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr

class GeneratorService:
    def __init__(self, repo_context: IRepositoryProvider):
        self.repo = repo_context

    def get_current_metrics(self) -> Dict[str, int]:
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
        stages: List[Dict[str, Any]] = []
        before_metrics = self.get_current_metrics()

        print(f"[GENERATOR PIPELINE] Workspace root: {self.repo.root}")

        # Step 3: Run Authoritative Generator #1 (generate_metadata.py)
        v2_script = self.repo.root / "generate_metadata.py"
        if not v2_script.exists():
            raise GeneratorValidationError(
                message=f"Authoritative Generator #1 script 'generate_metadata.py' not found at workspace root '{self.repo.root}'.",
                stage="generate_metadata",
                exit_code=1
            )

        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        if isinstance(self.repo, CloudRepository):
            env["GENERATOR_CACHE_MODE"] = "1"
            env["STRICT_CLOUD_SAFETY"] = "1"
            env["CLOUD_MODE"] = "1"

        print(f"[GENERATOR #1] Executing '{v2_script.name}'...")
        t0 = time.time()
        try:
            res1 = subprocess.run(
                [sys.executable, str(v2_script)],
                cwd=self.repo.root,
                env=env,
                capture_output=True,
                text=True,
                timeout=35,
                check=True
            )
            duration1 = round(time.time() - t0, 3)
            stages.append({
                "stage": "generate_metadata",
                "success": True,
                "exit_code": 0,
                "duration_seconds": duration1,
                "stdout": res1.stdout[:1000] if res1.stdout else "",
                "stderr": res1.stderr[:1000] if res1.stderr else ""
            })
        except subprocess.TimeoutExpired as e:
            raise GeneratorValidationError(
                message="Generator #1 (generate_metadata.py) timed out after 35 seconds.",
                stage="generate_metadata",
                exit_code=124,
                stdout=e.stdout if isinstance(e.stdout, str) else "",
                stderr=e.stderr if isinstance(e.stderr, str) else ""
            )
        except subprocess.CalledProcessError as e:
            err_msg = e.stderr or e.stdout or f"Failed with exit code {e.returncode}"
            raise GeneratorValidationError(
                message=f"Generator #1 (generate_metadata.py) failed (exit code {e.returncode}): {err_msg[:500]}",
                stage="generate_metadata",
                exit_code=e.returncode,
                stdout=e.stdout or "",
                stderr=e.stderr or ""
            )

        self._validate_generated_catalogs()

        # Safety Check: Total songs should not drop unexpectedly
        after_metrics = self.get_current_metrics()
        if after_metrics["total_songs"] < before_metrics["total_songs"]:
            raise GeneratorValidationError(
                message=f"UNEXPECTED DELETION SHIELD TRIGGERED: Total song count dropped from {before_metrics['total_songs']} to {after_metrics['total_songs']}. Halting sync for safety.",
                stage="generate_metadata",
                exit_code=1
            )

        # Step 4: Run Authoritative Generator #2 (generate_or_update_songs_with_details.py)
        legacy_script = self.repo.root / "generate_or_update_songs_with_details.py"
        if not legacy_script.exists():
            raise GeneratorValidationError(
                message=f"Authoritative Generator #2 script 'generate_or_update_songs_with_details.py' not found at workspace root '{self.repo.root}'.",
                stage="generate_or_update_songs_with_details",
                exit_code=1
            )

        print(f"[GENERATOR #2] Executing '{legacy_script.name}'...")
        t0 = time.time()
        try:
            res2 = subprocess.run(
                [sys.executable, str(legacy_script)],
                cwd=self.repo.root,
                env=env,
                capture_output=True,
                text=True,
                timeout=35,
                check=True
            )
            duration2 = round(time.time() - t0, 3)
            stages.append({
                "stage": "generate_or_update_songs_with_details",
                "success": True,
                "exit_code": 0,
                "duration_seconds": duration2,
                "stdout": res2.stdout[:1000] if res2.stdout else "",
                "stderr": res2.stderr[:1000] if res2.stderr else ""
            })
        except subprocess.TimeoutExpired as e:
            raise GeneratorValidationError(
                message="Generator #2 (generate_or_update_songs_with_details.py) timed out after 35 seconds.",
                stage="generate_or_update_songs_with_details",
                exit_code=124,
                stdout=e.stdout if isinstance(e.stdout, str) else "",
                stderr=e.stderr if isinstance(e.stderr, str) else ""
            )
        except subprocess.CalledProcessError as e:
            err_msg = e.stderr or e.stdout or f"Failed with exit code {e.returncode}"
            raise GeneratorValidationError(
                message=f"Generator #2 (generate_or_update_songs_with_details.py) failed (exit code {e.returncode}): {err_msg[:500]}",
                stage="generate_or_update_songs_with_details",
                exit_code=e.returncode,
                stdout=e.stdout or "",
                stderr=e.stderr or ""
            )

        return {
            "success": True,
            "before_metrics": before_metrics,
            "after_metrics": after_metrics,
            "songs_added": after_metrics["total_songs"] - before_metrics["total_songs"],
            "albums_count": after_metrics["total_albums"],
            "stages": stages
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
                raise GeneratorValidationError(
                    message=f"Required generated catalog file missing: {file_path.relative_to(self.repo.root)}",
                    stage="generate_metadata",
                    exit_code=1
                )

            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    json.load(f)
            except Exception as e:
                raise GeneratorValidationError(
                    message=f"Generated catalog file '{file_path.name}' contains invalid JSON: {e}",
                    stage="generate_metadata",
                    exit_code=1
                )
