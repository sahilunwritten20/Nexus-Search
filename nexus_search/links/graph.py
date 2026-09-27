"""Persistent link-graph store for Phase 6 (Link Intelligence).

One SQLite store per DB file, same discipline as every other store in this
codebase: shared connection + busy_timeout + RLock, schema via the versioned
migrations runner (store name "link_graph").

Edges are (from_url, to_url) — the normalized URLs the frontier already uses.
Recrawls UPDATE last_seen and refresh anchor/rel; they never duplicate an
edge. Anti-flood caps bound pathology: a page linking 50,000 times or two
domains cross-linking endlessly cannot blow up the table.
"""
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

from ..core.migrations import apply_migrations

SCHEMA = """
CREATE TABLE IF NOT EXISTS link_edges (
    from_url TEXT NOT NULL,
    to_url TEXT NOT NULL,
    anchor_text TEXT NOT NULL DEFAULT '',
    rel_attrs TEXT NOT NULL DEFAULT '',
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    PRIMARY KEY (from_url, to_url)
);
CREATE INDEX IF NOT EXISTS idx_link_edges_to ON link_edges (to_url);
CREATE INDEX IF NOT EXISTS idx_link_edges_from ON link_edges (from_url);

CREATE TABLE IF NOT EXISTS authority_scores (
    url TEXT PRIMARY KEY,
    pagerank REAL NOT NULL,
    inlink_count INTEGER NOT NULL,
    inlink_domains INTEGER NOT NULL,
    authority REAL NOT NULL,
    popularity REAL NOT NULL,
    computed_at REAL NOT NULL
)
"""

# ---------------------------------------------------------------------------
# Anti-flood caps. These bound TABLE growth from pathological pages/pairs;
# ranking-side spam resistance (weighting) lives in authority.py.
MAX_EDGES_PER_SOURCE_PAGE = 1000
MAX_EDGES_PER_DOMAIN_PAIR = 500


@dataclass
class LinkEdge:
    from_url: str
    to_url: str
    anchor_text: str = ""
    rel_attrs: str = ""


def _domain_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


class LinkGraph:
    """Durable (from_url -> to_url) edge store with anti-flood caps."""

    def __init__(self, db_path: str = "nexus_search.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.lock = threading.RLock()
        with self.lock:
            self.schema_version = apply_migrations(
                self.conn, "link_graph", [(1, SCHEMA)]
            )

    # ------------------------------------------------------------- writes

    def record_edge(self, from_url: str, to_url: str,
                    anchor_text: str = "", rel_attrs: str = "") -> bool:
        """Record/refresh one edge. False when refused (self-link or cap hit).
        Recrawl semantics: an existing edge only updates last_seen + metadata."""
        if not from_url or not to_url or from_url == to_url:
            return False  # self-votes never count
        now = time.time()
        with self.lock:
            existing = self.conn.execute(
                "SELECT 1 FROM link_edges WHERE from_url = ? AND to_url = ?",
                (from_url, to_url)).fetchone()
            if existing:
                self.conn.execute(
                    "UPDATE link_edges SET anchor_text = ?, rel_attrs = ?, "
                    "last_seen = ? WHERE from_url = ? AND to_url = ?",
                    (anchor_text, rel_attrs, now, from_url, to_url))
                self.conn.commit()
                return True
            # per-source-page cap
            out_count = self.conn.execute(
                "SELECT COUNT(*) FROM link_edges WHERE from_url = ?",
                (from_url,)).fetchone()[0]
            if out_count >= MAX_EDGES_PER_SOURCE_PAGE:
                return False
            # per domain-pair cap (ports are part of the host key pattern)
            f_dom, t_dom = _domain_of(from_url), _domain_of(to_url)
            pair_count = self.conn.execute(
                "SELECT COUNT(*) FROM link_edges WHERE from_url LIKE ? AND to_url LIKE ?",
                (f"%//{f_dom}%/%", f"%//{t_dom}%/%")).fetchone()[0]
            if pair_count >= MAX_EDGES_PER_DOMAIN_PAIR:
                return False
            self.conn.execute(
                "INSERT INTO link_edges (from_url, to_url, anchor_text, rel_attrs, "
                "first_seen, last_seen) VALUES (?,?,?,?,?,?)",
                (from_url, to_url, anchor_text, rel_attrs, now, now))
            self.conn.commit()
            return True

    # -------------------------------------------------------------- reads

    def edges(self) -> list[LinkEdge]:
        """All edges (recompute inputs — offline path only)."""
        with self.lock:
            return [LinkEdge(*row) for row in self.conn.execute(
                "SELECT from_url, to_url, anchor_text, rel_attrs FROM link_edges")]

    def edge_count(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM link_edges").fetchone()[0]

    def authority_for(self, url: str) -> Optional[tuple[float, float]]:
        """(authority, popularity) for a URL, None when the graph has never
        heard of it — the ranker maps None to NEUTRAL, never to zero."""
        with self.lock:
            row = self.conn.execute(
                "SELECT authority, popularity FROM authority_scores WHERE url = ?",
                (url,)).fetchone()
            return (row[0], row[1]) if row else None

    def stats(self) -> dict:
        with self.lock:
            return {
                "edges": self.edge_count(),
                "scored_urls": self.conn.execute(
                    "SELECT COUNT(*) FROM authority_scores").fetchone()[0],
            }

    def close(self):
        with self.lock:
            self.conn.close()
