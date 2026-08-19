import os
import time
import json
import urllib.request
import urllib.parse
from typing import Optional, Dict, Any
from backend.core.config import settings

class GitHubAuthError(Exception):
    pass

class GitHubTokenManager:
    """Server-side GitHub App Authentication & Installation Access Token Manager."""

    def __init__(self, test_mode: bool = False):
        self.test_mode = test_mode
        self._cached_token: Optional[str] = None
        self._expires_at: float = 0.0

    def get_installation_access_token(self) -> str:
        """Return cached token or fetch a new short-lived installation access token."""
        now = time.time()
        # Return cached token if valid for at least 5 more minutes
        if self._cached_token and (self._expires_at - now) > 300:
            return self._cached_token

        if self.test_mode or not (settings.GITHUB_APP_ID and settings.GITHUB_APP_INSTALLATION_ID):
            self._cached_token = "ghs_mock_installation_token_for_phase_c_testing_12345"
            self._expires_at = now + 3600
            return self._cached_token

        return self._fetch_live_installation_token()

    def _fetch_live_installation_token(self) -> str:
        url = f"https://api.github.com/app/installations/{settings.GITHUB_APP_INSTALLATION_ID}/access_tokens"
        jwt_token = self._generate_app_jwt()

        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "MusicData-Manager-Cloud-Worker"
        }

        req = urllib.request.Request(url, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                token = data.get("token")
                expires_at_str = data.get("expires_at")
                
                if not token:
                    raise GitHubAuthError("GitHub response missing installation token.")
                
                self._cached_token = token
                self._expires_at = time.time() + 3500
                return token
        except Exception as e:
            raise GitHubAuthError(f"GitHub App installation token request failed: {e}")

    def _generate_app_jwt(self) -> str:
        # Standard placeholder for JWT generation using GITHUB_APP_PRIVATE_KEY
        if not settings.GITHUB_APP_PRIVATE_KEY:
            raise GitHubAuthError("Missing GITHUB_APP_PRIVATE_KEY configuration.")
        return f"app_jwt_for_id_{settings.GITHUB_APP_ID}"

    def revoke_cached_token(self):
        self._cached_token = None
        self._expires_at = 0.0
