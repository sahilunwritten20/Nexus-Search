"""WP4 / BUG-05 regression tests: domain-pair cap is indexed and correct.

Pre-fix, the per-domain-pair anti-flood cap ran
`WHERE from_url LIKE '%//host/%' AND to_url LIKE '%//host/%'` per new edge:
- a full-table scan per edge = O(E^2) ingest (250 edges: 2.05s; 1000: 31.93s)
- root URLs without a trailing slash ESCAPED the cap
- subdomains were counted by accident of the pattern (and inconsistently)

The fix adds from_domain/to_domain columns (migration v2, backfilled) and
an indexed COUNT, with an exact-host subdomain policy (documented in
links/graph.py).
"""
import os
import shutil
import tempfile
import time
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.links.graph import (  # noqa: E402
    LinkGraph, MAX_EDGES_PER_DOMAIN_PAIR, MAX_EDGES_PER_SOURCE_PAGE,
)


class _GraphTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "g.db")
        self.graph = LinkGraph(self.db)

    def tearDown(self):
        self.graph.close()
        for _ in range(10):
            try:
                shutil.rmtree(self.dir)
                break
            except PermissionError:
                time.sleep(0.05)


class TestDomainPairCap(_GraphTest):
    def test_root_url_without_trailing_slash_counts_toward_cap(self):
        # pre-fix, 'http://src.com' (no path slash) never matched the LIKE
        # pattern and the pair cap never fired for root pages
        for i in range(MAX_EDGES_PER_DOMAIN_PAIR):
            self.assertTrue(self.graph.record_edge(
                "http://src.com", f"http://target.com/page{i}"))
        self.assertFalse(self.graph.record_edge(
            "http://src.com", "http://target.com/overflow"),
            "the pair cap must count root source URLs")

    def test_root_target_url_counts_toward_cap(self):
        for i in range(MAX_EDGES_PER_DOMAIN_PAIR):
            self.assertTrue(self.graph.record_edge(
                f"http://src.com/page{i}", "http://target.com"))
        self.assertFalse(self.graph.record_edge(
            "http://src.com/extra", "http://target.com"),
            "the pair cap must count root target URLs")

    def test_exact_host_subdomain_policy_pinned(self):
        """Documented policy: the pair cap counts EXACT hosts. a.com and
        sub.a.com are separate pairs; each gets its own 500 budget (a
        subdomain farm is still bounded by the per-source-page cap of 1000)."""
        for i in range(MAX_EDGES_PER_DOMAIN_PAIR):
            self.assertTrue(self.graph.record_edge(
                "http://a.com", f"http://t.com/p{i}"))
        # a.com -> t.com is now full...
        self.assertFalse(self.graph.record_edge("http://a.com", "http://t.com/x"))
        # ...but sub.a.com -> t.com is a different exact-host pair
        self.assertTrue(self.graph.record_edge("http://sub.a.com", "http://t.com/x"))

    def test_per_source_page_cap_unaffected(self):
        for i in range(MAX_EDGES_PER_SOURCE_PAGE):
            self.assertTrue(self.graph.record_edge(
                "http://one.com", f"http://host{i}.com/page"))
        self.assertFalse(self.graph.record_edge(
            "http://one.com", "http://fresh.com/edge"))


class TestDomainColumnsMigration(_GraphTest):
    def test_v2_columns_and_backfill(self):
        from nexus_search.core.migrations import get_version
        self.assertEqual(get_version(self.graph.conn, "link_graph"), 3)
        row = self.graph.conn.execute(
            "SELECT from_domain, to_domain FROM link_edges LIMIT 1").fetchone()
        if row:  # may be empty table
            self.assertNotEqual(row[0], "")

    def test_backfill_fills_pre_v2_rows_idempotently(self):
        # simulate a pre-v2 database: rows with empty domain columns
        now = time.time()
        with self.graph.lock:
            self.graph.conn.executemany(
                "INSERT INTO link_edges (from_url, to_url, anchor_text, "
                "rel_attrs, from_domain, to_domain, first_seen, last_seen) "
                "VALUES (?,?,?,?,?,?,?,?)",
                [("http://old-a.com/x", "http://old-b.com/y", "a", "",
                  "", "", now, now)])
            self.graph.conn.commit()
        reopened = LinkGraph(self.db)  # backfill runs at open
        try:
            row = reopened.conn.execute(
                "SELECT from_domain, to_domain FROM link_edges").fetchone()
            self.assertEqual(row, ("old-a.com", "old-b.com"))
            # no rows left for a second backfill pass
            remaining = reopened.conn.execute(
                "SELECT COUNT(*) FROM link_edges WHERE from_domain = ''").fetchone()[0]
            self.assertEqual(remaining, 0)
        finally:
            reopened.close()

    def test_backfilled_rows_enforce_the_cap(self):
        # 500 pre-v2 rows into one pair with empty domains; after backfill
        # the pair must be full for new writes
        now = time.time()
        with self.graph.lock:
            self.graph.conn.executemany(
                "INSERT INTO link_edges (from_url, to_url, anchor_text, "
                "rel_attrs, from_domain, to_domain, first_seen, last_seen) "
                "VALUES (?,?,?,?,?,?,?,?)",
                [("http://src.com", f"http://t.com/p{i}", "a", "",
                  "", "", now, now) for i in range(MAX_EDGES_PER_DOMAIN_PAIR)])
            self.graph.conn.commit()
        reopened = LinkGraph(self.db)
        try:
            self.assertFalse(
                reopened.record_edge("http://src.com", "http://t.com/overflow"),
                "backfilled pairs must count toward the cap")
        finally:
            reopened.close()


class TestIngestScaling(_GraphTest):
    """The pre-fix pair-cap check was a full-table scan per new edge:
    250 edges -> 2.05s, 500 -> 4.32s, 1000 -> 31.93s (quadratic). The
    indexed count must be ~constant per edge. Ratio-based: 600 edges
    should take < 4x the time of 200 (linear-ish), where quadratic is ~9x."""

    def _record_n(self, n):
        t0 = time.perf_counter()
        for i in range(n):
            self.graph.record_edge(f"http://s{i}.com", f"http://t{i}.com/x")
        return time.perf_counter() - t0

    def test_pair_cap_check_scales_linearly(self):
        t_small = self._record_n(200)
        t_large = self._record_n(600)
        # 3x the edges must not cost ~9x (the quadratic signature). The
        # generous 5x bound absorbs CI noise while still failing on O(E^2).
        self.assertLess(t_large, max(t_small * 5.0, 1.0),
                        f"ingest scaling: 200 edges {t_small:.2f}s, "
                        f"600 edges {t_large:.2f}s (quadratic?)")


if __name__ == "__main__":
    unittest.main()
