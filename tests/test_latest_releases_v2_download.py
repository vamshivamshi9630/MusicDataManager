import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
from backend.main import app
from backend.api.latest_releases_routes import is_domain_allowed, sanitize_filename

client = TestClient(app)

class TestLatestReleasesV2Download:

    def test_domain_authorization_whitelist(self):
        # Allowed providers
        assert is_domain_allowed("https://naasongs.com.co/songs/track1.mp3") is True
        assert is_domain_allowed("https://mp3teluguwap.net/download/song.mp3") is True
        assert is_domain_allowed("https://sensongsmp3.tv/audio/song.mp3") is True
        assert is_domain_allowed("https://cdn.naasongs.com.co/audio/song.mp3") is True

        # Disallowed / dangerous hosts
        assert is_domain_allowed("http://localhost:8000/secret.mp3") is False
        assert is_domain_allowed("http://127.0.0.1/admin.mp3") is False
        assert is_domain_allowed("http://192.168.1.1/router.mp3") is False
        assert is_domain_allowed("http://10.0.0.1/internal.mp3") is False
        assert is_domain_allowed("https://malicious-site.com/track.mp3") is False

    def test_filename_sanitization(self):
        # Illegal characters in filenames (: \ / * ? " < > |)
        raw_name = 'Pushpa 2: The Rule / Intro *Song?'
        clean = sanitize_filename(raw_name)
        assert ":" not in clean
        assert "/" not in clean
        assert "*" not in clean
        assert "?" not in clean
        assert clean == "Pushpa 2 The Rule Intro Song"

    def test_unauthorized_download_request_rejected(self):
        resp = client.get("/api/latest-releases/download-track?url=https://unauthorized-domain.com/hack.mp3")
        assert resp.status_code == 400
        data = resp.json()
        assert "Unauthorized download source" in (data.get("error") or data.get("detail") or "")

    @patch("backend.api.latest_releases_routes.urllib.request.urlopen")
    def test_authorized_download_streaming_response(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.side_effect = [b"MP3DATACHUNK", b""]
        mock_urlopen.return_value = mock_response

        target_url = "https://naasongs.com.co/pushpa2/track1.mp3"
        resp = client.get(
            f"/api/latest-releases/download-track?url={target_url}&album_name=Pushpa%202&song_title=Pushpa%20Pushpa&track_number=1"
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "audio/mpeg"
        assert 'attachment; filename="01 - Pushpa Pushpa.mp3"' in resp.headers["content-disposition"]
        assert resp.content == b"MP3DATACHUNK"
