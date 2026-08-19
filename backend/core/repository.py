import os
import sys
import stat
import shutil
import hashlib
import tempfile
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional, List, Dict
from urllib.parse import quote
from backend.core.config import settings

class PathTraversalError(ValueError):
    pass

class CloudWorkspaceError(Exception):
    pass

def _remove_readonly(func, path, excinfo):
    os.chmod(path, stat.S_IWRITE)
    func(path)

class IRepositoryProvider(ABC):
    @property
    @abstractmethod
    def root(self) -> Path:
        pass

    @property
    @abstractmethod
    def metadata_dir(self) -> Path:
        pass

    @property
    @abstractmethod
    def albums_dir(self) -> Path:
        pass

    @property
    @abstractmethod
    def indexes_dir(self) -> Path:
        pass

    @property
    @abstractmethod
    def staging_dir(self) -> Path:
        pass

    @abstractmethod
    def get_album_path(self, album_name: str) -> Path:
        pass

    @abstractmethod
    def get_image_path(self, album_name: str) -> Path:
        pass

    @abstractmethod
    def get_album_info_path(self, album_name: str) -> Path:
        pass

    @abstractmethod
    def get_song_path(self, album_name: str, song_filename: str) -> Path:
        pass

    @abstractmethod
    def get_staging_path(self, session_id: str, filename: str) -> Path:
        pass

    @abstractmethod
    def list_all_album_directories(self) -> List[Path]:
        pass

    def validate_safe_filename(self, name: str) -> str:
        if not name or not isinstance(name, str):
            raise PathTraversalError("Invalid or empty path element.")
        
        raw = name.strip()
        if ".." in raw or "/" in raw or "\\" in raw or ":" in raw:
            raise PathTraversalError(f"Path traversal security check failed for element: '{name}'")
        
        return raw

    def sanitize_name(self, name: str) -> str:
        return self.validate_safe_filename(name)

    def get_partition(self, album_name: str) -> str:
        if not album_name:
            return "0-9"
        first_char = album_name.strip()[0].upper()
        return first_char if first_char.isalpha() else "0-9"

    def get_stable_id(self, val: str) -> str:
        clean_val = val.strip().lower()
        return hashlib.sha256(clean_val.encode('utf-8')).hexdigest()[:12]

    def build_raw_audio_url(self, album_name: str, song_filename: str) -> str:
        return f"https://raw.githubusercontent.com/{settings.GITHUB_OWNER}/{settings.GITHUB_REPOSITORY}/{settings.GITHUB_BRANCH}/{quote(album_name)}/{quote(song_filename)}"

    def build_raw_image_url(self, album_name: str, image_filename: str) -> str:
        return f"https://raw.githubusercontent.com/{settings.GITHUB_OWNER}/{settings.GITHUB_REPOSITORY}/{settings.GITHUB_BRANCH}/{quote(album_name)}/{quote(image_filename)}"


