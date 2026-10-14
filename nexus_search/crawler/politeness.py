"""Robots.txt compliance and per-domain rate limiting for the crawler.

RFC 9309 matching is OWNED (crawler/robots.py, review ROBOTS-WILDCARD):
the stdlib robotparser treats `*`/`$` as literals on Python 3.12 (the CI
runtime) and supports them only on 3.14+ — politeness behavior must not
depend on the interpreter version. The fail-closed-on-unreachable
contract below is unchanged: register_robots_txt("") means robots
confirmed ABSENT (4xx -> allowed); an UNREACHABLE robots.txt must be
registered by the caller as the synthetic disallow-everything ruleset.
"""
import threading
import time

from .robots import RobotsRules
from typing import Dict, Optional


class PolitenessManager:
    """Caches robots.txt rules per domain and enforces crawl delay.

    Thread-safe: the fetcher runs multiple worker threads, and several of
    them may touch the same domain's rate-limit state concurrently.
    """

    def __init__(self, user_agent: str = "NexusSearchBot/0.1", default_delay: float = 1.0):
        self.user_agent = user_agent
        self.default_delay = default_delay
        self._parsers: Dict[str, RobotsRules] = {}
        self._last_request: Dict[str, float] = {}
        self._lock = threading.Lock()

    def register_robots_txt(self, domain: str, content: str):
        """Call once per domain after fetching /robots.txt.

        `content` semantics, as produced by Fetcher.fetch_robots():
        - ""        -> robots.txt confirmed ABSENT (4xx): fail OPEN, matching
          the convention that a missing robots.txt means crawling is allowed.
        - anything  -> parse as robots.txt rules normally.
        IMPORTANT: an UNREACHABLE robots.txt (5xx, timeout, DNS failure) is
        NOT passed here as "". The caller (crawler/pipeline.py::_ensure_robots)
        makes that decision itself and fails CLOSED by registering a
        synthetic "_ROBOTS_UNREACHABLE" ruleset that disallows the whole
        domain for the run. This class does not fail open on fetch errors;
        do not change _ensure_robots without understanding that contract.
        """
        rp = RobotsRules(content)
        with self._lock:
            self._parsers[domain] = rp

    def has_robots(self, domain: str) -> bool:
        with self._lock:
            return domain in self._parsers

    def is_allowed(self, domain: str, url: str) -> bool:
        with self._lock:
            parser = self._parsers.get(domain)
        if parser is None:
            return True  # not fetched yet — caller should register_robots_txt first
        return parser.is_allowed(self.user_agent, url)

    def crawl_delay(self, domain: str) -> float:
        with self._lock:
            parser = self._parsers.get(domain)
        if parser is not None:
            delay = parser.crawl_delay(self.user_agent)
            if delay is not None:
                return float(delay)
        return self.default_delay

    def time_until_allowed(self, domain: str) -> float:
        """Seconds to wait before this domain may be hit again. 0 if clear now.

        Runs on the MONOTONIC clock (review POLITENESS-MONO): a wall-clock
        step (NTP correction) must not collapse or inflate the delay a site
        owner asked for."""
        delay = self.crawl_delay(domain)
        with self._lock:
            last = self._last_request.get(domain)
        if last is None:
            return 0.0
        elapsed = time.monotonic() - last
        return max(0.0, delay - elapsed)

    def reserve_slot(self, domain: str) -> float:
        """Atomically claim the next request slot for this domain and return
        how long the caller must sleep before using it. Concurrent workers get
        DIFFERENT slots, so the crawl delay holds under concurrency."""
        delay = self.crawl_delay(domain)  # takes the lock itself, so call it first
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._last_request.get(domain, 0.0) + delay)
            self._last_request[domain] = slot
            return slot - now

    def sitemaps(self, domain: str) -> list[str]:
        """Sitemap: lines declared in this domain's robots.txt."""
        with self._lock:
            parser = self._parsers.get(domain)
        return list(parser.sitemaps) if parser is not None else []

    def record_request(self, domain: str):
        with self._lock:
            self._last_request[domain] = time.monotonic()