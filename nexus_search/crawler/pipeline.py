"""Crawler pipeline for Nexus Search."""

import logging
import threading
import time
from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed,
)
from typing import Callable, Optional
from urllib.parse import urlsplit

from ..ingestion.connectors.web import extract_page
from ..ingestion.dedup import content_hash

from .fetcher import Fetcher
from .frontier import Frontier, FrontierEntry
from .metrics import CrawlerMetrics
from .politeness import PolitenessManager
from .security import validate_url
from .url_utils import get_domain


logger = logging.getLogger(
    "nexus_search.crawler.pipeline"
)


IngestFn = Callable[
    [str, str, str, dict],
    None,
]


def default_ingest(
    url: str,
    title: str,
    text: str,
    metadata: dict,
) -> None:

    logger.info(
        "INGEST %s (%d chars)",
        url,
        len(text),
    )


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
    ):

        self.frontier = Frontier(
            db_path
        )

        self.politeness = (
            PolitenessManager()
        )

        self.fetcher = Fetcher()

        self.allowed_domains = set(
            allowed_domains or []
        )

        self.max_pages = max_pages

        self.max_depth = max_depth

        self.concurrency = max(
            1,
            concurrency,
        )

        self.ingest_fn = ingest_fn

        self.max_pages_per_domain = (
            max_pages_per_domain
        )

        self.recrawl_interval = (
            recrawl_interval
        )

        self.allow_private_hosts = (
            allow_private_hosts
        )

        self.metrics = (
            CrawlerMetrics()
        )

        self.domain_counts = {}

        # Reservation lock.
        self.domain_lock = (
            threading.Lock()
        )

        self.stats = {
            "crawled": 0,
            "skipped": 0,
            "errors": 0,
            "not_modified": 0,
        }

        self._closed = False

    # ======================================================
    # DOMAIN CHECK
    # ======================================================

    def _domain_allowed(
        self,
        domain: str,
    ) -> bool:

        if not self.allowed_domains:
            return True

        return any(
            domain == allowed
            or domain.endswith(
                "." + allowed
            )
            for allowed
            in self.allowed_domains
        )

    # ======================================================
    # RESERVE DOMAIN SLOT
    # ======================================================

    def _reserve_domain_slot(
        self,
        domain: str,
    ) -> bool:

        if (
            self.max_pages_per_domain
            <= 0
        ):
            return True

        with self.domain_lock:

            current = (
                self.domain_counts.get(
                    domain,
                    0,
                )
            )

            if (
                current
                >= self.max_pages_per_domain
            ):
                return False

            # Reserve BEFORE crawling.
            self.domain_counts[
                domain
            ] = current + 1

            return True

    # ======================================================
    # SEED
    # ======================================================

    def seed(
        self,
        urls: list[str],
    ) -> None:

        for url in urls:

            if (
                not self.allow_private_hosts
                and not validate_url(url)
            ):
                logger.warning(
                    "Ignoring unsafe seed: %s",
                    url,
                )
                continue

            self.frontier.add(
                url,
                depth=0,
                priority=10,
            )

    # ======================================================
    # ROBOTS
    # ======================================================

    def _ensure_robots(
        self,
        url: str,
        domain: str,
    ) -> None:

        if self.politeness.has_robots(
            domain
        ):
            return

        parts = urlsplit(url)

        robots_url = (
            f"{parts.scheme}://"
            f"{parts.netloc}/robots.txt"
        )

        content = (
            self.fetcher.fetch_text(
                robots_url
            )
        )

        self.politeness.register_robots_txt(
            domain,
            content,
        )

    # ======================================================
    # PROCESS URL
    # ======================================================

    def _process_one(
        self,
        entry: FrontierEntry,
    ) -> None:

        domain = get_domain(
            entry.url
        )

        # SSRF protection
        if (
            not self.allow_private_hosts
            and not validate_url(entry.url)
        ):

            self.frontier.mark_done(
                entry.url
            )

            self.stats["skipped"] += 1
            self.metrics.record_skipped()

            return

        # Domain filter
        if not self._domain_allowed(
            domain
        ):

            self.frontier.mark_done(
                entry.url
            )

            self.stats["skipped"] += 1
            self.metrics.record_skipped()

            return

        # Domain limit
        if not self._reserve_domain_slot(
            domain
        ):

            self.frontier.mark_done(
                entry.url
            )

            self.stats["skipped"] += 1
            self.metrics.record_skipped()

            return

        # Robots
        self._ensure_robots(
            entry.url,
            domain,
        )

        if not self.politeness.is_allowed(
            domain,
            entry.url,
        ):

            self.frontier.mark_done(
                entry.url
            )

            self.stats["skipped"] += 1
            self.metrics.record_skipped()

            return

        # Crawl delay
        wait = (
            self.politeness
            .time_until_allowed(domain)
        )

        if wait > 0:
            time.sleep(wait)

        self.politeness.record_request(
            domain
        )

        # Fetch with validators
        result = self.fetcher.fetch(
            entry.url,
            etag=entry.etag,
            last_modified=entry.last_modified,
        )

        # 304
        if result.not_modified:

            self.frontier.mark_not_modified(
                entry.url
            )

            self.stats[
                "not_modified"
            ] += 1

            self.metrics.record_not_modified()

            return

        # Error
        if (
            result.error
            or result.html is None
        ):

            self.frontier.mark_error(
                entry.url,
                result.error
                or (
                    f"status "
                    f"{result.status_code}"
                ),
            )

            self.stats["errors"] += 1

            self.metrics.record_error()

            return

        # Extract
        page = extract_page(
            result.html,
            entry.url,
        )

        # Hash
        chash = content_hash(
            page.text
        )

        # Save validators
        self.frontier.mark_done(
            entry.url,
            content_hash=chash,
            etag=result.etag,
            last_modified=result.last_modified,
        )

        self.stats["crawled"] += 1

        self.metrics.record_crawled(
            len(
                result.html.encode(
                    "utf-8",
                    errors="ignore",
                )
            )
        )

        # Ingest
        self.ingest_fn(
            entry.url,
            page.title,
            page.text,
            {
                "url": entry.url,
                "canonical_url": (
                    page.canonical_url
                    or entry.url
                ),
                "meta_description": (
                    page.meta_description
                ),
                "language": page.language,
                "depth": entry.depth,
            },
        )

        # Discover links
        if entry.depth < self.max_depth:

            for link in page.links:

                if (
                    not self.allow_private_hosts
                    and not validate_url(link)
                ):
                    continue

                link_domain = get_domain(
                    link
                )

                if not self._domain_allowed(
                    link_domain
                ):
                    continue

                self.frontier.add(
                    link,
                    depth=entry.depth + 1,
                    base=entry.url,
                )

    # ======================================================
    # RECRAWL
    # ======================================================

    def _queue_recrawls(self) -> None:

        if self.recrawl_interval <= 0:
            return

        urls = (
            self.frontier.recrawl_due_urls(
                self.recrawl_interval
            )
        )

        for url in urls:

            self.frontier.add(
                url,
                depth=0,
                priority=5,
                allow_visited=True,
            )

    # ======================================================
    # RUN
    # ======================================================

    def run(self) -> dict:

        try:

            self._queue_recrawls()

            completed = 0

            with ThreadPoolExecutor(
                max_workers=self.concurrency
            ) as pool:

                while (
                    completed
                    < self.max_pages
                ):

                    remaining = (
                        self.max_pages
                        - completed
                    )

                    batch_size = min(
                        self.concurrency,
                        remaining,
                    )

                    batch = (
                        self.frontier.next_batch(
                            batch_size
                        )
                    )

                    if not batch:
                        break

                    futures = [
                        pool.submit(
                            self._process_one,
                            entry,
                        )
                        for entry in batch
                    ]

                    for future in as_completed(
                        futures
                    ):

                        future.result()

                        completed += 1

            return {
                **self.stats,
                "metrics": (
                    self.metrics.snapshot()
                ),
            }

        finally:

            self.fetcher.close()

            self.frontier.close()

            self._closed = True

    # ======================================================
    # CLOSE
    # ======================================================

    def close(self) -> None:

        if self._closed:
            return

        try:
            self.fetcher.close()
        except Exception:
            pass

        try:
            self.frontier.close()
        except Exception:
            pass

        self._closed = True