"""CLI entrypoint for the Nexus Search crawler."""

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

        with open(
            path,
            encoding="utf-8",
        ) as file:

            return yaml.safe_load(
                file
            ) or {}

    except FileNotFoundError:

        return {}


def main():

    parser = argparse.ArgumentParser(
        description="Nexus Search — crawler"
    )

    parser.add_argument(
        "--seeds",
        help="Path to seed URL file",
    )

    parser.add_argument(
        "--config",
        default="crawler_config.yaml",
    )

    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
    )

    parser.add_argument(
        "--max-depth",
        type=int,
        default=None,
    )

    parser.add_argument(
        "--domains",
        help="Comma-separated allowed domains",
    )

    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
    )

    parser.add_argument(
        "--max-pages-per-domain",
        type=int,
        default=None,
    )

    parser.add_argument(
        "--recrawl-interval",
        type=float,
        default=None,
    )

    parser.add_argument(
        "--db",
        default="nexus_search.db",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s "
            "%(levelname)s "
            "%(message)s"
        ),
    )

    config = load_config(
        args.config
    )

    seeds = config.get(
        "seeds",
        [],
    )

    if args.seeds:

        with open(
            args.seeds,
            encoding="utf-8",
        ) as file:

            seeds = [
                line.strip()
                for line in file
                if line.strip()
                and not line.startswith("#")
            ]

    allowed_domains = config.get(
        "allowed_domains",
        [],
    )

    if args.domains:

        allowed_domains = [
            d.strip()
            for d in args.domains.split(",")
            if d.strip()
        ]

    if not seeds:

        raise SystemExit(
            "No seed URLs given."
        )

    storage = Storage(
        args.db
    )

    indexer = Indexer(
        storage
    )

    dedup = Deduplicator(
        args.db
    )

    ingest_fn = (
        make_crawler_ingest_fn(
            indexer,
            dedup,
        )
    )

    frontier_db = (
        args.db.replace(
            ".db",
            "_frontier.db",
        )
    )

    pipeline = CrawlPipeline(

        db_path=frontier_db,

        allowed_domains=allowed_domains,

        max_pages=(
            args.max_pages
            if args.max_pages is not None
            else config.get(
                "max_pages",
                1000,
            )
        ),

        max_depth=(
            args.max_depth
            if args.max_depth is not None
            else config.get(
                "max_depth",
                3,
            )
        ),

        concurrency=(
            args.concurrency
            if args.concurrency is not None
            else config.get(
                "concurrency",
                8,
            )
        ),

        max_pages_per_domain=(
            args.max_pages_per_domain
            if args.max_pages_per_domain is not None
            else config.get(
                "max_pages_per_domain",
                0,
            )
        ),

        recrawl_interval=(
            args.recrawl_interval
            if args.recrawl_interval is not None
            else config.get(
                "recrawl_interval",
                0,
            )
        ),

        ingest_fn=ingest_fn,
    )

    try:

        pipeline.seed(seeds)

        stats = pipeline.run()

        print(
            f"Done: {stats}"
        )

        print(
            "Index now has "
            f"{storage.document_count()} "
            "documents."
        )

    finally:

        pipeline.close()
        storage.close()


if __name__ == "__main__":
    main()