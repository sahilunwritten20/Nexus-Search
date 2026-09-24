"""SQLite-backed URL frontier for the Nexus Search crawler."""

from __future__ import annotations

import sqlite3
import threading
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
    content_hash TEXT,
    etag TEXT,
    last_modified TEXT
);

CREATE TABLE IF NOT EXISTS crawl_errors (
    url TEXT NOT NULL,
    error TEXT NOT NULL,
    timestamp REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_frontier_status_priority
    ON frontier (status, priority DESC, depth ASC);

CREATE INDEX IF NOT EXISTS idx_visited_last_crawled
    ON visited (last_crawled_at);
"""


@dataclass
class FrontierEntry:
    """A URL waiting to be crawled."""

    url: str
    depth: int
    priority: int = 0
    etag: Optional[str] = None
    last_modified: Optional[str] = None


class Frontier:
    """Persistent, deduplicated URL queue."""

    def __init__(self, db_path: str = "crawler.db"):
        self.conn = sqlite3.connect(
            db_path,
            check_same_thread=False,
        )

        self.conn.execute("PRAGMA busy_timeout = 5000")

        self.lock = threading.RLock()

        self.conn.executescript(SCHEMA)

        # Existing databases from the earlier version may not have
        # these columns.
        self._ensure_column(
            "visited",
            "etag",
            "TEXT",
        )

        self._ensure_column(
            "visited",
            "last_modified",
            "TEXT",
        )

        # Crash recovery: URLs claimed by a run that died (or skipped by
        # last run's limits) get another chance. One crawler process per frontier DB.
        self.conn.execute(
            "UPDATE frontier SET status = 'pending' "
            "WHERE status IN ('in_progress', 'skipped')"
        )

        self.conn.commit()

    # ================================================================
    # Database migration
    # ================================================================

    def _ensure_column(
        self,
        table: str,
        column: str,
        column_type: str,
    ) -> None:
        """Add a column if it does not already exist."""

        columns = self.conn.execute(
            f"PRAGMA table_info({table})"
        ).fetchall()

        existing_columns = {
            row[1]
            for row in columns
        }

        if column not in existing_columns:
            self.conn.execute(
                f"""
                ALTER TABLE {table}
                ADD COLUMN {column} {column_type}
                """
            )

    # ================================================================
    # Add URL
    # ================================================================

    def add(
        self,
        url: str,
        depth: int,
        priority: int = 0,
        base: Optional[str] = None,
        allow_visited: bool = False,
    ) -> bool:
        """
        Add a URL to the frontier.

        Returns True when the URL was added.

        Returns False when:
        - the URL has already been visited
        - the URL is already in the frontier
        """

        norm = normalize_url(
            url,
            base=base,
        )

        with self.lock:

            # Normally visited URLs must not be added again.
            # Recrawling can explicitly bypass this restriction.
            if not allow_visited:

                cur = self.conn.execute(
                    """
                    SELECT 1
                    FROM visited
                    WHERE url = ?
                    """,
                    (norm,),
                )

                if cur.fetchone():
                    return False

            try:
                self.conn.execute(
                    """
                    INSERT INTO frontier (
                        url,
                        depth,
                        priority,
                        status,
                        added_at
                    )
                    VALUES (
                        ?,
                        ?,
                        ?,
                        'pending',
                        ?
                    )
                    """,
                    (
                        norm,
                        depth,
                        priority,
                        time.time(),
                    ),
                )

                self.conn.commit()

                return True

            except sqlite3.IntegrityError:
                # URL is already queued or in progress.
                return False

    # ================================================================
    # Get next batch
    # ================================================================

    def next_batch(
        self,
        n: int = 1,
    ) -> list[FrontierEntry]:
        """
        Claim up to n pending URLs.

        Claimed URLs are changed to in_progress so that another
        worker cannot claim them again.
        """

        if n <= 0:
            return []

        with self.lock:

            rows = self.conn.execute(
                """
                SELECT
                    f.url,
                    f.depth,
                    f.priority,
                    v.etag,
                    v.last_modified
                FROM frontier AS f
                LEFT JOIN visited AS v
                    ON f.url = v.url
                WHERE f.status = 'pending'
                ORDER BY
                    f.priority DESC,
                    f.depth ASC,
                    f.added_at ASC
                LIMIT ?
                """,
                (n,),
            ).fetchall()

            if not rows:
                return []

            urls = [
                row[0]
                for row in rows
            ]

            placeholders = ",".join(
                "?" for _ in urls
            )

            self.conn.execute(
                f"""
                UPDATE frontier
                SET status = 'in_progress'
                WHERE url IN ({placeholders})
                """,
                urls,
            )

            self.conn.commit()

            return [
                FrontierEntry(
                    url=row[0],
                    depth=row[1],
                    priority=row[2],
                    etag=row[3],
                    last_modified=row[4],
                )
                for row in rows
            ]

    # ================================================================
    # Mark done
    # ================================================================

    def mark_done(
        self,
        url: str,
        content_hash: Optional[str] = None,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
    ) -> None:
        """Mark a URL as successfully crawled."""

        norm = normalize_url(url)

        with self.lock:

            # Remove it from the frontier.
            self.conn.execute(
                """
                DELETE FROM frontier
                WHERE url = ?
                """,
                (norm,),
            )

            # Store/update visited information.
            self.conn.execute(
                """
                INSERT INTO visited (
                    url,
                    last_crawled_at,
                    content_hash,
                    etag,
                    last_modified
                )
                VALUES (
                    ?,
                    ?,
                    ?,
                    ?,
                    ?
                )
                ON CONFLICT(url)
                DO UPDATE SET
                    last_crawled_at =
                        excluded.last_crawled_at,
                    content_hash =
                        excluded.content_hash,
                    etag =
                        excluded.etag,
                    last_modified =
                        excluded.last_modified
                """,
                (
                    norm,
                    time.time(),
                    content_hash,
                    etag,
                    last_modified,
                ),
            )

            self.conn.commit()

    # ================================================================
    # Mark not modified
    # ================================================================

    def mark_not_modified(
        self,
        url: str,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
    ) -> None:
        """
        Mark a URL as not modified.

        The existing content hash is preserved.

        Validators are updated when new values are supplied.
        """

        norm = normalize_url(url)

        with self.lock:

            # Remove from frontier.
            self.conn.execute(
                """
                DELETE FROM frontier
                WHERE url = ?
                """,
                (norm,),
            )

            # Update only crawl time and validators.
            # content_hash is deliberately NOT changed.
            self.conn.execute(
                """
                UPDATE visited
                SET
                    last_crawled_at = ?,
                    etag = COALESCE(?, etag),
                    last_modified =
                        COALESCE(?, last_modified)
                WHERE url = ?
                """,
                (
                    time.time(),
                    etag,
                    last_modified,
                    norm,
                ),
            )

            self.conn.commit()

    # ================================================================
    # Mark skipped
    # ================================================================

    def mark_skipped(self, url: str) -> None:
        """Deliberately not fetched (robots, domain limit, unsafe host).
        NOT recorded as visited, so a later run can still crawl it."""
        norm = normalize_url(url)
        with self.lock:
            self.conn.execute(
                "UPDATE frontier SET status = 'skipped' WHERE url = ?", (norm,)
            )
            self.conn.commit()

    # ================================================================
    # Mark error
    # ================================================================

    def mark_error(
        self,
        url: str,
        error: str,
    ) -> None:
        """Mark a crawl attempt as failed."""

        norm = normalize_url(url)

        with self.lock:

            # Remove the URL from the active frontier.
            self.conn.execute(
                """
                DELETE FROM frontier
                WHERE url = ?
                """,
                (norm,),
            )

            # Store the error.
            self.conn.execute(
                """
                INSERT INTO crawl_errors (
                    url,
                    error,
                    timestamp
                )
                VALUES (
                    ?,
                    ?,
                    ?
                )
                """,
                (
                    norm,
                    error,
                    time.time(),
                ),
            )

            self.conn.commit()

    # ================================================================
    # Visited
    # ================================================================

    def is_visited(
        self,
        url: str,
    ) -> bool:
        """Return True if the URL has been crawled."""

        norm = normalize_url(url)

        with self.lock:

            cur = self.conn.execute(
                """
                SELECT 1
                FROM visited
                WHERE url = ?
                """,
                (norm,),
            )

            return cur.fetchone() is not None

    # ================================================================
    # Get visited metadata
    # ================================================================

    def get_visited(
        self,
        url: str,
    ):
        """
        Return visited metadata.

        Tuple format:

        (
            url,
            content_hash,
            etag,
            last_modified,
            last_crawled_at
        )
        """

        norm = normalize_url(url)

        with self.lock:

            row = self.conn.execute(
                """
                SELECT
                    url,
                    content_hash,
                    etag,
                    last_modified,
                    last_crawled_at
                FROM visited
                WHERE url = ?
                """,
                (norm,),
            ).fetchone()

            return row

    # ================================================================
    # Recrawl URLs
    # ================================================================

    def recrawl_due_urls(
        self,
        interval_seconds: float,
    ) -> list[str]:
        """
        Return URLs whose recrawl interval has expired.

        This is the single recrawl-query API — it does NOT modify the
        frontier; the caller (CrawlPipeline._queue_recrawls) re-adds the
        URLs with fresh priority/depth via add(..., allow_visited=True),
        so the policies for requeuing live in exactly one place.
        """

        if interval_seconds < 0:
            raise ValueError(
                "interval_seconds cannot be negative"
            )

        cutoff = (
            time.time()
            - interval_seconds
        )

        with self.lock:

            rows = self.conn.execute(
                """
                SELECT url
                FROM visited
                WHERE last_crawled_at <= ?
                """,
                (cutoff,),
            ).fetchall()

            return [
                row[0]
                for row in rows
            ]

    # ================================================================
    # Counts
    # ================================================================

    def pending_count(self) -> int:
        """Return the number of pending URLs."""

        with self.lock:

            return self.conn.execute(
                """
                SELECT COUNT(*)
                FROM frontier
                WHERE status = 'pending'
                """
            ).fetchone()[0]

    def visited_count(self) -> int:
        """Return the number of visited URLs."""

        with self.lock:

            return self.conn.execute(
                """
                SELECT COUNT(*)
                FROM visited
                """
            ).fetchone()[0]

    # ================================================================
    # Close
    # ================================================================

    def close(self) -> None:
        """Close the SQLite database connection."""

        with self.lock:

            if self.conn is not None:
                self.conn.close()
                self.conn = None