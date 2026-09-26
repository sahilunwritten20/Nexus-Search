"""Webmaster opt-out blocklist for the crawler.

robots.txt is fine-grained but slow to change and some site owners just want
OUT — one email, no HTTP head games. This is that documented channel: a
durable blocklist table, checked before every fetch.

Store: same frontier DB (one crawl-state file), Storage-style pattern.
Precedence: blocklist > robots.txt > allow-list. A blocked URL is skipped
silently by the pipeline and NOT marked visited, so unblocking is effective
"the moment it's happy again" (no stale visit records to evict).
"""
import sqlite3
import threading
import time
from urllib.parse import urlsplit

from ..core.migrations import apply_migrations

SCHEMA = """
CREATE TABLE IF NOT EXISTS blocklist (
    host TEXT PRIMARY KEY,
    reason TEXT,
    created_at REAL NOT NULL
);
"""


class Blocklist:
    def __init__(self, db_path: str = "crawler.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.lock = threading.RLock()
        with self.lock:
            # Versioned like the other stores: v1 is this schema as-is;
            # columns added later become v2, ... (see core/migrations.py)
            self.schema_version = apply_migrations(
                self.conn, "blocklist", [(1, SCHEMA)]
            )

    @staticmethod
    def _host_of(url_or_host: str) -> str:
        host = urlsplit(url_or_host).hostname or url_or_host
        return host.strip().lower().lstrip(".")

    def block(self, url_or_host: str, reason: str = "") -> None:
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO blocklist (host, reason, created_at) VALUES (?,?,?)",
                (self._host_of(url_or_host), reason, time.time()))
            self.conn.commit()

    def unblock(self, url_or_host: str) -> bool:
        with self.lock:
            cur = self.conn.execute(
                "DELETE FROM blocklist WHERE host = ?", (self._host_of(url_or_host),))
            self.conn.commit()
            return cur.rowcount > 0

    def is_blocked(self, url_or_host: str) -> bool:
        host = self._host_of(url_or_host)
        if not host:
            return False
        with self.lock:
            cur = self.conn.execute("SELECT 1 FROM blocklist WHERE host = ?", (host,))
            if cur.fetchone():
                return True
            # parent-domain block counts for subdomains (opt-out of "example.com"
            # has to include "blog.example.com", operators' mental model)
            parts = host.split(".")
            for i in range(1, len(parts) - 1):
                parent = ".".join(parts[i:])
                if self.conn.execute(
                        "SELECT 1 FROM blocklist WHERE host = ?", (parent,)).fetchone():
                    return True
        return False

    def list_all(self) -> list[tuple]:
        with self.lock:
            return self.conn.execute(
                "SELECT host, reason, created_at FROM blocklist ORDER BY host").fetchall()

    def close(self):
        with self.lock:
            self.conn.close()
