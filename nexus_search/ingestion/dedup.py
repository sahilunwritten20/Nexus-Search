"""Content-hash deduplication. Two documents with identical content —
even from different sources or doc_ids — should only be indexed once.
"""
import hashlib
import sqlite3


def content_hash(text: str) -> str:
    """Stable hash: same content hashes identically regardless of case/whitespace."""
    normalized = " ".join(text.split()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class Deduplicator:
    def __init__(self, db_path: str = "nexus_search.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS content_hashes (hash TEXT PRIMARY KEY, doc_id TEXT NOT NULL)"
        )
        self.conn.commit()

    def is_duplicate(self, text: str) -> bool:
        h = content_hash(text)
        return self.conn.execute(
            "SELECT 1 FROM content_hashes WHERE hash = ?", (h,)
        ).fetchone() is not None

    def register(self, text: str, doc_id: str):
        h = content_hash(text)
        self.conn.execute(
            "INSERT OR REPLACE INTO content_hashes (hash, doc_id) VALUES (?, ?)", (h, doc_id)
        )
        self.conn.commit()

    def close(self):
        self.conn.close()
