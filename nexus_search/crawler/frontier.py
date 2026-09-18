"""SQLite-backed URL frontier for the Nexus Search crawler (Phase 3).

Persistent so a crawl survives a restart: pending URLs, visited URLs
(with content hash, for dedup), and errors all live in one SQLite file.
"""
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

from .url_utils import normalize_url

SCHEMA = """
CREATE TABLE IF NOT EXISTS frontier (
    url TEXT PRIMARY KEY,
    depth INTEGER NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    added_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS visited (
    url TEXT PRIMARY KEY,
    last_crawled_at REAL NOT NULL,
    content_hash TEXT
);

CREATE TABLE IF NOT EXISTS crawl_errors (
    url TEXT NOT NULL,
    error TEXT NOT NULL,
    timestamp REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_frontier_status_priority
    ON frontier (status, priority DESC, depth ASC);
"""


@dataclass
class FrontierEntry:
    url: str
    depth: int
    priority: int = 0


class Frontier:
    """Persistent, dedup'd URL queue. One frontier per crawl database."""

    def __init__(self, db_path: str = "crawler.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def add(self, url: str, depth: int, priority: int = 0, base: Optional[str] = None) -> bool:
        """Add a URL if it hasn't been seen (queued or visited). Returns True if newly added."""
        norm = normalize_url(url, base=base)

        cur = self.conn.execute("SELECT 1 FROM visited WHERE url = ?", (norm,))
        if cur.fetchone():
            return False

        try:
            self.conn.execute(
                "INSERT INTO frontier (url, depth, priority, status, added_at) "
                "VALUES (?, ?, ?, 'pending', ?)",
                (norm, depth, priority, time.time()),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False  # already queued or in progress

    def next_batch(self, n: int = 1) -> list[FrontierEntry]:
        """Claim up to n pending URLs, marking them in_progress so concurrent workers don't collide."""
        cur = self.conn.execute(
            "SELECT url, depth, priority FROM frontier "
            "WHERE status = 'pending' "
            "ORDER BY priority DESC, depth ASC, added_at ASC LIMIT ?",
            (n,),
        )
        rows = cur.fetchall()
        if not rows:
            return []

        urls = [r[0] for r in rows]
        placeholders = ",".join("?" * len(urls))
        self.conn.execute(
            f"UPDATE frontier SET status = 'in_progress' WHERE url IN ({placeholders})",
            urls,
        )
        self.conn.commit()
        return [FrontierEntry(url=r[0], depth=r[1], priority=r[2]) for r in rows]

    def mark_done(self, url: str, content_hash: Optional[str] = None):
        norm = normalize_url(url)
        self.conn.execute("DELETE FROM frontier WHERE url = ?", (norm,))
        self.conn.execute(
            "INSERT OR REPLACE INTO visited (url, last_crawled_at, content_hash) VALUES (?, ?, ?)",
            (norm, time.time(), content_hash),
        )
        self.conn.commit()

    def mark_error(self, url: str, error: str):
        norm = normalize_url(url)
        self.conn.execute("UPDATE frontier SET status = 'error' WHERE url = ?", (norm,))
        self.conn.execute(
            "INSERT INTO crawl_errors (url, error, timestamp) VALUES (?, ?, ?)",
            (norm, error, time.time()),
        )
        self.conn.commit()

    def is_visited(self, url: str) -> bool:
        norm = normalize_url(url)
        cur = self.conn.execute("SELECT 1 FROM visited WHERE url = ?", (norm,))
        return cur.fetchone() is not None

    def pending_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM frontier WHERE status = 'pending'").fetchone()[0]

    def visited_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM visited").fetchone()[0]

    def close(self):
        self.conn.close()
