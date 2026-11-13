import os
import sys
import time
import logging
import threading
import subprocess
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List
from pathlib import Path
from backend.core.repository import IRepositoryProvider, LocalRepository, CloudRepository, PathTraversalError
from backend.core.config import settings

logger = logging.getLogger(__name__)

# Repository-level thread locks to serialize mutations per repository root within a Python process
_repo_locks: Dict[str, threading.RLock] = {}
_repo_locks_guard = threading.Lock()

def get_repository_lock(repo_root: Path) -> threading.RLock:
    key = str(repo_root.resolve()).lower()
    with _repo_locks_guard:
        if key not in _repo_locks:
            _repo_locks[key] = threading.RLock()
        return _repo_locks[key]


class RepositoryFileLock:
    """
    Cross-process OS-level file lock ensuring mutual exclusion across multiple OS processes
    sharing the same Git workspace. Uses fcntl on POSIX (Linux/Render) and msvcrt on Windows.
    """
    def __init__(self, lock_file_path: Path, timeout: float = 60.0):
        self.lock_file_path = lock_file_path
        self.timeout = timeout
        self._file = None

    def acquire(self) -> bool:
        self.lock_file_path.parent.mkdir(parents=True, exist_ok=True)
        start = time.time()
        self._file = open(self.lock_file_path, "a+")
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    self._file.seek(0)
                    msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._file.seek(0)
                self._file.truncate()
                self._file.write(f"pid={os.getpid()} time={time.time()}\n")
                self._file.flush()
                return True
            except (BlockingIOError, PermissionError, OSError):
                if time.time() - start >= self.timeout:
                    raise GitSyncError(
                        message=f"Timeout waiting for OS repository lock at '{self.lock_file_path}' after {self.timeout}s.",
                        stage="git_lock",
                        exit_code=1
                    )
                time.sleep(0.05)

    def release(self) -> None:
        if self._file:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    self._file.seek(0)
                    msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


def is_git_process_running_in_worktree(repo_root: Path) -> bool:
    """
    Verifies whether an active Git process is currently executing in the given worktree.
    Prevents deleting an active .git/index.lock created by a live Git process.
    """
    root_str = str(repo_root.resolve()).lower()

    # 1. On POSIX (Linux/Render), inspect /proc if available
    proc_dir = Path("/proc")
    if proc_dir.exists() and proc_dir.is_dir():
        try:
            for p_dir in proc_dir.iterdir():
                if p_dir.name.isdigit():
                    try:
                        comm_file = p_dir / "comm"
                        if comm_file.exists() and "git" in comm_file.read_text(encoding="utf-8").lower():
                            cwd_link = (p_dir / "cwd").resolve()
                            if str(cwd_link).lower().startswith(root_str):
                                return True
                    except (PermissionError, FileNotFoundError, OSError):
                        continue
        except Exception:
            pass

    # 2. On Windows, check whether index.lock is held open with exclusive write handle by an active process
    if sys.platform == "win32":
        lock_file = repo_root / ".git" / "index.lock"
        if lock_file.exists():
            try:
                with open(lock_file, "r+"):
                    pass
                return False  # Handle test succeeded, no active process holds it exclusively locked
            except (PermissionError, OSError):
                return True  # An active process is holding it open

    return False


class GitSyncError(Exception):
    def __init__(self, message: str, stage: str = "git_sync", exit_code: int = 1, stdout: str = "", stderr: str = ""):
        super().__init__(message)
        self.message = message
        self.stage = stage
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr

class IGitSyncProvider(ABC):
    @abstractmethod
    def get_git_status(self) -> Dict[str, Any]:
        pass

    @abstractmethod
    def verify_remote_sync(self) -> Dict[str, Any]:
        pass

    @abstractmethod
    def stage_commit_and_push(self, album_name: str, mode: str = "add", custom_commit_msg: str = "", push_enabled: bool = True, extra_stage_paths: Optional[List[str]] = None) -> Dict[str, Any]:
        pass

