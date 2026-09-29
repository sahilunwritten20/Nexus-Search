"""Tests for Phase 6 link graph store."""
import os
import shutil
import tempfile
import threading
import time
import unittest

from nexus_search.links.graph import LinkGraph, MAX_EDGES_PER_SOURCE_PAGE, MAX_EDGES_PER_DOMAIN_PAIR


class TestLinkGraphStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "graph.db")
        self.graph = LinkGraph(self.db)

    def tearDown(self):
        self.graph.close()
        for _ in range(10):
            try:
                shutil.rmtree(self.dir)
                break
            except PermissionError:
                time.sleep(0.05)

    def test_record_and_dedup(self):
        self.assertTrue(self.graph.record_edge("a.com", "b.com", "click here", "nofollow"))
        # Recrawl of same edge updates metadata and returns True
        self.assertTrue(self.graph.record_edge("a.com", "b.com", "diff anchor", "sponsored"))
        edges = self.graph.edges()
        self.assertEqual(len(self.graph.edges()), 1)
        # The last write wins for anchor/rel
        self.assertEqual(self.graph.edges()[0].anchor_text, "diff anchor")
        self.assertEqual(self.graph.edges()[0].rel_attrs, "sponsored")

    def test_self_link_rejected(self):
        self.assertFalse(self.graph.record_edge("a.com", "a.com", "self", ""))

    def test_per_source_cap(self):
        for i in range(MAX_EDGES_PER_SOURCE_PAGE):
            self.assertTrue(self.graph.record_edge(f"src.com", f"t{i}.com"))
        self.assertFalse(self.graph.record_edge("src.com", "overflow.com"))

    def test_per_domain_pair_cap(self):
        # All edges from same source to SAME target domain (different paths)
        # to hit the domain-pair cap (500 edges per domain pair)
        for i in range(MAX_EDGES_PER_DOMAIN_PAIR):
            self.assertTrue(self.graph.record_edge("https://src.com/path", f"https://target.com/page{i}", f"anchor {i}", "rel"))
        # 501st edge to same domain pair should be refused
        self.assertFalse(self.graph.record_edge("https://src.com/page", "https://target.com/overflow", "overflow", "rel"))

    def test_self_link_rejected(self):
        self.assertFalse(self.graph.record_edge("a.com", "a.com", "self", ""))

    def test_migration_version(self):
        from nexus_search.core.migrations import get_version
        self.assertEqual(get_version(self.graph.conn, "link_graph"), 1)

    def test_unknown_url_returns_none(self):
        self.assertIsNone(self.graph.authority_for("unknown.com"))

    def test_record_edges_bulk(self):
        edges = [(f"src{i}.com", f"tgt{j}.com", "", "") for i in range(3) for j in range(2)]
        count = self.graph.record_edges(edges)
        self.assertEqual(count, 6)
        self.assertEqual(self.graph.edge_count(), 6)

    def test_concurrent_writes(self):
        errors = []

        def worker(n):
            try:
                for i in range(50):
                    self.graph.record_edge(f"src{n}.com", f"dst{n}_{i}.com")
            except Exception as exc:
                # noqa: BLE001
                raise

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.graph.edge_count(), 400)


if __name__ == "__main__":
    unittest.main()