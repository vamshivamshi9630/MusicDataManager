import time
import threading
import urllib.request
import urllib.error
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger("musicdata.latest_releases.source_client")

class SourceClient:
    """
    HTTP client for querying naasongs.com.co safely, respectfully, and with timeout protection.
    Follows rate limits, timeouts, and exponential backoff on transient errors.
    """

    BASE_URL = "https://naasongs.com.co"
    USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

    def __init__(self, request_delay: float = 0.15, timeout: int = 15):
        self.request_delay = request_delay
        self.timeout = timeout
        self.last_request_time = 0.0
        self._rate_lock = threading.Lock()
        self._next_allowed_time = 0.0
        self.last_fetch_meta: Dict[str, Any] = {}

    def _wait_for_rate_limit(self):
        sleep_for = 0.0
        with self._rate_lock:
            now = time.time()
            if now < self._next_allowed_time:
                sleep_for = self._next_allowed_time - now
                self._next_allowed_time += self.request_delay
            else:
                self._next_allowed_time = now + self.request_delay
            self.last_request_time = self._next_allowed_time
        if sleep_for > 0:
            time.sleep(sleep_for)

    def fetch_html(self, url: str, max_retries: int = 2) -> Optional[str]:
        self._wait_for_rate_limit()
        last_status: Optional[int] = None
        last_error: Optional[str] = None

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
                    last_status = resp.status
                    final_url = resp.geturl()
                    if resp.status == 200:
                        content_bytes = resp.read()
                        self.last_fetch_meta = {
                            "status_code": 200,
                            "requested_url": url,
                            "final_url": final_url,
                            "error": None,
                        }
                        # Decode utf-8 safely
                        return content_bytes.decode("utf-8", errors="ignore")
                    last_error = f"HTTP {resp.status}"
                    logger.warning(f"Unexpected HTTP status {resp.status} for {url}")
            except urllib.error.HTTPError as e:
                last_status = e.code
                last_error = f"HTTP {e.code}: {e.reason}"
                logger.warning(f"HTTPError {e.code} fetching {url}: {e.reason}")
                if e.code in (429, 503):
                    time.sleep(2.0 * (attempt + 1))
                elif e.code in (404, 403, 410):
                    self.last_fetch_meta = {
                        "status_code": e.code,
                        "requested_url": url,
                        "final_url": getattr(e, "url", url),
                        "error": last_error,
                    }
                    return None
            except Exception as e:
                last_error = str(e)
                logger.warning(f"Attempt {attempt + 1} failed fetching {url}: {e}")
                if attempt < max_retries:
                    time.sleep(1.0 * (attempt + 1))

        self.last_fetch_meta = {
            "status_code": last_status,
            "requested_url": url,
            "final_url": None,
            "error": last_error,
        }
        return None

    def fetch_homepage(self) -> Optional[str]:
        return self.fetch_html(f"{self.BASE_URL}/")

    def fetch_page(self, page_number: int, page_url: Optional[str] = None) -> Optional[str]:
        if page_url:
            return self.fetch_html(page_url)
        if page_number <= 1:
            return self.fetch_homepage()
        return self.fetch_html(f"{self.BASE_URL}/page/{page_number}")

    def fetch_album_page(self, album_url: str) -> Optional[str]:
        return self.fetch_html(album_url)
