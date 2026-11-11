import time
import urllib.request
import urllib.error
import logging
from typing import Optional

logger = logging.getLogger("musicdata.latest_releases.source_client")

class SourceClient:
    """
    HTTP client for querying naasongs.com.co safely, respectfully, and with timeout protection.
    Follows rate limits, timeouts, and exponential backoff on transient errors.
    """

    BASE_URL = "https://naasongs.com.co"
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

    def __init__(self, request_delay: float = 0.5, timeout: int = 15):
        self.request_delay = request_delay
        self.timeout = timeout
        self.last_request_time = 0.0

    def _wait_for_rate_limit(self):
        elapsed = time.time() - self.last_request_time
        if elapsed < self.request_delay:
            time.sleep(self.request_delay - elapsed)
        self.last_request_time = time.time()

    def fetch_html(self, url: str, max_retries: int = 2) -> Optional[str]:
        self._wait_for_rate_limit()

        for attempt in range(max_retries + 1):
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": self.USER_AGENT,
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "en-US,en;q=0.5",
                        "Referer": self.BASE_URL
                    }
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    if resp.status == 200:
                        content_bytes = resp.read()
                        # Decode utf-8 safely
                        return content_bytes.decode("utf-8", errors="ignore")
                    logger.warning(f"Unexpected HTTP status {resp.status} for {url}")
            except urllib.error.HTTPError as e:
                logger.warning(f"HTTPError {e.code} fetching {url}: {e.reason}")
                if e.code in (429, 503):
                    time.sleep(2.0 * (attempt + 1))
                elif e.code in (404, 403):
                    return None
            except Exception as e:
                logger.warning(f"Attempt {attempt + 1} failed fetching {url}: {e}")
                time.sleep(1.0 * (attempt + 1))

        return None

    def fetch_homepage(self) -> Optional[str]:
        return self.fetch_html(self.BASE_URL)

    def fetch_page(self, page_number: int) -> Optional[str]:
        if page_number <= 1:
            return self.fetch_homepage()
        return self.fetch_html(f"{self.BASE_URL}/page/{page_number}/")

    def fetch_album_page(self, album_url: str) -> Optional[str]:
        return self.fetch_html(album_url)
