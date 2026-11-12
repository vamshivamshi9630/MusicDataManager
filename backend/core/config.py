import os
import urllib.parse
from pathlib import Path
from typing import Optional


def resolve_musicdata_github_repository() -> str:
    """
    Resolves the MusicData catalog repository name safely.
    Uses dedicated MUSICDATA_GITHUB_REPOSITORY first and ignores GitHub Actions'
    predefined GITHUB_REPOSITORY (<owner>/<app_repo>) collision.
    """
    explicit = os.environ.get("MUSICDATA_GITHUB_REPOSITORY", "").strip()
    if explicit:
        cleaned = explicit.strip("/")
        if "/" in cleaned:
            return cleaned.split("/")[-1].strip() or "MusicData"
        return cleaned

    # Safe backward compatibility only outside GitHub Actions and when unambiguous
    in_gh_actions = os.environ.get("GITHUB_ACTIONS", "").strip().lower() in ("1", "true")
    if not in_gh_actions:
        legacy = os.environ.get("GITHUB_REPOSITORY", "").strip().strip("/")
        if legacy and "/" not in legacy and legacy.lower() != "musicdatamanager":
            return legacy

    return "MusicData"


class Settings:
    def __init__(self):
        self.MUSICDATA_REPOSITORY_ROOT: str = os.environ.get("MUSICDATA_REPOSITORY_ROOT", "")
        self.GITHUB_OWNER: str = os.environ.get("GITHUB_OWNER", "vamshivamshi9630").strip() or "vamshivamshi9630"
        self.MUSICDATA_GITHUB_REPOSITORY: str = resolve_musicdata_github_repository()
        self.GITHUB_BRANCH: str = os.environ.get("GITHUB_BRANCH", "main").strip() or "main"
        # Optional authentication sources for non-interactive git push
        self.GITHUB_TOKEN: str = os.environ.get("GITHUB_TOKEN", "")
        self.AGENT_AUTH_TOKEN: str = os.environ.get("AGENT_AUTH_TOKEN", "")

        # GitHub App credentials (optional) for installation token flow
        self.GITHUB_APP_ID: str = os.environ.get("GITHUB_APP_ID", "")
        self.GITHUB_APP_INSTALLATION_ID: str = os.environ.get("GITHUB_APP_INSTALLATION_ID", "")
        self.GITHUB_APP_PRIVATE_KEY: str = os.environ.get("GITHUB_APP_PRIVATE_KEY", "")
        self.SECRET_KEY: str = os.environ.get("SECRET_KEY", "musicdata-secret-key-change-in-production")
        self.API_PORT: int = int(os.environ.get("API_PORT", 8000))
        self.HOST: str = os.environ.get("HOST", "0.0.0.0")

        # Configurable Git Timeouts (Seconds)
        self.GIT_PUSH_TIMEOUT_SECONDS: int = int(os.environ.get("GIT_PUSH_TIMEOUT_SECONDS", 180))
        self.GIT_CLONE_TIMEOUT_SECONDS: int = int(os.environ.get("GIT_CLONE_TIMEOUT_SECONDS", 60))
        self.GIT_OPERATION_TIMEOUT_SECONDS: int = int(os.environ.get("GIT_OPERATION_TIMEOUT_SECONDS", 45))

        # Latest Releases Discovery Engine & Database Settings
        self.DATABASE_URL: str = os.environ.get("DATABASE_URL", "")
        self.LATEST_RELEASES_DB_PATH: str = os.environ.get("LATEST_RELEASES_DB_PATH", "data/latest_releases.db")
        self.CRON_SECRET: str = os.environ.get("CRON_SECRET", "musicdata-cron-secret-change-in-production")
        self.LATEST_RELEASES_ENABLED: bool = os.environ.get("LATEST_RELEASES_ENABLED", "1").lower() in ("1", "true")
        self.LATEST_RELEASES_MAX_PAGES: int = int(os.environ.get("LATEST_RELEASES_MAX_PAGES", 20))

    @property
    def GITHUB_REPOSITORY(self) -> str:
        """Deprecated alias retained only for external test/script compatibility."""
        return self.MUSICDATA_GITHUB_REPOSITORY

    @GITHUB_REPOSITORY.setter
    def GITHUB_REPOSITORY(self, value: str):
        cleaned = (value or "").strip().strip("/")
        if cleaned:
            self.MUSICDATA_GITHUB_REPOSITORY = cleaned.split("/")[-1] if "/" in cleaned else cleaned

    @property
    def musicdata_repo_slug(self) -> str:
        return f"{self.GITHUB_OWNER}/{self.MUSICDATA_GITHUB_REPOSITORY}"

    @property
    def musicdata_raw_base_url(self) -> str:
        return f"https://raw.githubusercontent.com/{self.GITHUB_OWNER}/{self.MUSICDATA_GITHUB_REPOSITORY}/{self.GITHUB_BRANCH}"

    def build_raw_github_url(self, relative_path: str, already_quoted: bool = False) -> str:
        clean = (relative_path or "").lstrip("/")
        encoded = clean if already_quoted else urllib.parse.quote(clean, safe="/:")
        return f"{self.musicdata_raw_base_url}/{encoded}"

    def musicdata_git_url(self, auth_token: Optional[str] = None) -> str:
        if auth_token:
            return f"https://x-access-token:{auth_token}@github.com/{self.musicdata_repo_slug}.git"
        return f"https://github.com/{self.musicdata_repo_slug}.git"

    def refresh_from_env(self):
        self.GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "vamshivamshi9630").strip() or "vamshivamshi9630"
        self.MUSICDATA_GITHUB_REPOSITORY = resolve_musicdata_github_repository()
        self.GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main").strip() or "main"


settings = Settings()
