"""Orchestrates frontier -> politeness -> fetch -> extract -> ingest.

This version is genuinely wired to Phase 2: `extract_page` is imported
from the ingestion package's web connector (one extraction implementation,
not two), and the CLI passes a real `ingest_fn` built from the actual
indexer + dedup rather than a logging placeholder.
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional
from urllib.parse import urlsplit

from ..ingestion.connectors.web import extract_page
from ..ingestion.dedup import content_hash
from .fetcher import Fetcher
from .frontier import Frontier, FrontierEntry
from .politeness import PolitenessManager
from .url_utils import get_domain

logger = logging.getLogger("nexus_search.crawler.pipeline")

IngestFn = Callable[[str, str, str, dict], None]


def default_ingest(url: str, title: str, text: str, metadata: dict) -> None:
    """Fallback for standalone crawler testing — logs instead of indexing.
    Real runs should pass an ingest_fn from
    `nexus_search.ingestion.pipeline.make_crawler_ingest_fn`, which is what
    `crawler.cli` does by default.
    """
    logger.info("INGEST %s (%d chars) — no ingest_fn was wired in", url, len(text))


class CrawlPipeline:
    def __init__(
        self,
        db_path: str = "crawler.db",
        allowed_domains: Optional[list[str]] = None,
        max_pages: int = 1000,
        max_depth: int = 3,
        concurrency: int = 8,
        ingest_fn: IngestFn = default_ingest,
    ):
        self.frontier = Frontier(db_path)
        self.politeness = PolitenessManager()
        self.fetcher = Fetcher()
        self.allowed_domains = set(allowed_domains or [])
        self.max_pages = max_pages
        self.max_depth = max_depth
        self.concurrency = concurrency
        self.ingest_fn = ingest_fn
        self.stats = {"crawled": 0, "skipped": 0, "errors": 0}

    def seed(self, urls: list[str]):
        for url in urls:
            self.frontier.add(url, depth=0, priority=10)

    def _domain_allowed(self, domain: str) -> bool:
        if not self.allowed_domains:
            return True
        return any(domain == d or domain.endswith("." + d) for d in self.allowed_domains)

    def _ensure_robots(self, url: str, domain: str):
        if self.politeness.has_robots(domain):
            return
        # Build the robots.txt URL from the actual netloc (host:port) being
        # crawled, not from `domain` (the port-stripped rate-limit key) —
        # reusing that would silently hit the default port instead.
        parts = urlsplit(url)
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        content = self.fetcher.fetch_text(robots_url)
        self.politeness.register_robots_txt(domain, content)  # "" fails open

    def _process_one(self, entry: FrontierEntry):
        domain = get_domain(entry.url)

        if not self._domain_allowed(domain):
            self.frontier.mark_done(entry.url)
            self.stats["skipped"] += 1
            return

        self._ensure_robots(entry.url, domain)
        if not self.politeness.is_allowed(domain, entry.url):
            self.frontier.mark_done(entry.url)
            self.stats["skipped"] += 1
            logger.info("SKIP (robots.txt) %s", entry.url)
            return

        wait = self.politeness.time_until_allowed(domain)
        if wait > 0:
            time.sleep(wait)
        self.politeness.record_request(domain)

        result = self.fetcher.fetch(entry.url)
        if result.error or result.html is None:
            self.frontier.mark_error(entry.url, result.error or f"status {result.status_code}")
            self.stats["errors"] += 1
            logger.warning("ERROR %s: %s", entry.url, result.error)
            return

        page = extract_page(result.html, entry.url)
        chash = content_hash(page.text)
        self.frontier.mark_done(entry.url, content_hash=chash)
        self.stats["crawled"] += 1

        self.ingest_fn(
            entry.url,
            page.title,
            page.text,
            {"url": entry.url, "canonical_url": page.canonical_url or entry.url, "meta_description": page.meta_description, "language": page.language, "depth": entry.depth},
        )

        if entry.depth < self.max_depth:
            for link in page.links:
                if self._domain_allowed(get_domain(link)):
                    self.frontier.add(link, depth=entry.depth + 1, base=entry.url)

        logger.info("OK depth=%d %s (%d chars, %.2fs)", entry.depth, entry.url, len(page.text), result.elapsed)

    def run(self) -> dict:
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            while self.stats["crawled"] < self.max_pages:
                batch = self.frontier.next_batch(self.concurrency)
                if not batch:
                    break
                futures = [pool.submit(self._process_one, entry) for entry in batch]
                for future in as_completed(futures):
                    future.result()

        self.fetcher.close()
        logger.info(
            "Crawl complete: %d crawled, %d skipped, %d errors",
            self.stats["crawled"], self.stats["skipped"], self.stats["errors"],
        )
        return self.stats
