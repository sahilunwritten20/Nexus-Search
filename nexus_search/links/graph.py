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

# v2 (BUG-05): from_domain/to_domain columns + index. The per-domain-pair
# anti-flood cap previously ran `LIKE '%//host/%'` — a full-table scan per
# NEW edge (O(E^2) ingest) that also missed root URLs without a trailing
# slash and counted subdomains by accident of the pattern. Domains are now
# first-class columns and the cap is an indexed COUNT.
#
# Subdomain policy (deliberate, documented): exact-host. a.com and
# sub.a.com are DIFFERENT domains for the pair cap — no public-suffix-list
# dependency at prototype scale; a subdomain farm is still bounded by the
# per-source-page cap (1000) and the pair cap per exact host (500).
_SCHEMA_V2 = """
ALTER TABLE link_edges ADD COLUMN from_domain TEXT NOT NULL DEFAULT '';
ALTER TABLE link_edges ADD COLUMN to_domain TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_link_edges_domains ON link_edges (from_domain, to_domain);
"""

# v3 (WP5): link-graph analysis features.
# - is_internal: -1 = unset (backfilled at open), 0 = external, 1 = internal.
#   Internal means SAME EXACT HOST (the one "domain" definition this
#   codebase uses — see the v2 policy note).
# - url_anchors: per-URL aggregated inbound anchor text (rebuilt by each
#   authority recompute; feeds the anchor_relevance ranking signal).
# - domain_authority: per-domain aggregate scores (page-authority fallback
#   behind NEXUS_DOMAIN_AUTHORITY_FALLBACK, default off).
# - graph_meta: key/value store for the edge-version counter (incremental
#   recompute guard) and friends.
_SCHEMA_V3 = """
ALTER TABLE link_edges ADD COLUMN is_internal INTEGER NOT NULL DEFAULT -1;
CREATE TABLE IF NOT EXISTS url_anchors (
    url TEXT PRIMARY KEY,
    anchor_text TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS domain_authority (
    domain TEXT PRIMARY KEY,
    authority REAL NOT NULL,
    popularity REAL NOT NULL,
    pages INTEGER NOT NULL,
    computed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS graph_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class LinkEdge:
    from_url: str
    to_url: str
    anchor_text: str = ""
    rel_attrs: str = ""


def _domain_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _domain_of_any(url: str) -> str:
    """Domain of a possibly-raw URL (backfill path): normalize when we can,
    never raise — a row the canonicalizer chokes on keeps its raw host."""
    try:
        url = normalize_url(url)
    except ValueError:
        pass
    return _domain_of(url)


def _domain_authority_fallback_enabled() -> bool:
    import os
    return (os.environ.get("NEXUS_DOMAIN_AUTHORITY_FALLBACK", "") or "").strip() \
        in ("1", "true", "yes")


class LinkGraph:
    """Durable (from_url -> to_url) edge store with anti-flood caps."""

    def __init__(self, db_path: str = "nexus_search.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA busy_timeout = 5000")
        # WAL, like every other store: the per-edge commits of a crawl's
        # record_edge loop must not pay a rollback-journal fsync each time
        # (BUG-05 measurements showed 4-50ms/edge without it on Windows)
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.lock = threading.RLock()
        # In-memory authority read cache (WP5): url -> (authority, popularity).
        # Invalidation: in-process recomputes call invalidate_authority_cache()
        # (own commits don't move PRAGMA data_version); cross-process writes
        # (CLI recompute, crawls) are detected via data_version polling.
        self._authority_cache: dict[str, tuple[float, float]] = {}
        self._authority_data_version: int = -1
        with self.lock:
            self.schema_version = apply_migrations(
                self.conn, "link_graph", [(1, SCHEMA), (2, _SCHEMA_V2),
                                           (3, _SCHEMA_V3)]
            )
            self._backfill_domains()
            self._backfill_is_internal()

    def _backfill_domains(self) -> None:
        """One-time backfill for rows written before v2 (empty from_domain).
        Idempotent: only rows with an empty domain are touched, and every
        write path populates both columns from v2 on."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT from_url, to_url FROM link_edges "
                "WHERE from_domain = '' OR to_domain = ''").fetchall()
            if not rows:
                return
            updates = [(_domain_of_any(f), _domain_of_any(t), f, t)
                       for f, t in rows]
            self.conn.executemany(
                "UPDATE link_edges SET from_domain = ?, to_domain = ? "
                "WHERE from_url = ? AND to_url = ?", updates)
            self.conn.commit()

    def _backfill_is_internal(self) -> None:
        """One-time backfill for rows written before v3 (is_internal = -1).
        Idempotent: only -1 rows are touched; every write path sets it."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT from_url, to_url FROM link_edges WHERE is_internal = -1"
            ).fetchall()
            if not rows:
                return
            updates = [(1 if _domain_of_any(f) == _domain_of_any(t) else 0, f, t)
                       for f, t in rows]
            self.conn.executemany(
                "UPDATE link_edges SET is_internal = ? "
                "WHERE from_url = ? AND to_url = ?", updates)
            self.conn.commit()

    # ----------------------------------------------------- version counter

    def bump_edges_version(self) -> None:
        """Increment the edge-set version (graph_meta['edges_version']) —
        the incremental-recompute guard: authority recompute is skipped
        when the version hasn't moved since the last pass. Callers bump on
        every INSERT and on UPDATEs that change anchor/rel (recrawl refresh
        of last_seen alone is not a semantic change)."""
        with self.lock:
            self.conn.execute(
                "INSERT INTO graph_meta (key, value) "
                "VALUES ('edges_version', '1') "
                "ON CONFLICT(key) DO UPDATE SET "
                "value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)")
            self.conn.commit()

    def edges_version(self) -> int:
        with self.lock:
            row = self.conn.execute(
                "SELECT value FROM graph_meta WHERE key = 'edges_version'"
            ).fetchone()
            return int(row[0]) if row else 0

    # ------------------------------------------------- authority read cache

    def invalidate_authority_cache(self) -> None:
        """Drop the in-memory authority cache. Called by in-process
        recomputes after the shadow swap (a connection's own commits do not
        move PRAGMA data_version, so polling cannot see them)."""
        with self.lock:
            self._authority_cache.clear()

    def _refresh_cache_if_stale(self) -> None:
        """Detect cross-process writes (CLI recompute, crawler) via
        PRAGMA data_version and drop the cache when they happened."""
        current = self.conn.execute("PRAGMA data_version").fetchone()[0]
        if current != self._authority_data_version:
            self._authority_data_version = current
            self._authority_cache.clear()

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
        is_internal = 1 if _domain_of(from_url) == _domain_of(to_url) else 0
        with self.lock:
            existing = self.conn.execute(
                "SELECT anchor_text, rel_attrs FROM link_edges "
                "WHERE from_url = ? AND to_url = ?",
                (from_url, to_url)).fetchone()
            if existing:
                # recrawl semantics: refresh anchor/rel + last_seen. A
                # metadata change moves the edge-set version (the anchor
                # feeds url_anchors on recompute); a pure last_seen refresh
                # does not — nothing downstream reads last_seen.
                if existing[0] != anchor_text or existing[1] != rel_attrs:
                    self.conn.execute(
                        "UPDATE link_edges SET anchor_text = ?, rel_attrs = ?, "
                        "last_seen = ?, is_internal = ? "
                        "WHERE from_url = ? AND to_url = ?",
                        (anchor_text, rel_attrs, now, is_internal, from_url, to_url))
                    self.conn.commit()
                    self.bump_edges_version()
                else:
                    self.conn.execute(
                        "UPDATE link_edges SET last_seen = ? "
                        "WHERE from_url = ? AND to_url = ?",
                        (now, from_url, to_url))
                    self.conn.commit()
                return True
            # per-source-page cap
            out_count = self.conn.execute(
                "SELECT COUNT(*) FROM link_edges WHERE from_url = ?",
                (from_url,)).fetchone()[0]
            if out_count >= MAX_EDGES_PER_SOURCE_PAGE:
                return False
            # per domain-pair cap — an INDEXED count on the v2 domain columns
            # (BUG-05: the pre-v2 LIKE '%//host/%' form was a full-table
            # scan per new edge = O(E^2) ingest, and it missed root URLs
            # without a trailing slash). Exact-host policy, documented above.
            # Host-less URLs (no scheme, e.g. test fixtures) have no domain
            # pair to budget — the old LIKE form never matched them either.
            f_dom, t_dom = _domain_of(from_url), _domain_of(to_url)
            if f_dom and t_dom:
                pair_count = self.conn.execute(
                    "SELECT COUNT(*) FROM link_edges "
                    "WHERE from_domain = ? AND to_domain = ?",
                    (f_dom, t_dom)).fetchone()[0]
                if pair_count >= MAX_EDGES_PER_DOMAIN_PAIR:
                    return False
            self.conn.execute(
                "INSERT INTO link_edges (from_url, to_url, anchor_text, rel_attrs, "
                "from_domain, to_domain, is_internal, first_seen, last_seen) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (from_url, to_url, anchor_text, rel_attrs, f_dom, t_dom,
                 is_internal, now, now))
            self.conn.commit()
            self.bump_edges_version()
            return True

    def record_edges(self, edges) -> int:
        """Bulk record for trusted backfills/tests and the graph benchmark.
        Shares record_edge's contract EXACTLY (BUG-09): normalization at the
        choke point, per-source-page and per-domain-pair caps (running
        counters + one DB COUNT per distinct page/pair), and recrawl upsert
        semantics that PRESERVE first_seen. One commit per 5,000 rows — a
        per-edge commit would make big crawls commit-bound. Returns rows
        written."""
        if isinstance(edges, (str, bytes)):
            raise TypeError("edges must be (from_url, to_url, anchor, rel) rows")
        now = time.time()
        # normalize + in-batch dedup: the LAST (anchor, rel) observation wins
        normalized: dict[tuple[str, str], tuple[str, str]] = {}
        for from_url, to_url, anchor_text, rel_attrs in edges:
            pair = self._norm_pair(from_url, to_url)
            if pair is None:
                continue
            normalized[pair] = (anchor_text, rel_attrs)

        written = 0
        with self.lock:
            out_counts: dict[str, int] = {}
            pair_counts: dict[tuple[str, str], int] = {}

            def _out_count(url: str) -> int:
                if url not in out_counts:
                    out_counts[url] = self.conn.execute(
                        "SELECT COUNT(*) FROM link_edges WHERE from_url = ?",
                        (url,)).fetchone()[0]
                return out_counts[url]

            def _pair_count(f_dom: str, t_dom: str) -> int:
                key = (f_dom, t_dom)
                if key not in pair_counts:
                    pair_counts[key] = self.conn.execute(
                        "SELECT COUNT(*) FROM link_edges "
                        "WHERE from_domain = ? AND to_domain = ?",
                        key).fetchone()[0]
                return pair_counts[key]

            pending = []
            for (fn, tn), (anchor, rel) in sorted(normalized.items()):
                if _out_count(fn) >= MAX_EDGES_PER_SOURCE_PAGE:
                    continue  # per-source-page cap (same rule as record_edge)
                f_dom, t_dom = _domain_of(fn), _domain_of(tn)
                if f_dom and t_dom and \
                        _pair_count(f_dom, t_dom) >= MAX_EDGES_PER_DOMAIN_PAIR:
                    continue  # per-domain-pair cap (exact-host policy)
                pending.append((fn, tn, anchor, rel, f_dom, t_dom,
                                1 if f_dom == t_dom else 0, now, now))
                out_counts[fn] = _out_count(fn) + 1
                if f_dom and t_dom:
                    pair_counts[(f_dom, t_dom)] = _pair_count(f_dom, t_dom) + 1
                if len(pending) >= 5000:
                    written += self._upsert_edges(pending)
                    pending = []
            if pending:
                written += self._upsert_edges(pending)
        if written:
            self.bump_edges_version()
        return written

    def _upsert_edges(self, rows: list[tuple]) -> int:
        """One executemany + commit. ON CONFLICT refreshes anchor/rel/
        domains/last_seen but PRESERVES first_seen — recrawl semantics,
        not the pre-BUG-09 INSERT OR REPLACE that reset edge history."""
        self.conn.executemany(
            "INSERT INTO link_edges (from_url, to_url, anchor_text, rel_attrs, "
            "from_domain, to_domain, is_internal, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(from_url, to_url) DO UPDATE SET "
            "anchor_text = excluded.anchor_text, "
            "rel_attrs = excluded.rel_attrs, "
            "from_domain = excluded.from_domain, "
            "to_domain = excluded.to_domain, "
            "is_internal = excluded.is_internal, "
            "last_seen = excluded.last_seen",
            rows)
        self.conn.commit()
        return len(rows)

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
        (BUG-02), so a caller holding a raw/variant URL still resolves.
        Served from the in-memory cache (WP5): invalidated on in-process
        recompute and on cross-process writes detected via data_version —
        rerank pays no per-candidate SQL."""
        if not url:
            return None
        try:
            url = normalize_url(url)
        except ValueError:
            return None
        with self.lock:
            self._refresh_cache_if_stale()
            hit = self._authority_cache.get(url)
            if hit is not None:
                return hit
            row = self.conn.execute(
                "SELECT authority, popularity FROM authority_scores WHERE url = ?",
                (url,)).fetchone()
            if row is not None:
                self._authority_cache[url] = (row[0], row[1])
                return (row[0], row[1])
            # unknown URL: fall back to the DOMAIN aggregate only when the
            # operator opted in (default off — a domain score is weaker
            # evidence than a page score and must never silently substitute)
            if _domain_authority_fallback_enabled():
                return self._domain_authority_for(url)
            return None

    def _domain_authority_for(self, url: str) -> Optional[tuple[float, float]]:
        domain = _domain_of(url)
        if not domain:
            return None
        row = self.conn.execute(
            "SELECT authority, popularity FROM domain_authority "
            "WHERE domain = ?", (domain,)).fetchone()
        return (row[0], row[1]) if row else None

    def stats(self) -> dict:
        with self.lock:
            return {
                "edges": self.edge_count(),
                "scored_urls": self.conn.execute(
                    "SELECT COUNT(*) FROM authority_scores").fetchone()[0],
            }

    # --------------------------------------------------------- traversal

    def neighbors(self, url: str, direction: str = "out",
                  limit: int = 100) -> list[LinkEdge]:
        """Edges touching the (normalized) URL, bounded by `limit`
        (clamped to <= 500). direction: 'out' | 'in' | 'both'."""
        if direction not in ("out", "in", "both"):
            raise ValueError("direction must be out, in, or both")
        try:
            url = normalize_url(url)
        except ValueError:
            return []
        limit = max(1, min(limit, 500))
        with self.lock:
            rows = []
            if direction in ("out", "both"):
                rows += self.conn.execute(
                    "SELECT from_url, to_url, anchor_text, rel_attrs "
                    "FROM link_edges WHERE from_url = ? "
                    "ORDER BY to_url LIMIT ?", (url, limit)).fetchall()
            if direction in ("in", "both"):
                rows += self.conn.execute(
                    "SELECT from_url, to_url, anchor_text, rel_attrs "
                    "FROM link_edges WHERE to_url = ? "
                    "ORDER BY from_url LIMIT ?", (url, limit)).fetchall()
        return [LinkEdge(*r) for r in rows[:limit]]

    def bounded_bfs(self, start: str, max_depth: int = 2,
                    max_nodes: int = 200, direction: str = "out") -> list[str]:
        """Iterative (no recursion) breadth-first traversal from `start`,
        bounded by depth and node count. Deterministic: frontier expansion
        follows sorted neighbor order. Returns visited URLs excluding start."""
        from collections import deque
        if max_depth <= 0 or max_nodes <= 0:
            return []
        try:
            origin = normalize_url(start)
        except ValueError:
            return []
        visited = {origin}
        queue = deque([(origin, 0)])
        while queue and len(visited) - 1 < max_nodes:
            url, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for edge in self.neighbors(url, direction, limit=max_nodes):
                nxt = edge.to_url if direction in ("out", "both") else edge.from_url
                if direction == "both" and nxt == url and edge.from_url != url:
                    nxt = edge.from_url
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append((nxt, depth + 1))
        return sorted(visited - {origin})

    def connected_components(self) -> tuple[dict[str, int], dict[int, int]]:
        """Undirected connected components over the edge set.

        Returns (url -> component_id, component_id -> size). Iterative
        union-find (no recursion); component ids are assigned in sorted
        first-URL order, so the mapping is deterministic across runs.
        Only URLs that appear in at least one edge are listed — isolated
        indexed pages are the orphan report's job, not the graph's."""
        parent: dict[str, str] = {}

        def find(x: str) -> str:
            root = x
            while parent[root] != root:
                root = parent[root]
            while parent[x] != root:  # path compression, iterative
                parent[x], x = root, parent[x]
            return root

        def union(a: str, b: str) -> None:
            for node in (a, b):
                parent.setdefault(node, node)
            ra, rb = find(a), find(b)
            if ra != rb:
                # deterministic: lexicographically smaller root wins
                if rb < ra:
                    ra, rb = rb, ra
                parent[rb] = ra

        with self.lock:
            edges = self.conn.execute(
                "SELECT from_url, to_url FROM link_edges "
                "ORDER BY from_url, to_url").fetchall()
        for f, t in edges:
            union(f, t)
        components: dict[str, int] = {}
        for url in sorted(parent):
            components[url] = find(url)
        id_of: dict[str, int] = {}
        sizes: dict[int, int] = {}
        for root in sorted(set(components.values())):
            id_of[root] = len(id_of)
        for url, root in components.items():
            sizes[id_of[root]] = sizes.get(id_of[root], 0) + 1
        return ({url: id_of[root] for url, root in components.items()}, sizes)

    def component_of(self, url: str) -> Optional[tuple[int, int]]:
        """(component_id, size) for a URL in the edge set, None if unknown."""
        try:
            url = normalize_url(url)
        except ValueError:
            return None
        mapping, sizes = self.connected_components()
        comp = mapping.get(url)
        return (comp, sizes[comp]) if comp is not None else None

    def internal_external_counts(self, url: str) -> dict[str, int]:
        """Per-page outbound link classification counts (is_internal is
        SAME EXACT HOST — the documented policy; see _SCHEMA_V3)."""
        try:
            url = normalize_url(url)
        except ValueError:
            return {"internal": 0, "external": 0}
        with self.lock:
            row = self.conn.execute(
                "SELECT SUM(is_internal = 1), SUM(is_internal = 0) "
                "FROM link_edges WHERE from_url = ?", (url,)).fetchone()
        return {"internal": row[0] or 0, "external": row[1] or 0}

    # ------------------------------------------------------------- anchors

    def anchor_text_for(self, url: str) -> str:
        """Aggregated inbound anchor text for a URL (rebuilt by each
        authority recompute; empty when nobody linked with text)."""
        try:
            url = normalize_url(url)
        except ValueError:
            return ""
        with self.lock:
            row = self.conn.execute(
                "SELECT anchor_text FROM url_anchors WHERE url = ?",
                (url,)).fetchone()
            return row[0] if row else ""

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
                    "rel_attrs, from_domain, to_domain, is_internal, "
                    "first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?)",
                    (fn, tn, anchor, rel, _domain_of(fn), _domain_of(tn),
                     1 if _domain_of(fn) == _domain_of(tn) else 0,
                     first_seen, last_seen))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        # the edge set changed semantically (merges/drops) — the version
        # guard must not suppress the follow-up recompute
        if groups or self_link_rows:
            graph.bump_edges_version()

    return {
        "rows_in": len(rows),
        "edges_out": len(groups),
        "merged_groups": sum(1 for m in groups.values() if len(m) > 1),
        "self_links_dropped": len(self_link_rows),
    }
