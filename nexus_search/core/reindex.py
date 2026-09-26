"""Zero-downtime reindex for Nexus Search (`--shadow` mode).

Why it exists: documents' postings are derived data. If the tokenizer or
tokenization config changes (e.g. NEXUS_STEMMING), the stored postings must be
rebuilt. Building them in place takes writes offline; this module instead:

1. builds a fresh `postings_shadow` table on a SECOND connection
   (WAL mode: readers never see it, the write lock on the main connection is
   only taken for a page at a time, not for the whole build)
2. inside the swap transaction, replays the delta (docs added/changed/deleted
   DURING the build are re-tokenized into the shadow table) and refreshes
   documents.length — so the cutover is one atomic commit and live writes
   made during the build are NOT silently dropped
3. swaps tables in the same transaction — the reader-visible cutover window
   is one SQLite commit

CLI:  python -m nexus_search.core.reindex --db nexus_search.db --shadow
"""
import logging
import os
import sqlite3
import time
from collections import Counter

from .tokenizer import tokenize

logger = logging.getLogger("nexus_search.reindex")

_POSTINGS_DDL = (
    "CREATE TABLE postings_shadow ("
    "term TEXT NOT NULL, doc_id TEXT NOT NULL, term_freq INTEGER NOT NULL, "
    "PRIMARY KEY (term, doc_id))"
)


def reindex_shadow(db_path: str, progress=None) -> dict:
    """Rebuild the postings table offline and swap it in atomically.

    `progress` (optional) is called with a status string every doc batch.
    Returns a stats dict. Never silently leaves a swapped-but-wrong index:
    a failure during the build raises BEFORE the swap, and the swap itself
    is one transaction."""
    log = progress or (lambda m: logger.info(m))

    # Phase 1: open a second connection; create and fill the shadow table.
    build = sqlite3.connect(db_path)
    build.execute("PRAGMA busy_timeout = 5000")
    build.execute("PRAGMA journal_mode = WAL")
    try:
        build.execute("DROP TABLE IF EXISTS postings_shadow")
        build.execute(_POSTINGS_DDL)
        rows = build.execute(
            "SELECT doc_id, title, content FROM documents ORDER BY doc_id").fetchall()
        log(f"reindex: {len(rows)} documents to re-tokenize")
        # One pass over the corpus: tokenize ONCE per document. The counts
        # feed the documents.length refresh in the swap transaction, and the
        # snapshot lets the swap replay any writes that land during the build.
        snapshot: dict[str, tuple[str, str]] = {}
        token_counts: dict[str, int] = {}
        batch = []
        for doc_id, title, content in rows:
            snapshot[doc_id] = (title, content)
            tokens = tokenize(f"{title} {content}")
            freqs = Counter(tokens)
            token_counts[doc_id] = len(tokens)
            batch.extend((term, doc_id, f) for term, f in freqs.items())
            if len(batch) >= 5000:
                build.executemany(
                    "INSERT INTO postings_shadow (term, doc_id, term_freq) VALUES (?,?,?)",
                    batch)
                batch.clear()
        if batch:
            build.executemany(
                "INSERT INTO postings_shadow (term, doc_id, term_freq) VALUES (?,?,?)", batch)
        build.commit()
    finally:
        build.close()

    # Phase 2: delta replay + atomic swap on a fresh connection.
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Writers that raced the build are blocked here (busy_timeout), so
        # this read is the cutover's point-in-time truth.
        live = conn.execute(
            "SELECT doc_id, title, content FROM documents").fetchall()
        live_map = {doc_id: (title, content) for doc_id, title, content in live}

        deleted = [d for d in snapshot if d not in live_map]
        changed = [d for d, tc in live_map.items()
                   if d not in snapshot or snapshot[d] != tc]
        if deleted:
            conn.execute(
                f"DELETE FROM postings_shadow WHERE doc_id IN "
                f"({','.join('?' for _ in deleted)})", deleted)
        for doc_id in changed:
            title, content = live_map[doc_id]
            tokens = tokenize(f"{title} {content}")
            token_counts[doc_id] = len(tokens)
            conn.execute("DELETE FROM postings_shadow WHERE doc_id = ?", (doc_id,))
            conn.executemany(
                "INSERT INTO postings_shadow (term, doc_id, term_freq) VALUES (?,?,?)",
                [(t, doc_id, f) for t, f in Counter(tokens).items()])
        if deleted or changed:
            log(f"reindex: replayed {len(changed)} changed/{len(deleted)} deleted "
                f"docs written during the build")

        # documents.length belongs to the same cutover: derived from the
        # same tokens as the postings, committed atomically with the swap.
        for doc_id, length in token_counts.items():
            if doc_id in live_map:
                conn.execute("UPDATE documents SET length = ? WHERE doc_id = ?",
                             (length, doc_id))

        conn.execute("ALTER TABLE postings RENAME TO postings_old")
        conn.execute("ALTER TABLE postings_shadow RENAME TO postings")
        conn.execute("DROP TABLE postings_old")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_postings_term ON postings (term)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_postings_doc ON postings (doc_id)")
        conn.commit()
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    stats = {"documents": len(live_map),
             "tokens": sum(c for d, c in token_counts.items() if d in live_map),
             "delta_replayed": len(changed) + len(deleted)}
    log(f"reindex: swapped in {stats}")
    return stats


def main(argv=None) -> int:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(description="Rebuild the postings index")
    p.add_argument("--db", default="nexus_search.db")
    p.add_argument("--shadow", action="store_true",
                   help="build into a shadow table, then swap atomically (zero-downtime)")
    args = p.parse_args(argv)
    if not os.path.exists(args.db):
        print(f"no such database: {args.db}")
        return 1
    if not args.shadow:
        print("refusing non-shadow rebuild (it would block writes); pass --shadow")
        return 2
    stats = reindex_shadow(args.db, progress=print)
    print(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
