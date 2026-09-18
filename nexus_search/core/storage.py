"""SQLite-backed storage for documents and the inverted-index postings."""
import json
import sqlite3
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
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def upsert_document(
        self, doc_id: str, title: str, content: str, doc_type: str, length: int, metadata: dict
    ):
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

    def add_postings(self, doc_id: str, term_freqs: dict[str, int]):
        if not term_freqs:
            return
        self.conn.executemany(
            "INSERT INTO postings (term, doc_id, term_freq) VALUES (?, ?, ?)",
            [(term, doc_id, freq) for term, freq in term_freqs.items()],
        )

    def commit(self):
        self.conn.commit()

    def get_document(self, doc_id: str) -> Optional[Document]:
        row = self.conn.execute(
            "SELECT doc_id, title, content, doc_type, length, metadata, added_at "
            "FROM documents WHERE doc_id = ?",
            (doc_id,),
        ).fetchone()
        if row is None:
            return None
        return Document(row[0], row[1], row[2], row[3], row[4], json.loads(row[5]), row[6])

    def delete_document(self, doc_id: str) -> bool:
        cur = self.conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
        self.conn.execute("DELETE FROM postings WHERE doc_id = ?", (doc_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def document_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

    def average_length(self) -> float:
        row = self.conn.execute("SELECT AVG(length) FROM documents").fetchone()
        return row[0] or 0.0

    def postings_for_term(self, term: str) -> list[tuple[str, int]]:
        """[(doc_id, term_freq), ...] for every document containing this term."""
        return self.conn.execute(
            "SELECT doc_id, term_freq FROM postings WHERE term = ?", (term,)
        ).fetchall()

    def document_frequency(self, term: str) -> int:
        """Number of distinct documents containing this term — BM25's n(t)."""
        return self.conn.execute(
            "SELECT COUNT(*) FROM postings WHERE term = ?", (term,)
        ).fetchone()[0]

    def close(self):
        self.conn.close()
