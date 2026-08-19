import os
import subprocess
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from backend.core.repository import IRepositoryProvider, LocalRepository, CloudRepository
from backend.core.config import settings

class GitSyncError(Exception):
    pass

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

    def _run_git(self, args: list, check: bool = True) -> str:
        try:
            res = subprocess.run(
                ["git"] + args,
                cwd=self.repo.root,
                capture_output=True,
                text=True,
                check=check
            )
            return res.stdout.strip()
        except subprocess.CalledProcessError as e:
            raise GitSyncError(f"Git command 'git {' '.join(args)}' failed: {e.stderr or e.stdout}")

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
            self._run_git(["fetch", "origin", settings.GITHUB_BRANCH], check=False)
            remote_head = self._run_git(["parse-remote", f"origin/{settings.GITHUB_BRANCH}"], check=False)[:12]
            local_head = self._run_git(["rev-parse", "HEAD"], check=False)[:12]
            return {
                "local_head": local_head,
                "remote_head": remote_head,
                "in_sync": local_head == remote_head if remote_head else True
            }
        except Exception as e:
            return {"error": str(e), "in_sync": False}

    def stage_commit_and_push(self, album_name: str, mode: str = "add", custom_commit_msg: str = "", push_enabled: bool = True) -> Dict[str, Any]:
        # Exact commit message rule:
        # ADD mode: "added songs/album with <Album Name>"
        # EDIT mode: "edited songs/album with <Album Name>"
        if custom_commit_msg and custom_commit_msg.strip():
            msg = custom_commit_msg.strip()
        elif mode == "edit":
            msg = f"edited songs/album with {album_name}"
        else:
            msg = f"added songs/album with {album_name}"

        # Stage all changes (album folder + PNG artwork + MP3s + generated metadata/indexes)
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

        push_out = self._run_git(["push", "origin", settings.GITHUB_BRANCH])

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
