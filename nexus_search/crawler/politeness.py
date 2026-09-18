"""Robots.txt compliance and per-domain rate limiting for the crawler."""
import threading
import time
import urllib.robotparser
from typing import Dict, Optional


class PolitenessManager:
    """Caches robots.txt rules per domain and enforces crawl delay.

    Thread-safe: the fetcher runs multiple worker threads, and several of
    them may touch the same domain's rate-limit state concurrently.
    """

    def __init__(self, user_agent: str = "NexusSearchBot/0.1", default_delay: float = 1.0):
        self.user_agent = user_agent
        self.default_delay = default_delay
        self._parsers: Dict[str, urllib.robotparser.RobotFileParser] = {}
        self._last_request: Dict[str, float] = {}
        self._lock = threading.Lock()

    def register_robots_txt(self, domain: str, content: str):
        """Call once per domain after fetching (or failing to fetch) /robots.txt.
        Pass an empty string if the fetch failed — this fails open, matching
        the convention that a missing robots.txt means "crawling is allowed."
        """
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(content.splitlines())
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
        return parser.can_fetch(self.user_agent, url)

    def crawl_delay(self, domain: str) -> float:
        with self._lock:
            parser = self._parsers.get(domain)
        if parser is not None:
            delay = parser.crawl_delay(self.user_agent)
            if delay is not None:
                return float(delay)
        return self.default_delay

    def time_until_allowed(self, domain: str) -> float:
        """Seconds to wait before this domain may be hit again. 0 if clear now."""
        delay = self.crawl_delay(domain)
        with self._lock:
            last = self._last_request.get(domain)
        if last is None:
            return 0.0
        elapsed = time.time() - last
        return max(0.0, delay - elapsed)

    def record_request(self, domain: str):
        with self._lock:
            self._last_request[domain] = time.time()
