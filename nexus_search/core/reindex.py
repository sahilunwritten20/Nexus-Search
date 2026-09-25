"""Zero-downtime reindex for Nexus Search (`--shadow` mode).

Why it exists: documents' postings are derived data. If the tokenizer or
tokenization config changes (e.g. NEXUS_STEMMING), the stored postings must be
rebuilt. Building them in place takes writes offline; this module instead:

1. builds a fresh `postings_shadow` table on a SECOND connection
   (WAL mode: readers never see it, the write lock on the main connection is
   only taken for a page at a time, not for the whole build)
2. swaps tables in a single transaction — the reader-visible cutover window
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
    "CREATE TABLE IF NOT EXISTS postings_shadow ("
    "term TEXT NOT NULL, doc_id TEXT NOT NULL, term_freq INTEGER NOT NULL, "
    "PRIMARY KEY (term, doc_id))"
)
_POSTINGS_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_shadow_term ON postings_shadow (term);"
    "CREATE INDEX IF NOT EXISTS idx_shadow_doc ON postings_shadow (doc_id)"
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
        build.execute(_POSTINGS_DDL.replace("postings_shadow", "postings_shadow"))
        build.execute("DROP TABLE IF EXISTS postings_shadow")
        build.execute("CREATE TABLE postings_shadow (term TEXT NOT NULL, "
                      "doc_id TEXT NOT NULL, term_freq INTEGER NOT NULL, "
                      "PRIMARY KEY (term, doc_id))")
        rows = build.execute(
            "SELECT doc_id, title, content FROM documents ORDER BY doc_id").fetchall()
        log(f"reindex: {len(rows)} documents to re-tokenize")
        total_terms = 0
        batch = []
        for doc_id, title, content in rows:
            tokens = tokenize(f"{title} {content}")
            freqs = Counter(tokens)
            total_terms += len(tokens)
            batch.extend((term, doc_id, f) for term, f in freqs.items())
            if len(batch) >= 5000:
                build.executemany(
                    "INSERT INTO postings_shadow (term, doc_id, term_freq) VALUES (?,?,?)",
                    batch)
                batch.clear()
        if batch:
            build.executemany(
                "INSERT INTO postings_shadow (term, doc_id, term_freq) VALUES (?,?,?)", batch)
        # Also refresh documents.length — it's derived from tokens
        for doc_id, title, content in rows:
            tokens = tokenize(f"{title} {content}")
            build.execute("UPDATE documents SET length = ? WHERE doc_id = ?",
                          (len(tokens), doc_id))
        build.commit()
    finally:
        build.close()

    # Phase 2: atomic swap on the primary connection.
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        conn.execute("BEGIN IMMEDIATE")
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

    stats = {"documents": len(rows), "tokens": total_terms}
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
