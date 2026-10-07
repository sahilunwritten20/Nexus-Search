"""SQLite-backed storage for documents and the inverted-index postings."""
import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    doc_type TEXT NOT NULL DEFAULT 'text',
    length INTEGER NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    added_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS postings (
    term TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    term_freq INTEGER NOT NULL,
    PRIMARY KEY (term, doc_id)
);

CREATE INDEX IF NOT EXISTS idx_postings_term ON postings (term);
CREATE INDEX IF NOT EXISTS idx_postings_doc ON postings (doc_id);
"""


@dataclass
class Document:
    doc_id: str
    title: str
    content: str
    doc_type: str
    length: int
    metadata: dict
    added_at: float


class Storage:
    """One SQLite file holds both the document store and the postings list.
    Fine at prototype scale (thousands of items); Phase 8-10 replaces this
    with a sharded/distributed store without changing this class's interface.
    """

    def __init__(self, db_path: str = "nexus_search.db"):
        from .migrations import apply_migrations

        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.lock = threading.RLock()
        # Schema comes up through the migration runner: v1 is the baseline
        # schema; future column changes become v2, v3, ... (see migrations.py)
        self.schema_version = apply_migrations(
            self.conn, "storage",
            [(1, SCHEMA),
             # v2 (WP6): freshness sorting reads added_at for the whole
             # candidate pool; the index makes the batch read cheap.
             (2, "CREATE INDEX IF NOT EXISTS idx_documents_added_at "
                 "ON documents (added_at);")],
        )

    def upsert_document(
        self, doc_id: str, title: str, content: str, doc_type: str, length: int, metadata: dict
    ):
        """Legacy split write — kept for tests that hand-roll postings. Now
        COMMITS before returning: this connection is shared across threads,
        so leaving a transaction open meant any LATER unrelated commit could
        flush a half-written doc row. Prefer upsert_document_with_postings
        (atomic) everywhere else."""
        with self.lock:
            self.conn.execute(
                "INSERT INTO documents (doc_id, title, content, doc_type, length, metadata, added_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(doc_id) DO UPDATE SET "
                "title=excluded.title, content=excluded.content, doc_type=excluded.doc_type, "
                "length=excluded.length, metadata=excluded.metadata, added_at=excluded.added_at",
                (doc_id, title, content, doc_type, length, json.dumps(metadata), time.time()),
            )
            # Clear old postings so re-indexing a doc_id doesn't leave stale terms behind.
            self.conn.execute("DELETE FROM postings WHERE doc_id = ?", (doc_id,))
            self.conn.commit()

    def add_postings(self, doc_id: str, term_freqs: dict[str, int]):
        if not term_freqs:
            return
        with self.lock:
            self.conn.executemany(
                "INSERT INTO postings (term, doc_id, term_freq) VALUES (?, ?, ?)",
                [(term, doc_id, freq) for term, freq in term_freqs.items()],
            )
            self.conn.commit()

    def delete_documents(self, doc_ids: list[str]) -> int:
        """One-transaction multi delete (cascade of parent + chunks): all
        rows+postings vanish together or not at all. Returns rows deleted."""
        if not doc_ids:
            return 0
        with self.lock:
            placeholders = ",".join("?" for _ in doc_ids)
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute(
                    f"DELETE FROM postings WHERE doc_id IN ({placeholders})", doc_ids)
                cur = self.conn.execute(
                    f"DELETE FROM documents WHERE doc_id IN ({placeholders})", doc_ids)
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")
            return cur.rowcount

    def upsert_document_with_postings(
        self, doc_id: str, title: str, content: str, doc_type: str, length: int,
        metadata: dict, term_freqs: dict[str, int],
    ):
        """Atomic document+postings write. An explicit BEGIN IMMEDIATE /
        COMMIT (ROLLBACK on any failure) guards against the real hazard of
        the split upsert+postings path: this connection is SHARED across
        threads, and any other operation's commit() would otherwise make a
        half-written doc (row present, postings missing) durable."""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute(
                    "INSERT INTO documents (doc_id, title, content, doc_type, length, metadata, added_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(doc_id) DO UPDATE SET "
                    "title=excluded.title, content=excluded.content, doc_type=excluded.doc_type, "
                    "length=excluded.length, metadata=excluded.metadata, added_at=excluded.added_at",
                    (doc_id, title, content, doc_type, length, json.dumps(metadata), time.time()),
                )
                self.conn.execute("DELETE FROM postings WHERE doc_id = ?", (doc_id,))
                if term_freqs:
                    self.conn.executemany(
                        "INSERT INTO postings (term, doc_id, term_freq) VALUES (?, ?, ?)",
                        [(term, doc_id, freq) for term, freq in term_freqs.items()],
                    )
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    def commit(self):
        with self.lock:
            self.conn.commit()

    def get_document(self, doc_id: str) -> Optional[Document]:
        with self.lock:
            row = self.conn.execute(
                "SELECT doc_id, title, content, doc_type, length, metadata, added_at "
                "FROM documents WHERE doc_id = ?",
                (doc_id,),
            ).fetchone()
        if row is None:
            return None
        return Document(row[0], row[1], row[2], row[3], row[4], json.loads(row[5]), row[6])

    def get_documents(self, doc_ids: list[str]) -> dict[str, Document]:
        """Batch fetch: one IN query instead of N SELECTs (reranker/facet
        counts fetch whole pages at once — the N+1 pattern was the cost)."""
        if not doc_ids:
            return {}
        with self.lock:
            placeholders = ",".join("?" for _ in doc_ids)
            rows = self.conn.execute(
                "SELECT doc_id, title, content, doc_type, length, metadata, added_at "
                f"FROM documents WHERE doc_id IN ({placeholders})",
                doc_ids,
            ).fetchall()
        return {r[0]: Document(r[0], r[1], r[2], r[3], r[4], json.loads(r[5]), r[6])
                for r in rows}

    def get_documents_meta(self, doc_ids: list[str] | None = None) -> dict[str, Document]:
        """Metadata-only fetch (WP12-B3): doc_id, doc_type, metadata in ONE
        query, WITHOUT the content column. Carries Document rows whose
        title/content/length/added_at are placeholders — the filter pushdown
        only reads .doc_type/.metadata, and reading full content per doc was
        O(corpus) per filtered vector search (audit R4)."""
        with self.lock:
            if doc_ids:
                placeholders = ",".join("?" for _ in doc_ids)
                rows = self.conn.execute(
                    "SELECT doc_id, doc_type, metadata FROM documents "
                    f"WHERE doc_id IN ({placeholders})", doc_ids).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT doc_id, doc_type, metadata FROM documents").fetchall()
        return {r[0]: Document(r[0], "", "", r[1], 0, json.loads(r[2]), 0.0)
                for r in rows}

    def delete_document(self, doc_id: str) -> bool:
        with self.lock:
            cur = self.conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
            self.conn.execute("DELETE FROM postings WHERE doc_id = ?", (doc_id,))
            self.conn.commit()
            return cur.rowcount > 0

    def document_count(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

    def average_length(self) -> float:
        with self.lock:
            row = self.conn.execute("SELECT AVG(length) FROM documents").fetchone()
            return row[0] or 0.0

    def postings_for_term(self, term: str) -> list[tuple[str, int]]:
        """[(doc_id, term_freq), ...] for every document containing this term."""
        with self.lock:
            return self.conn.execute(
                "SELECT doc_id, term_freq FROM postings WHERE term = ?", (term,)
            ).fetchall()

    def postings_for_terms(self, terms: list[str]) -> dict[str, list[tuple[str, int]]]:
        """postings_for_term for many terms in ONE query (BUG-01: one
        round trip per query term dominated adversarial-query cost; normal
        queries benefit too). Within-term row order matches the single-term
        call."""
        if not terms:
            return {}
        with self.lock:
            placeholders = ",".join("?" for _ in terms)
            rows = self.conn.execute(
                f"SELECT term, doc_id, term_freq FROM postings "
                f"WHERE term IN ({placeholders})",
                terms,
            ).fetchall()
        out: dict[str, list[tuple[str, int]]] = {}
        for term, doc_id, tf in rows:
            out.setdefault(term, []).append((doc_id, tf))
        return out

    def document_frequency(self, term: str) -> int:
        """Number of distinct documents containing this term — BM25's n(t)."""
        with self.lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM postings WHERE term = ?", (term,)
            ).fetchone()[0]

    def document_frequencies(self, terms: list[str]) -> dict[str, int]:
        """document_frequency for many terms in ONE query (spell-correction
        asks per OOV term of a query; N round trips was measurable on long
        queries — see BUG-01 remediation)."""
        if not terms:
            return {}
        with self.lock:
            placeholders = ",".join("?" for _ in terms)
            rows = self.conn.execute(
                f"SELECT term, COUNT(*) AS n FROM postings "
                f"WHERE term IN ({placeholders}) GROUP BY term",
                terms,
            ).fetchall()
        found = {term: count for term, count in rows}
        return {t: found.get(t, 0) for t in terms}

    def all_doc_ids(self) -> list[str]:
        with self.lock:
            return [r[0] for r in self.conn.execute("SELECT doc_id FROM documents ORDER BY doc_id")]

    def matching_doc_count(self, terms: list[str]) -> int:
        """COUNT(DISTINCT doc_id) over any term — the BM25 candidate-pool
        upper bound for a query, from ONE indexed IN query. Used to size
        the hybrid pool before the first retrieval pass (the scale
        benchmark showed the old fetch-then-refetch paid the whole
        candidate scan twice)."""
        if not terms:
            return 0
        with self.lock:
            placeholders = ",".join("?" for _ in terms)
            return self.conn.execute(
                f"SELECT COUNT(DISTINCT doc_id) FROM postings "
                f"WHERE term IN ({placeholders})", terms).fetchone()[0]

    def added_at_map(self, doc_ids: list[str]) -> dict[str, float]:
        """added_at for many docs in ONE query — the freshness sort fetches
        the whole candidate pool at once (per-doc get_document was the N+1).
        Missing ids are absent from the map (caller decides the default)."""
        if not doc_ids:
            return {}
        with self.lock:
            placeholders = ",".join("?" for _ in doc_ids)
            rows = self.conn.execute(
                f"SELECT doc_id, added_at FROM documents "
                f"WHERE doc_id IN ({placeholders})", doc_ids).fetchall()
        return {doc_id: ts for doc_id, ts in rows}

    def all_terms(self) -> list[str]:
        """Sorted index vocabulary (distinct postings terms). Phase 5 query
        understanding builds spelling/suggestion candidates from this."""
        with self.lock:
            return [r[0] for r in self.conn.execute("SELECT DISTINCT term FROM postings ORDER BY term")]

    def doc_ids_with_prefix(self, prefix: str) -> list[str]:
        with self.lock:
            return [
                r[0]
                for r in self.conn.execute(
                    "SELECT doc_id FROM documents WHERE substr(doc_id, 1, ?) = ?",
                    (len(prefix), prefix),
                )
            ]

    def chunk_ids(self, parent_id: str) -> list[str]:
        """Ids of the chunk documents (parent_id#chunk0, #chunk1, ...) of a parent."""
        pattern = re.compile(re.escape(parent_id) + r"#chunk\d+")
        return [
            d for d in self.doc_ids_with_prefix(parent_id + "#chunk") if pattern.fullmatch(d)
        ]

    def close(self):
        with self.lock:
            self.conn.close()