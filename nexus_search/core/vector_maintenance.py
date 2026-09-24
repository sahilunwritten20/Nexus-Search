"""Vector-index maintenance and self-healing for Nexus Search.

Covers the operational gaps that are NOT part of the write path:
- backfill: embed documents that are missing/stale in the vector store
  (e.g. after an embedding failure, or after switching models)
- model change: rows stored under an old model name can be dropped or
  re-embedded under the current one
- garbage collection: vector rows whose document no longer exists

Run as a CLI:  python -m nexus_search.core.vector_maintenance <db_path>
"""
import logging
import sqlite3
from typing import Callable, Optional

from .storage import Storage
from .vector_store import VectorStoreManager

logger = logging.getLogger("nexus_search.vector_maintenance")


def reindex_embeddings(
    storage: Storage,
    vector_store: VectorStoreManager,
    batch_size: int = 32,
    drop_stale_models: bool = True,
    progress: Optional[Callable[[str], None]] = None,
) -> dict:
    """Make the vector store exactly match the document store.

    - Re-embeds every document whose content hash differs (or is missing);
      unchanged documents are skipped by the content-hash gate.
    - Removes vector rows whose doc_id no longer exists (orphans).
    - Optionally removes rows stored under a DIFFERENT model name, so a
      model switch doesn't leave dead weight behind.

    Returns a stats dict.
    """
    log = progress or (lambda msg: logger.info(msg))
    stats = {"documents": 0, "embedded": 0, "unchanged": 0,
             "orphans_removed": 0, "stale_models_removed": 0}
    store = vector_store.store

    # 1. re-embed/backfill current-model rows (hash-gated: no-op when fresh)
    doc_ids = storage.all_doc_ids()
    stats["documents"] = len(doc_ids)
    batch: list[tuple[str, str, str, str]] = []
    for doc_id in doc_ids:
        doc = storage.get_document(doc_id)
        if doc is None:
            continue
        text = f"{doc.title} {doc.content}"
        batch.append((doc_id, text, doc.doc_type or "",
                      (doc.metadata or {}).get("language", "") or ""))
        if len(batch) >= batch_size:
            e, u = _flush_batch(vector_store, batch)
            stats["embedded"] += e
            stats["unchanged"] += u
            batch.clear()
    if batch:
        e, u = _flush_batch(vector_store, batch)
        stats["embedded"] += e
        stats["unchanged"] += u
    log(f"reindex: {stats['embedded']} embedded, {stats['unchanged']} unchanged")

    # 2. GC orphan rows (vector exists, document gone)
    with store.lock:
        cur = store.conn.execute(
            "DELETE FROM doc_vectors WHERE model = ? AND NOT EXISTS "
            "(SELECT 1 FROM documents d WHERE d.doc_id = doc_vectors.doc_id)",
            (store.model,),
        )
        stats["orphans_removed"] = max(cur.rowcount, 0)

        # 3. drop rows of other models (dead weight after a model switch)
        if drop_stale_models:
            cur = store.conn.execute(
                "DELETE FROM doc_vectors WHERE model != ?", (store.model,)
            )
            stats["stale_models_removed"] = max(cur.rowcount, 0)
        store.conn.commit()

    if stats["orphans_removed"] or stats["stale_models_removed"]:
        log(f"gc: {stats['orphans_removed']} orphans, "
            f"{stats['stale_models_removed']} stale-model rows removed")
        store._load_matrix()

    return stats


def _flush_batch(vector_store: VectorStoreManager,
                 batch: list[tuple[str, str, str, str]]) -> tuple[int, int]:
    """Returns (embedded, unchanged) for one batch."""
    store = vector_store.store
    changed = [item for item in batch
               if store.get_content_hash(item[0]) != vector_store._content_hash(item[1])]
    if changed:
        vector_store.upsert_batch(batch)
        return len(changed), len(batch) - len(changed)
    return 0, len(batch)


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Reindex/backfill vector embeddings")
    parser.add_argument("db", nargs="?", default="nexus_search.db")
    parser.add_argument("--keep-old-models", action="store_true",
                        help="keep vectors stored under other model names")
    parser.add_argument("--batch-size", type=int, default=32)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    storage = Storage(args.db)
    vector_store = VectorStoreManager(args.db)
    try:
        stats = reindex_embeddings(
            storage, vector_store,
            batch_size=args.batch_size,
            drop_stale_models=not args.keep_old_models,
            progress=print,
        )
        print(stats)
        return 0
    except sqlite3.Error as exc:
        print(f"reindex failed: {exc}")
        return 1
    finally:
        vector_store.close()
        storage.close()


if __name__ == "__main__":
    raise SystemExit(main())
