import os
from pathlib import Path

class Settings:
    def __init__(self):
        self.MUSICDATA_REPOSITORY_ROOT: str = os.environ.get("MUSICDATA_REPOSITORY_ROOT", "")
        self.GITHUB_OWNER: str = os.environ.get("GITHUB_OWNER", "vamshivamshi9630")
        self.GITHUB_REPOSITORY: str = os.environ.get("GITHUB_REPOSITORY", "MusicData")
        self.GITHUB_BRANCH: str = os.environ.get("GITHUB_BRANCH", "main")
        self.SECRET_KEY: str = os.environ.get("SECRET_KEY", "musicdata-secret-key-change-in-production")
        self.API_PORT: int = int(os.environ.get("API_PORT", 8000))
        self.HOST: str = os.environ.get("HOST", "0.0.0.0")

settings = Settings()
