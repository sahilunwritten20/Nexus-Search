"""HTTP fetcher: retries (network errors AND 429/5xx), ETag/Last-Modified,
redirect-hop validation (SSRF), response size cap, HTTP error statuses."""

import contextlib
import time
from dataclasses import dataclass
from typing import Callable, ContextManager, Optional
from urllib.parse import urljoin

import requests

RETRY_STATUS = {429, 500, 502, 503, 504}
REDIRECT_STATUS = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 5
MAX_BYTES = 5 * 1024 * 1024
MAX_BACKOFF = 30.0


class FetchBlocked(Exception):
    """A redirect led somewhere we refuse to go (or redirected too often)."""


class TooLarge(Exception):
    pass


@dataclass
class FetchResult:
    url: str
    status_code: Optional[int]
    html: Optional[str]
    error: Optional[str]
    elapsed: float
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    not_modified: bool = False
    final_url: Optional[str] = None


class Fetcher:
    def __init__(
        self,
        user_agent: str = "NexusSearchBot/0.1 (+https://example.com/bot)",
        timeout: float = 10.0,
        max_retries: int = 2,
        url_validator: Optional[Callable[[str], bool]] = None,
        max_bytes: int = MAX_BYTES,
        dns_pin: Optional[Callable[[str], ContextManager]] = None,
    ):
        self.timeout = timeout
        self.max_retries = max_retries
        self.url_validator = url_validator  # e.g. security.validate_url
        self.max_bytes = max_bytes
        self.dns_pin = dns_pin  # e.g. security.pin_dns_for_url
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})

    # ---------------------------------------------------------- internals

    def _get(self, url: str, headers: dict):
        """GET, following redirects by hand so EVERY hop is validated."""
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            pin = self.dns_pin(current) if self.dns_pin else contextlib.nullcontext()
            with pin:
                resp = self.session.get(
                    current, timeout=self.timeout, allow_redirects=False,
                    headers=headers, stream=True,
                )
            if resp.status_code in REDIRECT_STATUS:
                location = resp.headers.get("Location")
                resp.close()
                if not location:
                    raise FetchBlocked("redirect without Location header")
                current = urljoin(current, location)
                if self.url_validator and not self.url_validator(current):
                    raise FetchBlocked(f"redirect to disallowed URL: {current}")
                continue
            return resp, current
        raise FetchBlocked("too many redirects")

    def _read_text(self, resp) -> str:
        declared = str(resp.headers.get("content-length", ""))
        if declared.isdigit() and int(declared) > self.max_bytes:
            raise TooLarge(f"content-length {declared} exceeds {self.max_bytes}")
        try:
            chunks, size = [], 0
            for chunk in resp.iter_content(chunk_size=65536):
                size += len(chunk)
                if size > self.max_bytes:
                    raise TooLarge(f"body exceeds {self.max_bytes} bytes")
                chunks.append(chunk)
        except TypeError:  # test doubles that only provide .text
            return resp.text
        finally:
            resp.close()
        content_type = str(resp.headers.get("content-type", "")).lower()
        charset = "utf-8"
        if "charset=" in content_type:
            charset = content_type.split("charset=")[1].split(";")[0].strip() or "utf-8"
        try:
            return b"".join(chunks).decode(charset, errors="replace")
        except LookupError:
            return b"".join(chunks).decode("utf-8", errors="replace")

    @staticmethod
    def _backoff(attempt: int, retry_after: Optional[str] = None) -> float:
        if retry_after and str(retry_after).isdigit():
            return min(float(retry_after), MAX_BACKOFF)
        return min(2.0 ** attempt, MAX_BACKOFF)

    # ---------------------------------------------------------------- API

    def fetch(
        self,
        url: str,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
    ) -> FetchResult:
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        last_error = None
        elapsed = 0.0

        for attempt in range(self.max_retries + 1):
            start = time.monotonic()
            try:
                resp, final_url = self._get(url, headers)
                elapsed = time.monotonic() - start
                status = resp.status_code
                resp_etag = resp.headers.get("ETag")
                resp_lm = resp.headers.get("Last-Modified")

                if status == 304:
                    resp.close()
                    return FetchResult(
                        url=url, status_code=304, html=None, error=None, elapsed=elapsed,
                        etag=resp_etag or etag, last_modified=resp_lm or last_modified,
                        not_modified=True, final_url=final_url,
                    )

                if status in RETRY_STATUS and attempt < self.max_retries:
                    retry_after = resp.headers.get("Retry-After")
                    resp.close()
                    last_error = f"HTTP {status}"
                    time.sleep(self._backoff(attempt, retry_after))
                    continue

                if status >= 400:  # error pages must never be indexed as content
                    resp.close()
                    return FetchResult(url=url, status_code=status, html=None,
                                        error=f"HTTP {status}", elapsed=elapsed, final_url=final_url)

                content_type = resp.headers.get("content-type", "")
                if "text/html" not in content_type.lower():
                    resp.close()
                    return FetchResult(
                        url=url, status_code=status, html=None,
                        error=f"non-HTML content-type: {content_type}",
                        elapsed=elapsed, etag=resp_etag, last_modified=resp_lm, final_url=final_url,
                    )

                html = self._read_text(resp)
                return FetchResult(
                    url=url, status_code=status, html=html, error=None, elapsed=elapsed,
                    etag=resp_etag, last_modified=resp_lm, final_url=final_url,
                )

            except (FetchBlocked, TooLarge) as exc:  # deliberate refusals: don't retry
                return FetchResult(url=url, status_code=None, html=None, error=str(exc),
                                   elapsed=time.monotonic() - start)
            except requests.RequestException as exc:
                last_error = str(exc)
                elapsed = time.monotonic() - start
                if attempt < self.max_retries:
                    time.sleep(self._backoff(attempt))

        return FetchResult(url=url, status_code=None, html=None, error=last_error, elapsed=elapsed)

    def fetch_robots(self, url: str) -> Optional[str]:
        """robots.txt text. '' = none exists (4xx -> everything allowed);
        None = unreachable / 5xx (caller should be conservative)."""
        try:
            resp, _ = self._get(url, {})
            if resp.status_code == 200:
                return self._read_text(resp)[:512_000]
            resp.close()
            return "" if 400 <= resp.status_code < 500 else None
        except (FetchBlocked, TooLarge, requests.RequestException):
            return None

    def fetch_text(self, url: str) -> str:
        """Fetch a simple text resource (sitemaps etc.). '' on any failure."""
        try:
            resp, _ = self._get(url, {})
            if resp.status_code == 200:
                return self._read_text(resp)
            resp.close()
        except (FetchBlocked, TooLarge, requests.RequestException):
            pass
        return ""

    def close(self):
        self.session.close()