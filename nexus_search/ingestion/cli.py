"""Ingest CLI: batch-load files, code, or product data into the index.

Usage:
    python -m nexus_search.ingestion.cli --source files --path ./docs
    python -m nexus_search.ingestion.cli --source code --path ./src
    python -m nexus_search.ingestion.cli --source product --path ./catalog.csv
"""
import argparse
import logging

from ..core.embedding_sync import create_embedding_sync
from ..core.indexer import Indexer
from ..core.storage import Storage
from ..core.vector_store import VectorStoreManager
from .connectors.code import iter_code
from .connectors.files import iter_files
from .connectors.product import iter_products
from .dedup import Deduplicator
from .failures import FailureQueue
from .history import ContentHistory
from .pipeline import ingest_documents, ingest_one


def main():
    parser = argparse.ArgumentParser(description="Nexus Search — Phase 2 ingest CLI")
    parser.add_argument("--source", required=True, choices=["files", "code", "product"])
    parser.add_argument("--path", required=True, help="Directory (files/code) or file (product)")
    parser.add_argument("--db", default="nexus_search.db")
    parser.add_argument("--min-quality", type=float, default=None, help="Skip docs scoring below this (0-1)")
    parser.add_argument("--chunk-size", type=int, default=None, help="Split long docs into ~N-char chunks")
    parser.add_argument("--list-failures", action="store_true",
                        help="List dead-lettered ingestions and exit")
    parser.add_argument("--replay-failures", action="store_true",
                        help="Re-run due dead-lettered items and exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    storage = Storage(args.db)
    indexer = Indexer(storage)
    dedup = Deduplicator(args.db)
    failures = FailureQueue(args.db)

    if args.list_failures:
        rows = failures.list_all()
        for r in rows:
            print(f"#{r.id} {r.doc_id} attempts={r.attempts} error={r.error}")
        print(f"{len(rows)} dead-lettered")
        failures.close(); storage.close(); dedup.close()
        return
    if args.replay_failures:
        def _retry(doc):
            ingest_one(doc, indexer, dedup, min_quality=args.min_quality,
                       chunk_size=args.chunk_size)
        stats = failures.replay(_retry)
        print(f"Replay: {stats}")
        failures.close(); storage.close(); dedup.close()
        return
    vector_store = VectorStoreManager(args.db)
    sync = create_embedding_sync(vector_store, batch_size=32)
    sync.attach(indexer)

    if args.source == "files":
        docs = iter_files(args.path)
    elif args.source == "code":
        docs = iter_code(args.path)
    else:
        docs = iter_products(args.path)

    history = ContentHistory(args.db)
    try:
        stats = ingest_documents(docs, indexer, dedup, min_quality=args.min_quality,
                                 chunk_size=args.chunk_size, failure_queue=failures,
                                 history=history)
        print(f"Done: {stats}")
    finally:
        # a connector raising mid-iteration must not strand queued embeddings
        # or leak the sqlite connections
        history.close()
        failures.close()
        sync.flush()
        sync.close()
        storage.close()
        dedup.close()
        vector_store.close()


if __name__ == "__main__":
    main()