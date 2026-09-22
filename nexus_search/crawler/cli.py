"""CLI entrypoint for the Nexus Search crawler.

One-off:   python -m nexus_search.crawler.cli --seeds seeds.txt
Sitemap:   python -m nexus_search.crawler.cli --sitemap https://site.com/sitemap.xml --domains site.com
Scheduled: python -m nexus_search.crawler.cli --seeds seeds.txt --every 3600
"""

import argparse
import json
import logging
import os
import time

import yaml

from ..core.embedding_sync import create_embedding_sync
from ..core.indexer import Indexer
from ..core.storage import Storage
from ..core.vector_store import VectorStoreManager
from ..ingestion.dedup import Deduplicator
from ..ingestion.pipeline import make_crawler_ingest_fn
from .pipeline import CrawlPipeline
from .scheduler import CrawlScheduler


def load_config(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as file:
            return yaml.safe_load(file) or {}
    except FileNotFoundError:
        return {}


def _pick(cli_value, config: dict, key: str, default):
    return cli_value if cli_value is not None else config.get(key, default)


def run_once(args, config: dict, seeds: list, domains: list) -> dict:
    """One full crawl. Builds fresh objects each time (a pipeline closes itself)."""
    storage = Storage(args.db)
    indexer = Indexer(storage)
    dedup = Deduplicator(args.db)
    vector_store = VectorStoreManager(args.db)
    sync = create_embedding_sync(vector_store, batch_size=32)
    sync.attach(indexer)
    recrawl = _pick(args.recrawl_interval, config, "recrawl_interval", 0)
    if args.every and not recrawl:
        recrawl = args.every  # scheduled runs re-check old pages automatically

    pipeline = CrawlPipeline(
        db_path=os.path.splitext(args.db)[0] + "_frontier.db",
        allowed_domains=domains,
        max_pages=_pick(args.max_pages, config, "max_pages", 1000),
        max_depth=_pick(args.max_depth, config, "max_depth", 3),
        concurrency=_pick(args.concurrency, config, "concurrency", 8),
        max_pages_per_domain=_pick(args.max_pages_per_domain, config, "max_pages_per_domain", 0),
        recrawl_interval=recrawl,
        default_crawl_delay=config.get("default_crawl_delay", 1.0),
        user_agent=config.get("user_agent", "NexusSearchBot/0.1 (+https://example.com/bot)"),
        ingest_fn=make_crawler_ingest_fn(
            indexer,
            dedup,
            min_quality=_pick(args.min_quality, config, "min_quality", None),
            chunk_size=_pick(args.chunk_size, config, "chunk_size", None),
        ),
    )
    try:
        pipeline.seed(seeds)
        for sitemap_url in args.sitemap or []:
            pipeline.seed_from_sitemap(sitemap_url)
        stats = pipeline.run()
        stats["documents_in_index"] = storage.document_count()
        return stats
    finally:
        sync.flush()
        sync.close()
        pipeline.close()
        dedup.close()
        storage.close()
        vector_store.close()


def report(stats: dict, metrics_file: str | None) -> None:
    print(f"Done: {stats}")
    if metrics_file:
        with open(metrics_file, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), **stats}) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Nexus Search — crawler")
    parser.add_argument("--seeds", help="Path to seed URL file")
    parser.add_argument("--sitemap", action="append", help="Sitemap URL (repeatable)")
    parser.add_argument("--config", default="crawler_config.yaml")
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--max-depth", type=int, default=None)
    parser.add_argument("--domains", help="Comma-separated allowed domains")
    parser.add_argument("--concurrency", type=int, default=None)
    parser.add_argument("--max-pages-per-domain", type=int, default=None)
    parser.add_argument("--recrawl-interval", type=float, default=None)
    parser.add_argument("--every", type=float, default=None, help="Re-run the crawl every N seconds")
    parser.add_argument("--metrics-file", help="Append one JSON line of stats per run")
    parser.add_argument("--min-quality", type=float, default=None, help="Skip pages scoring below this (0-1)")
    parser.add_argument("--chunk-size", type=int, default=None, help="Split long pages into chunks")
    parser.add_argument("--db", default="nexus_search.db")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config(args.config)

    seeds = config.get("seeds", [])
    if args.seeds:
        with open(args.seeds, encoding="utf-8") as file:
            seeds = [l.strip() for l in file if l.strip() and not l.startswith("#")]

    domains = config.get("allowed_domains", [])
    if args.domains:
        domains = [d.strip() for d in args.domains.split(",") if d.strip()]

    if not seeds and not args.sitemap:
        raise SystemExit("No seed URLs or sitemaps given.")

    if not args.every:
        report(run_once(args, config, seeds, domains), args.metrics_file)
        return

    scheduler = CrawlScheduler(args.every)
    try:
        while True:
            if scheduler.due():
                report(run_once(args, config, seeds, domains), args.metrics_file)
                scheduler.schedule_next()
            time.sleep(min(max(scheduler.seconds_until_next(), 0.5), 30))
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()