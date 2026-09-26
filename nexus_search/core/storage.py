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
            self.conn, "storage", [(1, SCHEMA)]
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

    def document_frequency(self, term: str) -> int:
        """Number of distinct documents containing this term — BM25's n(t)."""
        with self.lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM postings WHERE term = ?", (term,)
            ).fetchone()[0]

    def all_doc_ids(self) -> list[str]:
        with self.lock:
            return [r[0] for r in self.conn.execute("SELECT doc_id FROM documents ORDER BY doc_id")]

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