class LocalRepository(IRepositoryProvider):
    """Local Mode implementation operating directly on persistent local disk."""

    def __init__(self, root_override: Optional[str] = None):
        self._root = self._resolve_repository_root(root_override)
        self._staging_dir = self._root / ".staging"
        self._staging_dir.mkdir(exist_ok=True)
        self._validate_repository_markers()

    def _resolve_repository_root(self, root_override: Optional[str] = None) -> Path:
        if root_override and Path(root_override).exists():
            return Path(root_override).resolve()

        env_root = settings.MUSICDATA_REPOSITORY_ROOT or os.environ.get("MUSICDATA_REPOSITORY_ROOT")
        if env_root and Path(env_root).exists():
            return Path(env_root).resolve()

        app_dir = Path(__file__).resolve().parent.parent.parent
        candidates = [
            app_dir.parent / "MusicData" / "MusicData-main",
            app_dir.parent / "MusicData",
            app_dir / "MusicData-main",
            Path.cwd()
        ]
        for candidate in candidates:
            if candidate.exists() and (candidate / ".git").exists() and (candidate / "generate_metadata.py").exists():
                return candidate.resolve()

        target = app_dir
        while target != target.parent:
            if (target / "MusicData-main").exists() and (target / "MusicData-main" / "generate_metadata.py").exists():
                return (target / "MusicData-main").resolve()
            target = target.parent

        raise FileNotFoundError(
            "Could not discover MusicData repository root. Please set the MUSICDATA_REPOSITORY_ROOT environment variable."
        )

    def _validate_repository_markers(self):
        v2_script = self._root / "generate_metadata.py"
        generator_pkg = self._root / "generator"
        metadata_dir = self._root / "metadata"

        if not (v2_script.exists() and generator_pkg.exists() and metadata_dir.exists()):
            raise ValueError(
                f"Directory '{self._root}' is not a valid MusicData repository. Missing core generator markers."
            )

    @property
    def root(self) -> Path:
        return self._root

    @property
    def git_root(self) -> Path:
        return self._root

    @property
    def metadata_dir(self) -> Path:
        return self._root / "metadata"

    @property
    def albums_dir(self) -> Path:
        return self._root / "metadata" / "albums"

    @property
    def indexes_dir(self) -> Path:
        return self._root / "metadata" / "indexes"

    @property
    def staging_dir(self) -> Path:
        return self._staging_dir

    def get_album_path(self, album_name: str) -> Path:
        safe_album = self.validate_safe_filename(album_name)
        target = (self._root / safe_album).resolve()
        try:
            target.relative_to(self._root.resolve())
        except ValueError:
            raise PathTraversalError(f"Path traversal detected! Path '{album_name}' escapes repository root.")
        return target

    def get_image_path(self, album_name: str) -> Path:
        safe_album = self.validate_safe_filename(album_name)
        target = (self._root / safe_album / f"{safe_album}.png").resolve()
        try:
            target.relative_to(self._root.resolve())
        except ValueError:
            raise PathTraversalError(f"Path traversal detected for artwork: '{album_name}'")
        return target

    def get_album_info_path(self, album_name: str) -> Path:
        safe_album = self.validate_safe_filename(album_name)
        target = (self._root / safe_album / "album_info.json").resolve()
        try:
            target.relative_to(self._root.resolve())
        except ValueError:
            raise PathTraversalError(f"Path traversal detected for metadata: '{album_name}'")
        return target

    def get_song_path(self, album_name: str, song_filename: str) -> Path:
        safe_album = self.validate_safe_filename(album_name)
        safe_song = self.validate_safe_filename(song_filename)
        target = (self._root / safe_album / safe_song).resolve()
        try:
            target.relative_to(self._root.resolve())
        except ValueError:
            raise PathTraversalError(f"Path traversal detected for song: '{song_filename}'")
        return target

    def get_staging_path(self, session_id: str, filename: str) -> Path:
        safe_session = self.validate_safe_filename(session_id)
        safe_file = self.validate_safe_filename(filename)
        session_dir = self._staging_dir / safe_session
        session_dir.mkdir(exist_ok=True)
        target = (session_dir / safe_file).resolve()
        try:
            target.relative_to(self._staging_dir.resolve())
        except ValueError:
            raise PathTraversalError(f"Staging path traversal detected: '{filename}'")
        return target

    def list_all_album_directories(self) -> List[Path]:
        ignored = {".git", "generator", "metadata", "tmp", "__pycache__", ".idea", "MusicDirectorImages", "New_Icons", ".staging"}
        return sorted([
            d for d in self._root.iterdir()
            if d.is_dir() and not d.name.startswith(".") and d.name not in ignored
        ], key=lambda d: d.name.lower())


