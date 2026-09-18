"""Ingest CLI: batch-load files, code, or product data into the index.

Usage:
    python -m nexus_search.ingestion.cli --source files --path ./docs
    python -m nexus_search.ingestion.cli --source code --path ./src
    python -m nexus_search.ingestion.cli --source product --path ./catalog.csv
"""
import argparse
import logging

from ..core.indexer import Indexer
from ..core.storage import Storage
from .connectors.code import iter_code
from .connectors.files import iter_files
from .connectors.product import iter_products
from .dedup import Deduplicator
from .pipeline import ingest_documents


def main():
    parser = argparse.ArgumentParser(description="Nexus Search — Phase 2 ingest CLI")
    parser.add_argument("--source", required=True, choices=["files", "code", "product"])
    parser.add_argument("--path", required=True, help="Directory (files/code) or file (product)")
    parser.add_argument("--db", default="nexus_search.db")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    storage = Storage(args.db)
    indexer = Indexer(storage)
    dedup = Deduplicator(args.db)

    if args.source == "files":
        docs = iter_files(args.path)
    elif args.source == "code":
        docs = iter_code(args.path)
    else:
        docs = iter_products(args.path)

    stats = ingest_documents(docs, indexer, dedup)
    print(f"Done: {stats}")


if __name__ == "__main__":
    main()
