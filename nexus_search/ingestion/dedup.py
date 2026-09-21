"""Content-hash deduplication that survives updates and deletes."""
import hashlib
import sqlite3


def content_hash(text: str) -> str:
    """Stable hash: same content hashes identically regardless of case/whitespace."""
    normalized = " ".join(text.split()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class Deduplicator:
    def __init__(self, db_path: str = "nexus_search.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS content_hashes (hash TEXT PRIMARY KEY, doc_id TEXT NOT NULL)"
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_hashes_doc ON content_hashes (doc_id)")
        self.conn.commit()

    def is_duplicate(self, text: str) -> bool:
        h = content_hash(text)
        row = self.conn.execute("SELECT doc_id FROM content_hashes WHERE hash = ?", (h,)).fetchone()
        if row is None:
            return False
        owner = row[0]

        # Check if documents table exists in this DB
        tables = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='documents'"
        ).fetchone()
        if tables is None:
            return True  # no documents table (standalone use): trust the hash

        chunk_prefix = owner + "#chunk"
        prefix_len = len(chunk_prefix)
        alive = self.conn.execute(
            "SELECT 1 FROM documents WHERE doc_id = :owner "
            "OR substr(doc_id, 1, :plen) = :prefix LIMIT 1",
            {"owner": owner, "plen": prefix_len, "prefix": chunk_prefix},
        ).fetchone()
        if alive is None:  # owner was deleted -> stale hash, allow re-ingest
            self.forget_hash(h)
            # Also clean up any chunk hashes for this owner
            self.conn.execute(
                "DELETE FROM content_hashes WHERE substr(doc_id, 1, ?) = ?",
                (prefix_len, chunk_prefix),
            )
            self.conn.commit()
            return False
        return True

    def register(self, text: str, doc_id: str):
        """Record this doc's current content; drops the doc's previous hash."""
        self.conn.execute("DELETE FROM content_hashes WHERE doc_id = ?", (doc_id,))
        self.conn.execute(
            "INSERT OR REPLACE INTO content_hashes (hash, doc_id) VALUES (?, ?)",
            (content_hash(text), doc_id),
        )
        self.conn.commit()

    def forget(self, doc_id: str):
        self.conn.execute("DELETE FROM content_hashes WHERE doc_id = ?", (doc_id,))
        self.conn.commit()

    def forget_hash(self, h: str):
        self.conn.execute("DELETE FROM content_hashes WHERE hash = ?", (h,))
        self.conn.commit()

    def close(self):
        self.conn.close()