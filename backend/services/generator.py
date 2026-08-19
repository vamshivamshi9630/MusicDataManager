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

    def _ensure_sparse_scanner_support(self):
        scanner_file = self.repo.root / "generator" / "scanner.py"
        if not scanner_file.exists():
            return

        sparse_scanner_code = '''import json
import io
from pathlib import Path
from typing import List, Dict, Optional
from generator.config import BASE_PATH, AUDIO_EXTENSIONS, IMAGE_EXTENSIONS

class DummyMp3Path(Path):
    _flavour = Path()._flavour
    def __new__(cls, filename: str, album_dir_ref: Path):
        path_obj = super().__new__(cls, album_dir_ref / filename)
        path_obj._custom_filename = filename
        return path_obj

    @property
    def name(self) -> str:
        return getattr(self, "_custom_filename", super().name)

    def stat(self, *, follow_symlinks=True):
        class DummyStat:
            st_size = 1000
        return DummyStat()

    def exists(self, *, follow_symlinks=True):
        return True

    def read_bytes(self):
        return b""

    def open(self, *args, **kwargs):
        return io.BytesIO(b"")

def find_album_image(album_path: Path, album_name: str) -> str:
    possible_names = [
        f"{album_name}.png",
        f"{album_name}.jpg",
        f"{album_name}.jpeg",
        "folder.png",
        "folder.jpg",
        "cover.png",
        "cover.jpg",
        "cover.jpeg"
    ]
    for name in possible_names:
        p = album_path / name
        if p.exists() and p.stat().st_size > 0:
            return name

    for ext in IMAGE_EXTENSIONS:
        for f in album_path.glob(f"*{ext}"):
            if f.stat().st_size > 0:
                return f.name

    return ""

def scan_album_directory(album_dir: Path) -> Optional[Dict]:
    info_file = album_dir / "album_info.json"
    if not info_file.exists():
        return None

    mp3_files = sorted(
        [f for f in album_dir.glob("*.mp3") if f.stat().st_size > 0],
        key=lambda f: f.name.lower()
    )

    if not mp3_files:
        return None

    try:
        with open(info_file, "r", encoding="utf-8") as f:
            album_info = json.load(f)
    except Exception as e:
        print(f"Error reading {info_file}: {e}")
        return None

    album_name = album_dir.name
    image_name = find_album_image(album_dir, album_name)

    return {
        "dir_path": album_dir,
        "album_name": album_name,
        "album_info": album_info,
        "image_name": image_name,
        "mp3_files": mp3_files
    }

def scan_all_albums() -> List[Dict]:
    ignored = {".git", "generator", "metadata", "tmp", "__pycache__", ".idea", "MusicDirectorImages", "New_Icons"}
    disk_dirs = sorted(
        [d for d in BASE_PATH.iterdir() if d.is_dir() and not d.name.startswith(".") and d.name not in ignored],
        key=lambda d: d.name.lower()
    )

    scanned_albums = []
    scanned_names = set()

    for d in disk_dirs:
        data = scan_album_directory(d)
        if data:
            scanned_albums.append(data)
            scanned_names.add(d.name.lower())

    metadata_albums_dir = BASE_PATH / "metadata" / "albums"
    if metadata_albums_dir.exists():
        for partition_dir in metadata_albums_dir.iterdir():
            if partition_dir.is_dir():
                for json_file in partition_dir.glob("*.json"):
                    album_stem = json_file.stem
                    if album_stem.lower() not in scanned_names:
                        try:
                            with open(json_file, "r", encoding="utf-8") as f:
                                cached_data = json.load(f)
                            alb = cached_data.get("album", {})
                            songs_list = cached_data.get("songs", [])
                            alb_name = alb.get("name", album_stem)
                            dummy_dir = BASE_PATH / alb_name
                            dummy_mp3s = [
                                DummyMp3Path(s.get("audio") or f"{s.get('title')}.mp3", dummy_dir)
                                for s in songs_list
                            ]
                            scanned_albums.append({
                                "dir_path": dummy_dir,
                                "album_name": alb_name,
                                "album_info": {
                                    "musicDirector": alb.get("musicDirector", "Unknown"),
                                    "year": alb.get("year", 2026),
                                    "genre": alb.get("genre", "Tollywood Soundtrack"),
                                    "language": alb.get("language", "Telugu")
                                },
                                "image_name": alb.get("image", f"{alb_name}.png"),
                                "mp3_files": dummy_mp3s
                            })
                            scanned_names.add(alb_name.lower())
                        except Exception:
                            pass

    return scanned_albums
'''
        try:
            with open(scanner_file, "w", encoding="utf-8") as f:
                f.write(sparse_scanner_code)
        except Exception:
            pass

    def _ensure_generator2_base_path_support(self):
        gen2_file = self.repo.root / "generate_or_update_songs_with_details.py"
        if not gen2_file.exists():
            return
        try:
            content = gen2_file.read_text(encoding="utf-8")
            if 'BASE_PATH = Path(r"D:\\MusicData")' in content or 'BASE_PATH = Path("D:\\\\MusicData")' in content:
                updated = content.replace(
                    'BASE_PATH = Path(r"D:\\MusicData")',
                    'BASE_PATH = Path(os.environ.get("MUSICDATA_REPOSITORY_ROOT", Path(__file__).resolve().parent))'
                ).replace(
                    'BASE_PATH = Path("D:\\\\MusicData")',
                    'BASE_PATH = Path(os.environ.get("MUSICDATA_REPOSITORY_ROOT", Path(__file__).resolve().parent))'
                )
                gen2_file.write_text(updated, encoding="utf-8")
        except Exception:
            pass

    def run_generator_pipeline(self) -> Dict[str, Any]:
        stages: List[Dict[str, Any]] = []
        before_metrics = self.get_current_metrics()

        print(f"[GENERATOR PIPELINE] Workspace root: {self.repo.root}")

        self._ensure_generator2_base_path_support()

        if isinstance(self.repo, CloudRepository):
            self._ensure_sparse_scanner_support()

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
        env["MUSICDATA_REPOSITORY_ROOT"] = str(self.repo.root)
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
