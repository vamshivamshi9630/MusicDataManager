import os
from pathlib import Path

class Settings:
    def __init__(self):
        self.MUSICDATA_REPOSITORY_ROOT: str = os.environ.get("MUSICDATA_REPOSITORY_ROOT", "")
        self.GITHUB_OWNER: str = os.environ.get("GITHUB_OWNER", "vamshivamshi9630")
        self.GITHUB_REPOSITORY: str = os.environ.get("GITHUB_REPOSITORY", "MusicData")
        self.GITHUB_BRANCH: str = os.environ.get("GITHUB_BRANCH", "main")
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

settings = Settings()
