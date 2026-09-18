"""HTTP fetcher with retries and timeout handling.

Built on `requests` rather than an async client (httpx/aiohttp aren't
installable without network access in every environment) — concurrency
comes from running multiple Fetcher.fetch() calls across worker threads
in the pipeline instead of an event loop. Functionally equivalent for a
prototype-scale crawl; swap in httpx.AsyncClient later if you want true
async I/O at higher concurrency.
"""
import time
from dataclasses import dataclass
from typing import Optional

import requests


@dataclass
class FetchResult:
    url: str
    status_code: Optional[int]
    html: Optional[str]
    error: Optional[str]
    elapsed: float


class Fetcher:
    def __init__(
        self,
        user_agent: str = "NexusSearchBot/0.1 (+https://example.com/bot)",
        timeout: float = 10.0,
        max_retries: int = 2,
    ):
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})

    def fetch(self, url: str) -> FetchResult:
        last_error = None
        elapsed = 0.0
        for attempt in range(self.max_retries + 1):
            start = time.monotonic()
            try:
                resp = self.session.get(url, timeout=self.timeout, allow_redirects=True)
                elapsed = time.monotonic() - start
                content_type = resp.headers.get("content-type", "")
                if "text/html" not in content_type:
                    return FetchResult(url, resp.status_code, None, f"non-HTML content-type: {content_type}", elapsed)
                return FetchResult(url, resp.status_code, resp.text, None, elapsed)
            except requests.RequestException as e:
                last_error = str(e)
                elapsed = time.monotonic() - start
                if attempt < self.max_retries:
                    time.sleep(2 ** attempt)  # exponential backoff: 1s, 2s, ...
        return FetchResult(url, None, None, last_error, elapsed)

    def fetch_text(self, url: str) -> str:
        """Simple fetch for non-HTML text resources like robots.txt. Empty string on failure."""
        try:
            resp = self.session.get(url, timeout=self.timeout)
            return resp.text if resp.status_code == 200 else ""
        except requests.RequestException:
            return ""

    def close(self):
        self.session.close()
