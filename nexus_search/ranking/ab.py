"""Phase 5 — A/B instrumentation for ranking experiments.

What this IS: deterministic variant bucketing + a per-query SQLite log, so a
production deployment can later compute real uplift from real traffic.

What this is NOT (and cannot be, honestly): an A/B ANALYSIS platform. There
is no live user traffic in this codebase, and fabricating "results" from a
local test is exactly the completeness theater SPEC.md forbids. The log
schema and bucketing ship tested; the analysis is downstream's job once it
has data ("instrumentation, not results").
"""
import hashlib
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS query_experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    variant TEXT NOT NULL,
    ranking_mode TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_query_experiments_query ON query_experiments (query);
CREATE INDEX IF NOT EXISTS idx_query_experiments_created ON query_experiments (created_at);
"""


def assign_variant(user_or_session_id: str, variants: list[str]) -> str:
    """Deterministic, hash-based bucket assignment.

    The same id ALWAYS lands in the same variant (across processes and
    restarts — no RNG state involved), and variants with equal length get
    ~uniform share (blake2b low bits). Raises on empty variant list."""
    if not variants:
        raise ValueError("variants must not be empty")
    digest = hashlib.blake2b(user_or_session_id.encode("utf-8"), digest_size=8).digest()
    bucket = int.from_bytes(digest, "little") % len(variants)
    return variants[bucket]


class ExperimentLog:
    """SQLite-backed query log following the core/storage.py pattern
    (one file, busy_timeout, RLock, tiny schema).

    The "experiment" recorded per row is just (query, variant, ranking_mode);
    joining this against whatever the user clicked is the downstream Phase 7+
    concern this schema is built to support.
    """

    def __init__(self, db_path: str = "nexus_search.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def record(self, query: str, variant: str, ranking_mode: str) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO query_experiments (query, variant, ranking_mode, created_at) "
                "VALUES (?, ?, ?, ?)",
                (query, variant, ranking_mode, time.time()),
            )
            self.conn.commit()

    def rows_for(self, query: str) -> list[tuple]:
        with self.lock:
            return self.conn.execute(
                "SELECT query, variant, ranking_mode, created_at FROM query_experiments "
                "WHERE query = ? ORDER BY id",
                (query,),
            ).fetchall()

    def count(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM query_experiments").fetchone()[0]

    def close(self):
        with self.lock:
            self.conn.close()
