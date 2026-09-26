"""Retry / dead-letter queue for ingestion failures.

Today a connector exception mid-batch either kills the loop or silently
drops the document — both are bad for a real ingestion system. This module
persists failures to SQLite with exponential backoff, so a transient DB
lock or a one-off corrupt upload doesn't lose work permanently, and a
permanently-bad document doesn't hot-loop.

Design (follows the project's Storage pattern: one file, busy_timeout,
RLock):
- record(): on ingest failure, persist (doc fields serialized) with
  attempts=1 and next_retry_at = now + base * 2**attempts
- due(): rows whose backoff has elapsed
- replay(): re-run the ingest function per due row; success deletes the row,
  failure bumps attempts and pushes next_retry_at further out (capped)
- rows past max_attempts stay listed but stop being retried — a human can
  list_dlq()/delete() them
"""
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass

from ..core.migrations import apply_migrations

logger = logging.getLogger("nexus_search.ingestion.failures")

SCHEMA = """
CREATE TABLE IF NOT EXISTS failed_ingestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 1,
    next_retry_at REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_failed_ingestions_due
    ON failed_ingestions (next_retry_at);
"""

DEFAULT_BASE_SECONDS = 5.0
DEFAULT_MAX_ATTEMPTS = 5
MAX_BACKOFF_SECONDS = 3600.0


@dataclass
class FailedIngestion:
    id: int
    doc_id: str
    title: str
    content: str
    doc_type: str
    metadata: dict
    error: str
    attempts: int
    next_retry_at: float
    created_at: float


class FailureQueue:
    def __init__(self, db_path: str = "nexus_search.db",
                 base_seconds: float = DEFAULT_BASE_SECONDS,
                 max_attempts: int = DEFAULT_MAX_ATTEMPTS):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.lock = threading.RLock()
        self.base_seconds = base_seconds
        self.max_attempts = max_attempts
        with self.lock:
            # Versioned like the other stores: v1 is this schema as-is;
            # columns added later become v2, ... (see core/migrations.py)
            self.schema_version = apply_migrations(
                self.conn, "failed_ingestions", [(1, SCHEMA)]
            )

    def _backoff(self, attempts: int) -> float:
        return min(MAX_BACKOFF_SECONDS, self.base_seconds * (2 ** (attempts - 1)))

    def record(self, doc, error: str) -> None:
        """Persist a failed ingest (doc = IngestDoc or anything with the same
        fields). Idempotent per doc_id: a repeat failure bumps attempts and
        pushes next_retry_at rather than inserting a second row.

        Fields are coerced defensively: a dead-letter queue that crashes on
        the exact malformed input it's supposed to record would be useless."""
        now = time.time()
        doc_id = str(getattr(doc, "doc_id", None) or "<unknown>")
        title = str(getattr(doc, "title", None) or "")
        content = getattr(doc, "content", None)
        content = content if isinstance(content, str) else ("" if content is None else str(content))
        doc_type = str(getattr(doc, "doc_type", None) or "")
        metadata = getattr(doc, "metadata", None) or {}
        with self.lock:
            row = self.conn.execute(
                "SELECT id, attempts FROM failed_ingestions WHERE doc_id = ?",
                (doc_id,)).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO failed_ingestions "
                    "(doc_id, title, content, doc_type, metadata, error, attempts, "
                    " next_retry_at, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (doc_id, title, content, doc_type,
                     json.dumps(metadata), error, 1,
                     now + self._backoff(1), now))
            else:
                attempts = row[1] + 1
                self.conn.execute(
                    "UPDATE failed_ingestions SET attempts = ?, error = ?, "
                    "next_retry_at = ? WHERE id = ?",
                    (attempts, error, now + self._backoff(attempts), row[0]))
            self.conn.commit()

    def due(self, now: float | None = None) -> list[FailedIngestion]:
        """Rows whose backoff has elapsed AND that still have attempts left."""
        now = time.time() if now is None else now
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, doc_id, title, content, doc_type, metadata, error, "
                "attempts, next_retry_at, created_at "
                "FROM failed_ingestions WHERE next_retry_at <= ? AND attempts < ? "
                "ORDER BY next_retry_at",
                (now, self.max_attempts)).fetchall()
        return [FailedIngestion(*r[:1], r[1], r[2], r[3], r[4],
                                json.loads(r[5]), r[6], r[7], r[8], r[9])
                for r in rows]

    def list_all(self) -> list[FailedIngestion]:
        """Everything dead-lettered, including exhausted rows (max attempts):
        those still want an operator's eye."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, doc_id, title, content, doc_type, metadata, error, "
                "attempts, next_retry_at, created_at FROM failed_ingestions "
                "ORDER BY created_at").fetchall()
        return [FailedIngestion(*r[:1], r[1], r[2], r[3], r[4],
                                json.loads(r[5]), r[6], r[7], r[8], r[9])
                for r in rows]

    def delete(self, row_id: int) -> bool:
        with self.lock:
            cur = self.conn.execute("DELETE FROM failed_ingestions WHERE id = ?",
                                    (row_id,))
            self.conn.commit()
            return cur.rowcount > 0

    def replay(self, ingest_fn, now: float | None = None) -> dict:
        """Re-run due rows through `ingest_fn(doc)`. Success deletes the row;
        failure re-records it (bumping backoff). ingest_fn must raise on
        failure — it is called inside the same contract as ingest_one."""
        from .types import IngestDoc
        stats = {"replayed": 0, "recovered": 0, "failed_again": 0}
        for row in self.due(now):
            stats["replayed"] += 1
            try:
                ingest_fn(IngestDoc(row.doc_id, row.title, row.content,
                                    row.doc_type, row.metadata))
            except Exception as exc:
                logger.warning("replay of %s failed again: %s", row.doc_id, exc)
                self.record(IngestDoc(row.doc_id, row.title, row.content,
                                      row.doc_type, row.metadata),
                            f"{type(exc).__name__}: {exc}")
                stats["failed_again"] += 1
            else:
                self.delete(row.id)
                stats["recovered"] += 1
        return stats

    def count(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM failed_ingestions").fetchone()[0]

    def close(self):
        with self.lock:
            self.conn.close()
