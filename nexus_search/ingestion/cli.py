"""Ingest CLI: batch-load files, code, or product data into the index.

Usage:
    python -m nexus_search.ingestion.cli --source files --path ./docs
    python -m nexus_search.ingestion.cli --source code --path ./src
    python -m nexus_search.ingestion.cli --source product --path ./catalog.csv
"""
import argparse
import logging

from ..core.hybrid_search import create_hybrid_search
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
    parser.add_argument("--min-quality", type=float, default=None, help="Skip docs scoring below this (0-1)")
    parser.add_argument("--chunk-size", type=int, default=None, help="Split long docs into ~N-char chunks")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    storage = Storage(args.db)
    indexer = Indexer(storage)
    dedup = Deduplicator(args.db)
    hybrid = create_hybrid_search(storage, db_path=args.db)

    if args.source == "files":
        docs = iter_files(args.path)
    elif args.source == "code":
        docs = iter_code(args.path)
    else:
        docs = iter_products(args.path)

    stats = ingest_documents(docs, indexer, dedup, min_quality=args.min_quality, chunk_size=args.chunk_size, hybrid=hybrid)
    print(f"Done: {stats}")

    hybrid.close()
    storage.close()
    dedup.close()


if __name__ == "__main__":
    main()