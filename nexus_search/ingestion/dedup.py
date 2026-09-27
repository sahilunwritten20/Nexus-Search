"""Content-hash deduplication that survives updates and deletes."""
import hashlib
import sqlite3
import threading
from typing import Optional


def content_hash(text: str) -> str:
    """Stable hash: same content hashes identically regardless of case/whitespace."""
    normalized = " ".join(text.split()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class Deduplicator:
    """Shared-connection dedup store. Thread-safe on its own (internal
    RLock, same pattern as the other one-file stores) — callers must NOT
    rely on wrapping us in an outer lock for sqlite correctness."""

    def __init__(self, db_path: str = "nexus_search.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS content_hashes (hash TEXT PRIMARY KEY, doc_id TEXT NOT NULL)"
            )
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_hashes_doc ON content_hashes (doc_id)")
            self.conn.commit()

    def is_duplicate(self, text: str) -> bool:
        with self.lock:
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
                self.forget_hash(h)  # RLock: re-entrant, same thread
                # Also clean up any chunk hashes for this owner
                self.conn.execute(
                    "DELETE FROM content_hashes WHERE substr(doc_id, 1, ?) = ?",
                    (prefix_len, chunk_prefix),
                )
                self.conn.commit()
                return False
            return True

    def hash_of(self, doc_id: str) -> Optional[str]:
        """The hash this doc_id is currently registered under, or None."""
        with self.lock:
            row = self.conn.execute(
                "SELECT hash FROM content_hashes WHERE doc_id = ?", (doc_id,)).fetchone()
            return row[0] if row else None

    def register(self, text: str, doc_id: str):
        """Record this doc's current content; drops the doc's previous hash."""
        with self.lock:
            self.conn.execute("DELETE FROM content_hashes WHERE doc_id = ?", (doc_id,))
            self.conn.execute(
                "INSERT OR REPLACE INTO content_hashes (hash, doc_id) VALUES (?, ?)",
                (content_hash(text), doc_id),
            )
            self.conn.commit()

    def claim_if_new(self, text: str, doc_id: str) -> bool:
        """Atomic is_duplicate → register: exactly one caller per content
        hash wins. (ingest_one's check-then-register happens AFTER indexing
        by design — a failed ingest stays re-tryable — so callers needing
        thread-dedup should use this instead.)"""
        with self.lock:
            if self.is_duplicate(text):
                return False
            self.register(text, doc_id)
            return True

    def forget(self, doc_id: str):
        with self.lock:
            self.conn.execute("DELETE FROM content_hashes WHERE doc_id = ?", (doc_id,))
            self.conn.commit()

    def forget_hash(self, h: str):
        with self.lock:
            self.conn.execute("DELETE FROM content_hashes WHERE hash = ?", (h,))
            self.conn.commit()

    def close(self):
        with self.lock:
            self.conn.close()