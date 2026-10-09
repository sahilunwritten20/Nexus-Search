"""HTTP fetcher: retries (network errors AND 429/5xx), ETag/Last-Modified,
redirect-hop validation (SSRF), response size cap, HTTP error statuses.

render_js (headless-browser rendering) is an EXPLICIT opt-in behind
NEXUS_RENDER_JS=1: a real browser navigates and loads subresources on its
own, which is a fundamentally bigger attack surface than plain HTTP.
See `_render_with_browser` for the protections and their residual limits.
"""

import contextlib
import logging
import os
import time
from dataclasses import dataclass
from typing import Callable, ContextManager, Optional
from urllib.parse import urljoin

import requests

logger = logging.getLogger("nexus_search.crawler.fetcher")


def _render_js_enabled() -> bool:
    """NEXUS_RENDER_JS=1 unlocks the headless-browser path (P1-7). The
    browser bypasses the plain-HTTP protections by design, so it must be
    a conscious operator decision, never a config-file default."""
    return (os.environ.get("NEXUS_RENDER_JS", "") or "").strip().lower() in (
        "1", "true", "yes")

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
        render_js: bool = False,
        render_js_allow_private: bool = False,
    ):
        self.timeout = timeout
        self.max_retries = max_retries
        self.url_validator = url_validator  # e.g. security.validate_url
        self.max_bytes = max_bytes
        self.dns_pin = dns_pin  # e.g. security.pin_dns_for_url
        self.render_js = bool(render_js)
        if render_js and not _render_js_enabled():
            logger.warning(
                "render_js=True ignored: set NEXUS_RENDER_JS=1 to opt into "
                "the headless-browser path (SSRF-hardened: every subrequest "
                "is route-validated, but DNS stays browser-resolved)")
            self.render_js = False
        # WP12-B7 (audit R7a): without a url_validator the browser path
        # validates NOTHING (route handler and final-URL check both
        # no-op), so refusing is the only safe default. The explicit
        # allow-private flag documents that the operator accepts an
        # UNVALIDATED browser (private networks reachable).
        if self.render_js and not url_validator and not render_js_allow_private:
            logger.warning(
                "render_js=True refused: no url_validator is configured, so "
                "subrequests and client-side redirects would be completely "
                "unvalidated (SSRF). Pass url_validator=..., or set "
                "render_js_allow_private=True to accept an unvalidated "
                "browser explicitly.")
            self.render_js = False
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})

    def _render_with_browser(self, url: str) -> Optional[str]:
        """Headless-browser render for JS-heavy pages (Playwright).

        SSRF protections (P1-7), in order:
        - OPT-IN: never runs unless NEXUS_RENDER_JS=1 (constructor warns and
          disables render_js otherwise), and only with a `url_validator`
          wired (WP12-B7: no validator = nothing is checked, so the
          constructor refuses; `render_js_allow_private=True` is the
          explicit unvalidated escape hatch).
        - Every HTTP(S) subrequest is intercepted with page.route("**/*")
          and validated with the same `validate_url` as plain fetches — a
          page cannot pull scripts/images/data from private networks.
        - Service workers are BLOCKED at the context level
          (service_workers="block"): page.route cannot observe SW-initiated
          fetches, so allowing SWs would open an unvalidated channel
          (WP12-B7, audit R7b).
        - WebSocket upgrades are blocked/continued with
          page.route_web_socket when the installed playwright supports it
          (playwright >= 1.44); older builds skip this and the RESIDUAL
          RISK (unrouted WS connections) is accepted behind the env gate
          — the same class of limitation as DNS below (WP12-B7).
        - The final URL after navigation is validated too (client-side
          redirects would otherwise be invisible).

        RESIDUAL RISKS (documented, accepted only behind the env gate):
        - Chromium resolves DNS on its own, so `pin_dns_for_url` CANNOT
          apply — a DNS-rebinding server can still pass validate_url's
          lookup and then answer the browser's second lookup from a
          private address.
        - On playwright builds without route_web_socket, ws:// upgrades
          are not interceptable by this layer.
        Enable NEXUS_RENDER_JS only for crawls of hosts you trust, or front
        the browser with an egress firewall.

        Deliberately OPTIONAL: Playwright+Chromium is a heavyweight browser,
        not a test dependency. If it isn't installed, we log the limitation
        and return None so the caller falls back to the plain HTTP body —
        honest degradation, matching the OCR path's stance (no fake DOM)."""
        if not _render_js_enabled():  # defense in depth: the gate also
            return None               # covers direct calls
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning("render_js requested but playwright is not "
                           "installed; falling back to plain HTTP GET for %s",
                           url)
            return None
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    # service_workers="block": SW fetches bypass page.route
                    # entirely, so the only safe setting is disabled.
                    context = browser.new_context(service_workers="block")
                    page = context.new_page(
                        user_agent=str(self.session.headers.get("user-agent")))

                    def _validate_route(route):
                        # every subrequest goes through the same SSRF gate
                        # as the page itself
                        target = route.request.url
                        if self.url_validator and not self.url_validator(target):
                            logger.warning("browser subrequest blocked "
                                           "(SSRF): %s", target)
                            route.abort()
                            return
                        route.continue_()

                    page.route("**/*", _validate_route)

                    # WP12-B7: WebSocket upgrades are NOT covered by
                    # page.route; route_web_socket (playwright >= 1.44)
                    # closes the gap on builds that have it. On older
                    # builds this is skipped and the residual risk is the
                    # documented trade above.
                    if hasattr(page, "route_web_socket"):
                        def _validate_ws(ws_route):
                            target = ws_route.url
                            # validate_url only knows http/https; map the
                            # WS schemes onto their HTTP equivalents so the
                            # HOST/SSRF rules apply unchanged.
                            check = (target.replace("wss://", "https://", 1)
                                     if target.startswith("wss://")
                                     else target.replace("ws://", "http://", 1))
                            if self.url_validator and not self.url_validator(check):
                                logger.warning("browser websocket blocked "
                                               "(SSRF): %s", target)
                                ws_route.abort()
                                return
                            ws_route.continue_()

                        try:
                            page.route_web_socket("**/*", _validate_ws)
                        except Exception:
                            logger.warning("route_web_socket registration "
                                           "failed; WS stays unvalidated for "
                                           "%s", url, exc_info=True)

                    page.goto(url, timeout=int(self.timeout * 1000),
                              wait_until="networkidle")
                    final_url = page.url
                    if self.url_validator and not self.url_validator(final_url):
                        logger.warning("JS render ended on a disallowed URL "
                                       "(SSRF): %s", final_url)
                        return None
                    return page.content()
                finally:
                    browser.close()
        except Exception as exc:
            logger.warning("JS render failed for %s: %s", url, exc)
            return None

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

    def _read_bytes(self, resp) -> bytes:
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
            return resp.text.encode("utf-8", errors="replace")
        finally:
            resp.close()
        return b"".join(chunks)

    def _read_text(self, resp) -> str:
        content_type = str(resp.headers.get("content-type", "")).lower()
        charset = "utf-8"
        if "charset=" in content_type:
            charset = content_type.split("charset=")[1].split(";")[0].strip() or "utf-8"
        raw = self._read_bytes(resp)
        try:
            return raw.decode(charset, errors="replace")
        except LookupError:
            return raw.decode("utf-8", errors="replace")

    def fetch_bytes(self, url: str) -> bytes:
        """Raw bytes (sitemaps may be *.xml.gz — decompression is the
        caller's job). b'' on any fetch refusal/failure."""
        try:
            resp, _ = self._get(url, {})
            if resp.status_code == 200:
                return self._read_bytes(resp)
            resp.close()
        except (FetchBlocked, TooLarge, requests.RequestException):
            pass
        return b""

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
                if self.render_js and html:
                    # Plain HTML always comes first; only a JS render attempt
                    # REPLACES it when the browser path actually produced one.
                    rendered = self._render_with_browser(final_url)
                    if rendered is not None and len(rendered.encode("utf-8", "ignore")) <= self.max_bytes:
                        html = rendered
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