class CloudRepository(IRepositoryProvider):
    """Cloud Mode implementation operating on ephemeral, blobless Git workspaces."""

    def __init__(self, job_id: str, base_temp_dir: Optional[Path] = None, remote_url: Optional[str] = None):
        self.job_id = self.validate_safe_filename(job_id)
        self.base_temp_dir = (base_temp_dir or Path(tempfile.gettempdir()) / "musicdata_workspaces").resolve()
        self.base_temp_dir.mkdir(parents=True, exist_ok=True)
        
        self.workspace_dir = (self.base_temp_dir / self.job_id).resolve()
        self._staging_dir = (self.workspace_dir / ".staging").resolve()
        self._root = (self.workspace_dir / "MusicData").resolve()
        
        # Build authenticated remote URL if token is available
        auth_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("AGENT_AUTH_TOKEN")
        if not auth_token:
            try:
                from backend.services.github_auth import GitHubTokenManager
                auth_token = GitHubTokenManager().get_installation_access_token()
            except Exception:
                pass

        if auth_token and not str(auth_token).startswith("ghs_mock") and "x-access-token" not in (remote_url or ""):
            self.remote_url = f"https://x-access-token:{auth_token}@github.com/{settings.GITHUB_OWNER}/{settings.GITHUB_REPOSITORY}.git"
        else:
            self.remote_url = remote_url or f"https://github.com/{settings.GITHUB_OWNER}/{settings.GITHUB_REPOSITORY}.git"

    def provision_blobless_workspace(self, album_name: Optional[str] = None, source_repo_override: Optional[Path] = None) -> Path:
        if self.workspace_dir.exists():
            shutil.rmtree(self.workspace_dir, onerror=_remove_readonly)

        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self._staging_dir.mkdir(parents=True, exist_ok=True)

        if source_repo_override and source_repo_override.exists():
            shutil.copytree(str(source_repo_override), str(self._root))
        else:
            env = os.environ.copy()
            env["GIT_TERMINAL_PROMPT"] = "0"
            
            # Step 1: Shallow blobless clone without checkout (super fast, < 4s)
            cmd_clone = [
                "git", "-c", "pack.threads=1", "clone",
                "--filter=blob:none",
                "--no-checkout",
                "--depth", "1",
                "--branch", settings.GITHUB_BRANCH,
                self.remote_url,
                str(self._root)
            ]
            try:
                res = subprocess.run(cmd_clone, capture_output=True, text=True, env=env, timeout=25)
                if res.returncode != 0:
                    raise CloudWorkspaceError(f"Minimal repository fetch failed (exit code {res.returncode}): {res.stderr or res.stdout}")
            except subprocess.TimeoutExpired:
                raise CloudWorkspaceError("Minimal repository fetch timed out after 25 seconds.")

            # Step 2: Initialize sparse-checkout for metadata, generator, scripts, and target album
            subprocess.run(["git", "sparse-checkout", "init", "--cone"], cwd=self._root, capture_output=True, text=True, env=env, timeout=15, check=False)
            
            sparse_targets = [
                "metadata",
                "generator",
                "generate_metadata.py",
                "generate_or_update_songs_with_details.py",
                "songs_with_details.json",
                "MusiDirector_Year.txt"
            ]
            if album_name:
                sparse_targets.append(album_name)

            subprocess.run(["git", "sparse-checkout", "set"] + sparse_targets, cwd=self._root, capture_output=True, text=True, env=env, timeout=15, check=False)
            
            # Step 3: Checkout branch main and root generator scripts
            subprocess.run(["git", "checkout", settings.GITHUB_BRANCH], cwd=self._root, capture_output=True, text=True, env=env, timeout=35, check=False)
            subprocess.run(["git", "checkout", settings.GITHUB_BRANCH, "--", "generate_metadata.py", "generate_or_update_songs_with_details.py", "songs_with_details.json"], cwd=self._root, capture_output=True, text=True, env=env, timeout=15, check=False)

        # Configure author identity for Cloud commit operations
        subprocess.run(["git", "config", "user.name", "MusicData Manager Cloud"], cwd=self._root, check=False)
        subprocess.run(["git", "config", "user.email", "cloud-agent@musicdata.internal"], cwd=self._root, check=False)

        print(f"[CLOUD WORKSPACE] Provisioned minimal sparse repository workspace at '{self._root}'")
        return self._root

    def cleanup_workspace(self) -> bool:
        resolved_ws = self.workspace_dir.resolve()
        resolved_base = self.base_temp_dir.resolve()

        try:
            resolved_ws.relative_to(resolved_base)
        except ValueError:
            raise PathTraversalError(f"Cleanup safety violation! Target '{resolved_ws}' escapes base temp dir '{resolved_base}'.")

        if resolved_ws.exists():
            shutil.rmtree(resolved_ws, onerror=_remove_readonly)
            return True
        return False

    @property
    def root(self) -> Path:
        return self._root

    @property
    def metadata_dir(self) -> Path:
        return self._root / "metadata"

    @property
    def albums_dir(self) -> Path:
        return self._root / "metadata" / "albums"

    @property
    def indexes_dir(self) -> Path:
        return self._root / "metadata" / "indexes"

    @property
    def staging_dir(self) -> Path:
        return self._staging_dir

    def get_album_path(self, album_name: str) -> Path:
        safe_album = self.validate_safe_filename(album_name)
        target = (self._root / safe_album).resolve()
        try:
            target.relative_to(self._root.resolve())
        except ValueError:
            raise PathTraversalError(f"Cloud path traversal detected: '{album_name}'")
        return target

    def get_image_path(self, album_name: str) -> Path:
        safe_album = self.validate_safe_filename(album_name)
        target = (self._root / safe_album / f"{safe_album}.png").resolve()
        try:
            target.relative_to(self._root.resolve())
        except ValueError:
            raise PathTraversalError(f"Cloud artwork path traversal detected: '{album_name}'")
        return target

    def get_album_info_path(self, album_name: str) -> Path:
        safe_album = self.validate_safe_filename(album_name)
        target = (self._root / safe_album / "album_info.json").resolve()
        try:
            target.relative_to(self._root.resolve())
        except ValueError:
            raise PathTraversalError(f"Cloud metadata path traversal detected: '{album_name}'")
        return target

    def get_song_path(self, album_name: str, song_filename: str) -> Path:
        safe_album = self.validate_safe_filename(album_name)
        safe_song = self.validate_safe_filename(song_filename)
        target = (self._root / safe_album / safe_song).resolve()
        try:
            target.relative_to(self._root.resolve())
        except ValueError:
            raise PathTraversalError(f"Cloud song path traversal detected: '{song_filename}'")
        return target

    def get_staging_path(self, session_id: str, filename: str) -> Path:
        safe_session = self.validate_safe_filename(session_id)
        safe_file = self.validate_safe_filename(filename)
        session_dir = self._staging_dir / safe_session
        session_dir.mkdir(exist_ok=True)
        target = (session_dir / safe_file).resolve()
        try:
            target.relative_to(self._staging_dir.resolve())
        except ValueError:
            raise PathTraversalError(f"Cloud staging path traversal detected: '{filename}'")
        return target

    def list_all_album_directories(self) -> List[Path]:
        ignored = {".git", "generator", "metadata", "tmp", "__pycache__", ".idea", "MusicDirectorImages", "New_Icons", ".staging"}
        return sorted([
            d for d in self._root.iterdir()
            if d.is_dir() and not d.name.startswith(".") and d.name not in ignored
        ], key=lambda d: d.name.lower())

RepositoryContext = LocalRepository
