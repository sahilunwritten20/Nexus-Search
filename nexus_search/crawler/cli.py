"""CLI entrypoint for the crawler — wired to real ingestion by default.

Usage:
    python -m nexus_search.crawler.cli --seeds seeds.txt --max-pages 1000 \\
        --max-depth 3 --domains example.com,docs.example.com
"""
import argparse
import logging

import yaml

from ..core.indexer import Indexer
from ..core.storage import Storage
from ..ingestion.dedup import Deduplicator
from ..ingestion.pipeline import make_crawler_ingest_fn
from .pipeline import CrawlPipeline


def load_config(path: str) -> dict:
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def main():
    parser = argparse.ArgumentParser(description="Nexus Search — crawler")
    parser.add_argument("--seeds", help="Path to a text file of seed URLs, one per line")
    parser.add_argument("--config", default="crawler_config.yaml")
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--max-depth", type=int, default=None)
    parser.add_argument("--domains", help="Comma-separated allowed domains")
    parser.add_argument("--concurrency", type=int, default=None)
    parser.add_argument("--db", default="nexus_search.db", help="Shared DB — same file the index and dedup use")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config = load_config(args.config)

    seeds = config.get("seeds", [])
    if args.seeds:
        with open(args.seeds) as f:
            seeds = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    allowed_domains = config.get("allowed_domains", [])
    if args.domains:
        allowed_domains = [d.strip() for d in args.domains.split(",")]

    if not seeds:
        raise SystemExit("No seed URLs given — pass --seeds seeds.txt or set `seeds:` in the config.")

    # Real wiring: crawled pages go straight into the actual index, through
    # the same dedup every other connector uses — not a logging stub.
    storage = Storage(args.db)
    indexer = Indexer(storage)
    dedup = Deduplicator(args.db)
    ingest_fn = make_crawler_ingest_fn(indexer, dedup)

    pipeline = CrawlPipeline(
        db_path=args.db.replace(".db", "_frontier.db"),  # frontier state is separate from the document index
        allowed_domains=allowed_domains,
        max_pages=args.max_pages or config.get("max_pages", 1000),
        max_depth=args.max_depth or config.get("max_depth", 3),
        concurrency=args.concurrency or config.get("concurrency", 8),
        ingest_fn=ingest_fn,
    )
    pipeline.seed(seeds)
    stats = pipeline.run()
    print(f"Done: {stats}")
    print(f"Index now has {storage.document_count()} documents — try: "
          f"python -m nexus_search.ingestion.cli --source files --path . (or query via the API)")


if __name__ == "__main__":
    main()