class BaseGitSyncService(IGitSyncProvider):
    def __init__(self, repo_context: IRepositoryProvider):
        self.repo = repo_context
        self._repo_lock = get_repository_lock(self.repo.root)
        self._file_lock = RepositoryFileLock(self.repo.root / ".git" / "musicdata_repo.lock")

    def _clean_stale_index_lock(self) -> None:
        """
        Detect and safely clean up dangling index.lock left behind by killed or timed-out git processes.
        Safety rules:
        - NEVER delete index.lock based solely on age.
        - Age must exceed 25 seconds.
        - An active Git process must NOT be running in the worktree.
        """
        try:
            index_lock = self.repo.root / ".git" / "index.lock"
            if index_lock.exists():
                lock_age = time.time() - index_lock.stat().st_mtime
                if lock_age > 25:  # Stale lock older than 25 seconds
                    if is_git_process_running_in_worktree(self.repo.root):
                        logger.warning(
                            f"[GIT SYNC] .git/index.lock is {round(lock_age, 1)}s old, but an active git process was detected in {self.repo.root}. Preserving lock."
                        )
                        return
                    index_lock.unlink(missing_ok=True)
                    logger.warning(f"[GIT SYNC] Safely cleaned up abandoned stale index.lock (age {round(lock_age, 1)}s) at {index_lock}")
        except Exception as e:
            logger.warning(f"[GIT SYNC] Error checking/cleaning index.lock: {e}")

    def _run_git(self, args: list, check: bool = True, timeout: int = 60) -> str:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"

        mutating_commands = {"add", "commit", "checkout", "rm", "reset", "pull", "rebase", "merge", "clean"}
        is_mutating = bool(args and args[0] in mutating_commands)

        if is_mutating:
            self._clean_stale_index_lock()

        try:
            res = subprocess.run(
                ["git"] + args,
                cwd=self.repo.root,
                capture_output=True,
                text=True,
                env=env,
                timeout=timeout,
                check=check
            )
            return res.stdout.strip()
        except subprocess.TimeoutExpired as e:
            if is_mutating:
                time.sleep(0.5)
                self._clean_stale_index_lock()
            raise GitSyncError(
                message=f"Git command 'git {' '.join(args)}' timed out after {timeout} seconds.",
                stage="git_sync",
                exit_code=124,
                stdout=e.stdout if isinstance(e.stdout, str) else "",
                stderr=e.stderr if isinstance(e.stderr, str) else ""
            )
        except subprocess.CalledProcessError as e:
            err_msg = e.stderr or e.stdout or str(e)
            raise GitSyncError(
                message=f"Git command 'git {' '.join(args)}' failed (exit code {e.returncode}): {err_msg}",
                stage="git_sync",
                exit_code=e.returncode,
                stdout=e.stdout or "",
                stderr=e.stderr or ""
            )

    def get_git_status(self) -> Dict[str, Any]:
        branch = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"], check=False) or "main"
        head = self._run_git(["rev-parse", "HEAD"], check=False)[:12] or "000000000000"
        porcelain = self._run_git(["status", "--porcelain"], check=False)
        remote_url = self._run_git(["config", "--get", "remote.origin.url"], check=False)

        is_clean = len(porcelain.strip()) == 0
        changed_files = porcelain.strip().splitlines() if porcelain.strip() else []

        return {
            "branch": branch,
            "head": head,
            "remote": remote_url,
            "is_clean": is_clean,
            "changed_files_count": len(changed_files),
            "changed_files": changed_files[:20]
        }

    def get_remote_head_sha(self) -> str:
        """Fetch current remote HEAD SHA for branch main using ls-remote or rev-parse origin/main."""
        try:
            ls_out = self._run_git(["ls-remote", "origin", settings.GITHUB_BRANCH], check=False, timeout=15)
            if ls_out and ls_out.strip():
                return ls_out.strip().split()[0]
        except Exception:
            pass
        try:
            return self._run_git(["rev-parse", f"origin/{settings.GITHUB_BRANCH}"], check=False, timeout=10) or ""
        except Exception:
            return ""

    def verify_remote_sync(self) -> Dict[str, Any]:
        if os.environ.get("SOURCE_REPO_OVERRIDE") or not settings.GITHUB_APP_INSTALLATION_ID:
            local_head = self._run_git(["rev-parse", "HEAD"], check=False)[:12] or "dry_run_head"
            return {"local_head": local_head, "remote_head": local_head, "in_sync": True}

        try:
            self._run_git(["fetch", "origin", settings.GITHUB_BRANCH], check=False, timeout=20)
            remote_head = self._run_git(["rev-parse", f"origin/{settings.GITHUB_BRANCH}"], check=False)[:12]
            local_head = self._run_git(["rev-parse", "HEAD"], check=False)[:12]
            return {
                "local_head": local_head,
                "remote_head": remote_head,
                "in_sync": local_head == remote_head if remote_head else True
            }
        except Exception as e:
            return {"error": str(e), "in_sync": False}

    def stage_commit_and_push(self, album_name: str, mode: str = "add", custom_commit_msg: str = "", push_enabled: bool = True, extra_stage_paths: Optional[List[str]] = None) -> Dict[str, Any]:
        with self._repo_lock, self._file_lock:
            # Path traversal and safety validation for target album
            safe_album = self.repo.validate_safe_filename(album_name)
            album_dir = (self.repo.root / safe_album).resolve()
            try:
                album_dir.relative_to(self.repo.root.resolve())
            except ValueError:
                raise PathTraversalError(f"Target album path '{album_dir}' escapes repository root '{self.repo.root}'")

            # Verify git worktree validity before committing
            is_inside = self._run_git(["rev-parse", "--is-inside-work-tree"], check=False)
            if is_inside != "true":
                raise GitSyncError(
                    message=f"Workspace '{self.repo.root}' is not a valid Git repository worktree.",
                    stage="git_status",
                    exit_code=1
                )

            if custom_commit_msg and custom_commit_msg.strip():
                msg = custom_commit_msg.strip()
            elif mode == "edit":
                msg = f"edited songs/album with {safe_album}"
            else:
                msg = f"added songs/album with {safe_album}"

            # Cleanup Python bytecode cache (__pycache__/*.pyc) before git add to prevent committing binary cache junk
            try:
                import shutil
                for pycache_path in self.repo.root.rglob("__pycache__"):
                    if pycache_path.is_dir():
                        shutil.rmtree(pycache_path, ignore_errors=True)
            except Exception:
                pass

            # Stage only explicit intended album, metadata, and generator artifact paths (prevents slow recursive tree traversal on sparse checkouts)
            paths_to_stage = [safe_album]
            if (self.repo.root / "metadata").exists():
                paths_to_stage.append("metadata")
            for opt_file in ("songs_with_details.json", "MusiDirector_Year.txt"):
                if (self.repo.root / opt_file).exists():
                    paths_to_stage.append(opt_file)
            if extra_stage_paths:
                for ep in extra_stage_paths:
                    ep_clean = ep.strip()
                    if ".." in ep_clean:
                        raise PathTraversalError(f"Path traversal detected in extra stage path: '{ep}'")
                    resolved_ep = (self.repo.root / ep_clean).resolve()
                    try:
                        resolved_ep.relative_to(self.repo.root.resolve())
                    except ValueError:
                        raise PathTraversalError(f"Extra stage path '{ep}' escapes repository root '{self.repo.root}'")
                    if ep_clean not in paths_to_stage:
                        paths_to_stage.append(ep_clean)

            self._clean_stale_index_lock()
            self._run_git(["add", "--"] + paths_to_stage)
            self._run_git(["rm", "-r", "--cached", "generator/__pycache__"], check=False)
            self._run_git(["rm", "-r", "--cached", "__pycache__"], check=False)

            status_out = self._run_git(["status", "--porcelain"], check=False)

            if not status_out or not status_out.strip():
                head_sha = self._run_git(["rev-parse", "HEAD"], check=False)[:12] or "clean_tree_sha"
                return {
                    "committed": False,
                    "pushed": False,
                    "commit_sha": head_sha,
                    "commit_message": msg,
                    "message": "No new changes detected to commit."
                }

            commit_out = self._run_git(["commit", "-m", msg])
            commit_sha = self._run_git(["rev-parse", "HEAD"])[:12]

            if not push_enabled:
                return {
                    "committed": True,
                    "pushed": False,
                    "commit_sha": commit_sha,
                    "commit_message": msg,
                    "message": "Git push disabled for safety."
                }

            # Push commit to origin main
            auth_token = None
            if isinstance(self.repo, CloudRepository):
                auth_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("AGENT_AUTH_TOKEN")
                if not auth_token:
                    try:
                        from backend.services.github_auth import GitHubTokenManager
                        auth_token = GitHubTokenManager().get_installation_access_token()
                    except Exception:
                        auth_token = None

                # If we have a non-mock token, update the remote URL to embed it so git can push non-interactively.
                if auth_token and not str(auth_token).startswith("ghs_mock"):
                    auth_url = settings.musicdata_git_url(auth_token=auth_token)
                    self._run_git(["remote", "set-url", "origin", auth_url], check=False)
                else:
                    # If no usable token is available, surface a clearer error before attempting `git push`.
                    remote_url = self._run_git(["config", "--get", "remote.origin.url"], check=False) or ""
                    if "github.com" in remote_url and "@" not in remote_url:
                        raise GitSyncError(
                            message=(
                                "No GitHub authentication available for non-interactive push. "
                                "Set the `GITHUB_TOKEN` or `AGENT_AUTH_TOKEN` environment variable, "
                                "or configure a GitHub App via GITHUB_APP_ID/GITHUB_APP_INSTALLATION_ID/GITHUB_APP_PRIVATE_KEY."
                            ),
                            stage="git_push_auth",
                            exit_code=128,
                        )

            push_timeout = getattr(settings, "GIT_PUSH_TIMEOUT_SECONDS", 180)
            try:
                push_out = self._run_git(["push", "origin", settings.GITHUB_BRANCH], timeout=push_timeout)
            except GitSyncError as push_err:
                # Task 4 & Task 5: Post-timeout Remote Verification
                # If git push timed out or raised an error, verify whether the commit actually reached remote HEAD!
                remote_after_sha = self.get_remote_head_sha()
                if remote_after_sha and (remote_after_sha == commit_sha or remote_after_sha.startswith(commit_sha[:7])):
                    print(f"[GIT SYNC] Push timed out or errored locally, but remote HEAD '{remote_after_sha[:7]}' matches local commit SHA '{commit_sha[:7]}'. Verified push SUCCESS.")
                    return {
                        "committed": True,
                        "pushed": True,
                        "commit_sha": commit_sha,
                        "commit_message": msg,
                        "push_log": f"Verified via remote HEAD check: {remote_after_sha[:7]}"
                    }
                push_err.stage = "git_push"
                raise push_err

            return {
                "committed": True,
                "pushed": True,
                "commit_sha": commit_sha,
                "commit_message": msg,
                "push_log": push_out
            }

LocalGitSyncService = BaseGitSyncService
CloudGitSyncService = BaseGitSyncService
GitSyncService = BaseGitSyncService
