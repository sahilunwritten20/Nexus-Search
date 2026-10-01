"""WP2 / BUG-02 regression tests: link-graph node identity is normalized.

Pre-fix, the crawler recorded `to_url` raw (fragments, utm params, trailing
slash, host case, default ports) while page URLs were frontier-normalized —
`authority_for` (exact match) missed real scores and PageRank mass fragmented
across URL variants of one logical page.

These tests pin the fixed contract:
- record_edge/record_edges normalize BOTH endpoints (no caller can bypass)
- self-links are detected AFTER normalization (a fragment self-link counts)
- authority_for normalizes the lookup URL
- the ranking signal path resolves variant metadata URLs to real scores
- the `normalize-links` CLI re-normalizes legacy rows, merges duplicates,
  and is idempotent (running twice is a no-op)
"""
import importlib
import os
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.links.authority import compute_authority  # noqa: E402
from nexus_search.links.graph import LinkGraph, normalize_existing_edges  # noqa: E402


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


class TestNormalizedNodeIdentity(_GraphTest):
    def test_url_variants_collapse_to_one_target_node(self):
        """Six distinct-domain sources link to six URL VARIANTS of the same
        page. Edges are per-source (6 of them), but every to_url must
        normalize to ONE node — pre-fix these were 6 fragment target nodes,
        splitting PageRank mass (BUG-02)."""
        variants = [
            "http://hub.com/page#section",       # fragment
            "http://hub.com/page?utm_source=x",   # tracking param
            "http://hub.com/page/",              # trailing slash
            "http://HUB.com/page",               # host case
            "http://hub.com:80/page",             # default port
            "http://hub.com/p%61ge",              # unreserved percent-escape
        ]
        for i, tgt in enumerate(variants):
            self.assertTrue(self.graph.record_edge(f"http://src{i}.com", tgt))
        edges = self.graph.edges()
        self.assertEqual(len(edges), 6)  # one edge per (source, target) pair
        self.assertEqual({e.to_url for e in edges}, {"http://hub.com/page"},
                         "every variant must collapse to the ONE target node")
        compute_authority(self.graph)
        # the merged node sees ALL six inlinks — no fragmented mass
        row = self.graph.conn.execute(
            "SELECT inlink_count FROM authority_scores WHERE url = ?",
            ("http://hub.com/page",)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 6)

    def test_self_link_after_normalization_rejected(self):
        # the fragment differs pre-normalization; post-normalization it is a
        # self-vote and must be dropped (record_edge returns False)
        self.assertFalse(self.graph.record_edge("http://a.com/x",
                                                "http://a.com/x#frag"))

    def test_variant_lookup_finds_authority(self):
        # four distinct-domain inlinks, each written with a variant target URL
        for i in range(4):
            self.graph.record_edge(f"http://d{i}.com",
                                   f"http://hub.com/page{'/' if i == 1 else ''}")
        compute_authority(self.graph)
        for probe in ("http://hub.com/page", "http://hub.com/page#top",
                      "http://hub.com/page?utm_campaign=zz"):
            found = self.graph.authority_for(probe)
            self.assertIsNotNone(found, f"authority_for({probe!r}) must resolve")
            self.assertGreater(found[0], 0.0)

    def test_ranking_signal_resolves_variant_url(self):
        """The ranker's _link_scores path uses metadata['url'] verbatim —
        with normalization inside authority_for, a variant metadata URL still
        finds the score (this was the exact production miss)."""
        from nexus_search.ranking.features import build_context, extract_features
        from nexus_search.ranking.signals import NEUTRAL
        from nexus_search.core.storage import Storage
        from nexus_search.core.indexer import Indexer

        storage = Storage(self.db)
        try:
            Indexer(storage).add_document(
                "hub", "hub content", title="Hub", doc_type="web",
                metadata={"url": "http://hub.com/page?utm_source=twitter"})
            for i in range(4):
                self.graph.record_edge(f"http://d{i}.com", "http://hub.com/page")
            compute_authority(self.graph)
            ctx = build_context("hub content", link_intel=self.graph)
            feats = extract_features("hub", storage, ctx,
                                      doc=storage.get_document("hub"))
            self.assertNotEqual(feats.source_authority, NEUTRAL,
                                "variant metadata URL must resolve to a graph score")
        finally:
            storage.close()

    def test_bulk_record_edges_normalized_too(self):
        edges = [
            ("http://b1.com", "http://t.com/x#frag", "a", ""),
            ("http://b2.com", "http://t.com/x?utm_term=y", "b", ""),
        ]
        self.graph.record_edges(edges)
        got = self.graph.edges()
        self.assertEqual(len(got), 2)  # distinct sources -> distinct edges
        self.assertEqual({e.to_url for e in got}, {"http://t.com/x"},
                         "bulk path cannot bypass identity normalization")


