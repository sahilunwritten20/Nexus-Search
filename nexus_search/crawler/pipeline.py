
"""Crawler pipeline for Nexus Search."""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional
from urllib.parse import urlsplit

from ..ingestion.connectors.web import extract_page
from ..ingestion.dedup import content_hash
from .fetcher import Fetcher
from .frontier import Frontier, FrontierEntry
from .metrics import CrawlerMetrics
from .politeness import PolitenessManager
from .security import pin_dns_for_url, validate_url
from .sitemap import parse_sitemap, parse_sitemap_index
from .url_utils import get_domain

logger = logging.getLogger("nexus_search.crawler.pipeline")

IngestFn = Callable[[str, str, str, dict], None]

DEFAULT_USER_AGENT = "NexusSearchBot/0.1 (+https://example.com/bot)"  # put YOUR bot page here

# Refuse to trust a robots.txt we couldn't fetch: block the domain for this run.
_ROBOTS_UNREACHABLE = "User-agent: *\nDisallow: /\n"


def default_ingest(url: str, title: str, text: str, metadata: dict) -> None:
    logger.info("INGEST %s (%d chars)", url, len(text))


class CrawlPipeline:
    def __init__(
        self,
        db_path: str = "crawler.db",
        allowed_domains: Optional[list[str]] = None,
        max_pages: int = 1000,
        max_depth: int = 3,
        concurrency: int = 8,
        ingest_fn: IngestFn = default_ingest,
        max_pages_per_domain: int = 0,
        recrawl_interval: float = 0,
        allow_private_hosts: bool = False,
        default_crawl_delay: float = 1.0,
        user_agent: str = DEFAULT_USER_AGENT,
    ):
        self.frontier = Frontier(db_path)
        self.politeness = PolitenessManager(
            user_agent=user_agent.split()[0].split("/")[0],  # robots.txt product token
            default_delay=default_crawl_delay,
        )
        self.allow_private_hosts = allow_private_hosts
        self.fetcher = Fetcher(
            user_agent=user_agent,
            url_validator=None if allow_private_hosts else validate_url,  # checks redirect hops too
            dns_pin=None if allow_private_hosts else pin_dns_for_url,  # closes DNS-rebinding window
        )
        self.allowed_domains = set(allowed_domains or [])
        self.max_pages = max_pages
        self.max_depth = max_depth
        self.concurrency = max(1, concurrency)
        self.ingest_fn = ingest_fn
        self.max_pages_per_domain = max_pages_per_domain
        self.recrawl_interval = recrawl_interval
        self.metrics = CrawlerMetrics()
        self.domain_counts: dict[str, int] = {}
        self.domain_lock = threading.Lock()
        self.stats = {"crawled": 0, "skipped": 0, "errors": 0, "not_modified": 0}
        self._stats_lock = threading.Lock()
        self._closed = False

    # ------------------------------------------------------------ helpers

    def _bump(self, key: str, nbytes: int = 0) -> None:
        with self._stats_lock:
            self.stats[key] += 1
        if key == "crawled":
            self.metrics.record_crawled(nbytes)
        elif key == "skipped":
            self.metrics.record_skipped()
        elif key == "errors":
            self.metrics.record_error()
        elif key == "not_modified":
            self.metrics.record_not_modified()

    def _domain_allowed(self, domain: str) -> bool:
        if not self.allowed_domains:
            return True
        return any(domain == a or domain.endswith("." + a) for a in self.allowed_domains)

    def _reserve_domain_slot(self, domain: str) -> bool:
        if self.max_pages_per_domain <= 0:
            return True
        with self.domain_lock:
            current = self.domain_counts.get(domain, 0)
            if current >= self.max_pages_per_domain:
                return False
            self.domain_counts[domain] = current + 1
            return True

    def _ensure_robots(self, url: str, domain: str) -> None:
        if self.politeness.has_robots(domain):
            return
        parts = urlsplit(url)
        content = self.fetcher.fetch_robots(f"{parts.scheme}://{parts.netloc}/robots.txt")
        if content is None:
            logger.warning("robots.txt unreachable for %s - treating as disallowed this run", domain)
            content = _ROBOTS_UNREACHABLE
        self.politeness.register_robots_txt(domain, content)

    # --------------------------------------------------------------- seeds

    def seed(self, urls: list[str]) -> None:
        for url in urls:
            if not self.allow_private_hosts and not validate_url(url):
                logger.warning("Ignoring unsafe seed: %s", url)
                continue
            self.frontier.add(url, depth=0, priority=10)

    def seed_from_sitemap(self, sitemap_url: str, max_sitemaps: int = 10, max_urls: int = 5000) -> int:
        """Queue page URLs from a sitemap (following sitemap-index files)."""
        queue, seen, added = [sitemap_url], set(), 0
        while queue and len(seen) < max_sitemaps:
            url = queue.pop(0)
            if url in seen or (not self.allow_private_hosts and not validate_url(url)):
                continue
            seen.add(url)
            xml = self.fetcher.fetch_text(url)
            queue.extend(parse_sitemap_index(xml))
            for loc in parse_sitemap(xml):
                if added >= max_urls:
                    break
                if self._domain_allowed(get_domain(loc)) and self.frontier.add(loc, depth=0, priority=5):
                    added += 1
        return added

    # ------------------------------------------------------------- crawling

    def _skip(self, entry: FrontierEntry, reason: str) -> None:
        logger.info("SKIP %s (%s)", entry.url, reason)
        self.frontier.mark_skipped(entry.url)  # NOT marked visited
        self._bump("skipped")

    def _process_one(self, entry: FrontierEntry) -> None:
        try:
            self._crawl(entry)
        except Exception as exc:  # one bad page must never kill the crawl
            logger.exception("Crawl failed for %s", entry.url)
            self.frontier.mark_error(entry.url, f"{type(exc).__name__}: {exc}")
            self._bump("errors")

    def _crawl(self, entry: FrontierEntry) -> None:
        domain = get_domain(entry.url)

        if not self.allow_private_hosts and not validate_url(entry.url):
            return self._skip(entry, "unsafe host")
        if not self._domain_allowed(domain):
            return self._skip(entry, "domain not allowed")

        self._ensure_robots(entry.url, domain)
        if not self.politeness.is_allowed(domain, entry.url):
            return self._skip(entry, "robots.txt")

        # Only pages we will actually fetch count toward the domain limit.
        if not self._reserve_domain_slot(domain):
            return self._skip(entry, "domain limit")

        # Atomic slot: concurrent workers on one domain get spaced-out slots.
        wait = self.politeness.reserve_slot(domain)
        if wait > 0:
            time.sleep(wait)

        result = self.fetcher.fetch(entry.url, etag=entry.etag, last_modified=entry.last_modified)

        if result.not_modified:
            self.frontier.mark_not_modified(entry.url)
            self._bump("not_modified")
            return

        if result.error or result.html is None:
            self.frontier.mark_error(entry.url, result.error or f"status {result.status_code}")
            self._bump("errors")
            return

        page = extract_page(result.html, entry.url)

        # Ingest FIRST; only mark visited if indexing succeeded, so a failed
        # ingest is retried on the next run instead of being lost.
        self.ingest_fn(
            entry.url,
            page.title,
            page.text,
            {
                "url": entry.url,
                "canonical_url": page.canonical_url or entry.url,
                "meta_description": page.meta_description,
                "language": page.language,
                "depth": entry.depth,
            },
        )
        self.frontier.mark_done(
            entry.url,
            content_hash=content_hash(page.text),
            etag=result.etag,
            last_modified=result.last_modified,
        )
        self._bump("crawled", len(result.html.encode("utf-8", errors="ignore")))

        if entry.depth < self.max_depth:
            for link in page.links:
                # Cheap filters only; full SSRF/DNS validation happens when the
                # URL is actually processed (one lookup per fetch, not per link).
                if urlsplit(link).scheme not in ("http", "https"):
                    continue
                if not self._domain_allowed(get_domain(link)):
                    continue
                self.frontier.add(link, depth=entry.depth + 1, base=entry.url)

    # ---------------------------------------------------------------- run

    def _queue_recrawls(self) -> None:
        if self.recrawl_interval <= 0:
            return
        for url in self.frontier.recrawl_due_urls(self.recrawl_interval):
            self.frontier.add(url, depth=0, priority=5, allow_visited=True)

    def run(self) -> dict:
        try:
            self._queue_recrawls()
            completed = 0
            with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
                while completed < self.max_pages:
                    batch = self.frontier.next_batch(min(self.concurrency, self.max_pages - completed))
                    if not batch:
                        break
                    futures = [pool.submit(self._process_one, e) for e in batch]
                    for future in as_completed(futures):
                        future.result()  # _process_one never raises
                        completed += 1
            return {**self.stats, "metrics": self.metrics.snapshot()}
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        for closer in (self.fetcher.close, self.frontier.close):
            try:
                closer()
            except Exception:
                pass
        self._closed = True
