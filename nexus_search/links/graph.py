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
from ..crawler.url_utils import normalize_url

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

    @staticmethod
    def _norm_pair(from_url: str, to_url: str) -> Optional[tuple[str, str]]:
        """The ONE identity choke point (BUG-02): every edge is stored under
        the frontier's normalizer — fragments, tracking params, trailing
        slashes, host case, default ports and unreserved percent-escapes all
        collapse — so graph nodes and document metadata['url'] share the
        same URL space. None when either side is unparseable or the edge is
        a post-normalization self-link."""
        if not from_url or not to_url:
            return None
        try:
            fn = normalize_url(from_url)
            tn = normalize_url(to_url)
        except ValueError:
            return None
        if fn == tn:
            return None  # self-votes never count (fragments included)
        return fn, tn

    def record_edge(self, from_url: str, to_url: str,
                    anchor_text: str = "", rel_attrs: str = "") -> bool:
        """Record/refresh one edge. False when refused (self-link, unparseable
        URL, or cap hit). Recrawl semantics: an existing edge only updates
        last_seen + metadata."""
        pair = self._norm_pair(from_url, to_url)
        if pair is None:
            return False
        from_url, to_url = pair
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
            # per domain-pair cap (check same from_domain -> to_domain)
            f_dom, t_dom = _domain_of(from_url), _domain_of(to_url)
            pair_count = self.conn.execute(
                "SELECT COUNT(*) FROM link_edges WHERE from_url LIKE ? AND to_url LIKE ?",
                (f"%//{f_dom}/%", f"%//{t_dom}/%")).fetchone()[0]
            if pair_count >= MAX_EDGES_PER_DOMAIN_PAIR:
                return False
            self.conn.execute(
                "INSERT INTO link_edges (from_url, to_url, anchor_text, rel_attrs, "
                "first_seen, last_seen) VALUES (?,?,?,?,?,?)",
                (from_url, to_url, anchor_text, rel_attrs, now, now))
            self.conn.commit()
            return True

    def record_edges(self, edges) -> int:
        """Bulk record for trusted backfills/tests (self-links dropped; the
        anti-flood per-page/domain-pair caps are enforced by record_edge,
        the path live crawls use). One commit per 5,000 rows — a per-edge
        commit would make big crawls commit-bound. Returns rows inserted.
        Endpoints are normalized through the same choke point as
        record_edge (BUG-02) — bulk callers cannot bypass identity."""
        if isinstance(edges, (str, bytes)):
            raise TypeError("edges must be (from_url, to_url, anchor, rel) rows")
        now = time.time()
        inserted, pending = 0, []
        with self.lock:
            for from_url, to_url, anchor_text, rel_attrs in edges:
                pair = self._norm_pair(from_url, to_url)
                if pair is None:
                    continue
                fn, tn = pair
                pending.append((fn, tn, anchor_text, rel_attrs, now, now))
                if len(pending) >= 5000:
                    self.conn.executemany(
                        "INSERT OR REPLACE INTO link_edges (from_url, to_url, "
                        "anchor_text, rel_attrs, first_seen, last_seen) "
                        "VALUES (?,?,?,?,?,?)", pending)
                    self.conn.commit()
                    inserted += len(pending)
                    pending.clear()
            if pending:
                self.conn.executemany(
                    "INSERT OR REPLACE INTO link_edges (from_url, to_url, anchor_text, "
                    "rel_attrs, first_seen, last_seen) VALUES (?,?,?,?,?,?)", pending)
                self.conn.commit()
                inserted += len(pending)
        return inserted

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
        heard of it — the ranker maps None to NEUTRAL, never to zero.
        The lookup is normalized through the same choke point writes use
        (BUG-02), so a caller holding a raw/variant URL still resolves."""
        if not url:
            return None
        try:
            url = normalize_url(url)
        except ValueError:
            return None
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


def normalize_existing_edges(graph: LinkGraph) -> dict:
    """Idempotent data migration for rows written before BUG-02's fix
    (raw, unnormalized endpoints). The versioned migration runner is
    SQL-only and URL canonicalization is Python, so this CLI-invoked
    function IS the migration — deterministic, transactional, and a
    complete no-op on an already-normalized table.

    Merge policy per group of raw rows that normalize to one edge:
    - first_seen: earliest observation; last_seen: latest
    - anchor/rel: from the most-recently-seen row (recrawl semantics);
      anchor falls back to the first non-empty anchor in latest-first
      order when the newest row's anchor is empty
    - post-normalization self-links are dropped

    Returns {"rows_in", "edges_out", "merged_groups", "self_links_dropped"}.
    Re-run compute_authority afterwards (the crawler CLI's normalize-links
    --recompute does both)."""
    with graph.lock:
        conn = graph.conn
        rows = conn.execute(
            "SELECT from_url, to_url, anchor_text, rel_attrs, "
            "first_seen, last_seen FROM link_edges").fetchall()
        groups: dict[tuple[str, str], list] = {}
        self_link_rows: list[tuple[str, str]] = []
        for f, t, a, r, fs, ls in rows:
            pair = LinkGraph._norm_pair(f, t)
            if pair is None:
                if f and t:
                    # post-normalization self-vote: drop the raw row entirely
                    self_link_rows.append((f, t))
                continue
            groups.setdefault(pair, []).append((f, t, a, r, fs, ls))

        conn.execute("BEGIN IMMEDIATE")
        try:
            for f, t in self_link_rows:
                conn.execute(
                    "DELETE FROM link_edges WHERE from_url = ? AND to_url = ?",
                    (f, t))
            for (fn, tn), members in groups.items():
                # latest-first deterministic order for anchor fallback
                ordered = sorted(members, key=lambda m: (-m[5], m[0], m[1]))
                latest = ordered[0]
                anchor = latest[2] if latest[2].strip() else next(
                    (m[2] for m in ordered if m[2].strip()), "")
                rel = latest[3]
                first_seen = min(m[4] for m in members)
                last_seen = max(m[5] for m in members)
                for m in members:
                    conn.execute(
                        "DELETE FROM link_edges WHERE from_url = ? AND to_url = ?",
                        (m[0], m[1]))
                conn.execute(
                    "INSERT INTO link_edges (from_url, to_url, anchor_text, "
                    "rel_attrs, first_seen, last_seen) VALUES (?,?,?,?,?,?)",
                    (fn, tn, anchor, rel, first_seen, last_seen))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    return {
        "rows_in": len(rows),
        "edges_out": len(groups),
        "merged_groups": sum(1 for m in groups.values() if len(m) > 1),
        "self_links_dropped": len(self_link_rows),
    }