class TestNormalizeCli(_GraphTest):
    """The idempotent data migration for pre-fix rows (SQL-only migrations
    cannot express URL canonicalization; this CLI is the migration tool)."""

    def _insert_raw_rows(self):
        """Simulate a pre-fix (BUG-02) database: raw, unnormalized URLs."""
        now = time.time()
        rows = [
            # one logical edge a->b recorded three times with variants
            ("http://a.com/p1", "http://b.com/target#frag", "anchor one",
             "", now - 300, now - 300),
            ("http://a.com/p1", "http://b.com/target?utm_source=x", "",
             "nofollow", now - 200, now - 100),   # latest, non-empty rel
            ("http://a.com/p1", "http://b.com/target/", "anchor three",
             "", now - 250, now - 50),            # latest last_seen
            # a fragment self-link that pre-normalization slipped through
            ("http://c.com/x", "http://c.com/x#me", "self", "", now - 10, now - 10),
            # a clean edge that must survive untouched
            ("http://d.com", "http://e.com/page", "clean", "", now - 5, now - 5),
        ]
        with self.graph.lock:
            self.graph.conn.executemany(
                "INSERT INTO link_edges (from_url, to_url, anchor_text, "
                "rel_attrs, first_seen, last_seen) VALUES (?,?,?,?,?,?)", rows)
            self.graph.conn.commit()

    def test_merges_variants_and_preserves_history(self):
        self._insert_raw_rows()
        stats = normalize_existing_edges(self.graph)
        self.assertEqual(stats["rows_in"], 5)
        self.assertEqual(stats["self_links_dropped"], 1)
        self.assertEqual(stats["edges_out"], 2)  # a->b (merged) + d->e
        edges = {e.from_url: e for e in self.graph.edges()}
        merged = edges["http://a.com/p1"]
        self.assertEqual(merged.to_url, "http://b.com/target")
        self.assertEqual(merged.anchor_text, "anchor three")  # latest, non-empty
        self.assertEqual(merged.rel_attrs, "")                # from latest row
        # first_seen/last_seen live in the table, not the LinkEdge projection
        row = self.graph.conn.execute(
            "SELECT first_seen, last_seen FROM link_edges "
            "WHERE from_url = ? AND to_url = ?",
            ("http://a.com/p1", "http://b.com/target")).fetchone()
        self.assertIsNotNone(row)
        self.assertLessEqual(row[0], time.time() - 299)   # earliest of the three
        self.assertGreaterEqual(row[1], time.time() - 51)  # latest of the three

    def test_running_twice_is_a_noop(self):
        self._insert_raw_rows()
        first = normalize_existing_edges(self.graph)
        snapshot = sorted((e.from_url, e.to_url, e.anchor_text, e.rel_attrs)
                          for e in self.graph.edges())
        second = normalize_existing_edges(self.graph)
        after = sorted((e.from_url, e.to_url, e.anchor_text, e.rel_attrs)
                       for e in self.graph.edges())
        self.assertEqual(snapshot, after)
        # second pass: every row is already normalized -> nothing to merge
        self.assertEqual(second["rows_in"], first["edges_out"])
        self.assertEqual(second["merged_groups"], 0)
        self.assertEqual(second["self_links_dropped"], 0)

    def test_recompute_after_normalize_flows_into_scores(self):
        self._insert_raw_rows()
        normalize_existing_edges(self.graph)
        compute_authority(self.graph)
        found = self.graph.authority_for("http://b.com/target")
        self.assertIsNotNone(found)
        self.assertGreater(found[0], 0.0)


class _VariantHandler(BaseHTTPRequestHandler):
    """Page /linker contains three URL variants pointing at the SAME target
    page /target. Pre-fix the graph stored three edges; post-fix: one."""

    def do_GET(self):
        if self.path == "/robots.txt":
            body = b"User-agent: *\nAllow: /\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/linker":
            body = (b"<html><head><title>Linker</title></head><body>"
                    b'<a href="/target#top">one</a>'
                    b'<a href="/target?utm_source=test">two</a>'
                    b'<a href="/target/">three</a>'
                    b"</body></html>")
        elif self.path == "/target":
            body = (b"<html><head><title>Target</title></head>"
                    b"<body>the target page</body></html>")
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class TestEndToEndVariantCrawl(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _VariantHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "e2e.db")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for _ in range(10):
            try:
                shutil.rmtree(self.dir)
                break
            except PermissionError:
                time.sleep(0.05)

    def test_variant_links_collapse_in_live_crawl(self):
        from nexus_search.crawler.pipeline import CrawlPipeline

        graph = LinkGraph(self.db)
        pipeline = CrawlPipeline(
            db_path=os.path.join(self.dir, "frontier.db"),
            allowed_domains=["127.0.0.1"],
            max_pages=5, max_depth=1, concurrency=1,
            ingest_fn=lambda *a: None,
            allow_private_hosts=True,
            link_graph=graph,
        )
        try:
            pipeline.seed([f"{self.base}/linker"])
            stats = pipeline.run()
            self.assertGreaterEqual(stats["crawled"], 1)
            target_edges = [e for e in graph.edges()
                            if e.to_url.endswith("/target")]
            self.assertEqual(len(target_edges), 1,
                             "three variant links must record ONE edge")
            self.assertEqual(target_edges[0].to_url, f"{self.base}/target")
            compute_authority(graph)
            found = graph.authority_for(f"{self.base}/target")
            self.assertIsNotNone(found, "authority must resolve for the target")
            self.assertGreater(found[0], 0.0,
                               "merged inlink mass must be non-zero")
        finally:
            graph.close()


if __name__ == "__main__":
    unittest.main()
