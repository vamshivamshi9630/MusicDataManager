import os
import subprocess
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from backend.core.repository import IRepositoryProvider, LocalRepository, CloudRepository
from backend.core.config import settings

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
    def stage_commit_and_push(self, album_name: str, mode: str = "add", custom_commit_msg: str = "", push_enabled: bool = True) -> Dict[str, Any]:
        pass

class BaseGitSyncService(IGitSyncProvider):
    def __init__(self, repo_context: IRepositoryProvider):
        self.repo = repo_context

    def _run_git(self, args: list, check: bool = True, timeout: int = 35) -> str:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
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

    def stage_commit_and_push(self, album_name: str, mode: str = "add", custom_commit_msg: str = "", push_enabled: bool = True) -> Dict[str, Any]:
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
            msg = f"edited songs/album with {album_name}"
        else:
            msg = f"added songs/album with {album_name}"

        # Stage all intended album and metadata changes
        self._run_git(["add", "."])
        
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
                auth_url = f"https://x-access-token:{auth_token}@github.com/{settings.GITHUB_OWNER}/{settings.GITHUB_REPOSITORY}.git"
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

        try:
            push_out = self._run_git(["push", "origin", settings.GITHUB_BRANCH], timeout=35)
        except GitSyncError as e:
            e.stage = "git_push"
            raise e

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
