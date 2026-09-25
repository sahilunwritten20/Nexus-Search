"""Content versioning: an append-only audit trail for document changes.

Today re-ingesting a changed document silently overwrites it; nothing can
answer "what did this URL look like last Tuesday?". This module records the
PREVIOUS content (the actual text, so diffs are real, not just hash flips)
every time a doc's hash changes. First-time ingests are NOT recorded (there
is no previous version to preserve); unchanged re-ingests add nothing —
the pipeline comparison-checks before calling record_change().
"""
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS content_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    prev_content TEXT NOT NULL,
    new_hash TEXT NOT NULL,
    recorded_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_content_history_doc ON content_history (doc_id);
"""


class ContentHistory:
    """Per-DB history of content changes. Follows the Storage pattern."""

    def __init__(self, db_path: str = "nexus_search.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def record_change(self, doc_id: str, prev_hash: str, prev_content: str,
                      new_hash: str) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO content_history (doc_id, prev_hash, prev_content, "
                "new_hash, recorded_at) VALUES (?,?,?,?,?)",
                (doc_id, prev_hash, prev_content, new_hash, time.time()))
            self.conn.commit()

    def history_for(self, doc_id: str) -> list[tuple]:
        with self.lock:
            return self.conn.execute(
                "SELECT prev_hash, prev_content, new_hash, recorded_at FROM "
                "content_history WHERE doc_id = ? ORDER BY id", (doc_id,)).fetchall()

    def count(self, doc_id: str | None = None) -> int:
        with self.lock:
            if doc_id is None:
                return self.conn.execute("SELECT COUNT(*) FROM content_history").fetchone()[0]
            return self.conn.execute(
                "SELECT COUNT(*) FROM content_history WHERE doc_id = ?",
                (doc_id,)).fetchone()[0]

    def close(self):
        with self.lock:
            self.conn.close()
