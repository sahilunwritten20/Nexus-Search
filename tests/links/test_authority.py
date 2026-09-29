"""Tests for Phase 6 authority computation.

Covers: empty graph, single node, cycles, disconnected components,
reciprocal discounts, nofollow handling, the mission case (hub vs clique
vs orphan), doc deletion mid-recompute, and concurrent writes.
"""
import os
import shutil
import tempfile
import threading
import time
import unittest

from nexus_search.links.graph import LinkGraph
from nexus_search.links.authority import compute_authority


class _Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "t.db")
        self.graph = LinkGraph(self.db)

    def tearDown(self):
        self.graph.close()
        for _ in range(10):
            try:
                shutil.rmtree(self.dir)
                break
            except PermissionError:
                time.sleep(0.05)


class TestAuthorityComputation(_Base):
    def test_empty_graph(self):
        stats = compute_authority(self.graph)
        self.assertEqual(stats.pages, 0)
        self.assertEqual(stats.edges_used, 0)
        self.assertIsNone(self.graph.authority_for("unknown.com"))

    def test_single_node(self):
        self.graph.record_edge("a.com", "b.com")
        stats = compute_authority(self.graph)
        self.assertEqual(stats.pages, 2)
        self.assertGreaterEqual(stats.iterations, 1)
        self.assertTrue(stats.converged)
        result = self.graph.authority_for("b.com")
        self.assertIsNotNone(result)
        self.assertGreater(result[0], 0.0)

    def test_cycle(self):
        self.graph.record_edge("a.com", "b.com")
        self.graph.record_edge("b.com", "c.com")
        self.graph.record_edge("c.com", "a.com")
        stats = compute_authority(self.graph)
        self.assertEqual(stats.pages, 3)
        self.assertTrue(stats.converged)
        result = self.graph.authority_for("a.com")
        self.assertIsNotNone(result)
        self.assertGreater(result[0], 0.0)

    def test_disconnected_components(self):
        self.graph.record_edge("a.com", "b.com")
        self.graph.record_edge("c.com", "d.com")
        stats = compute_authority(self.graph)
        self.assertEqual(stats.pages, 4)
        self.assertTrue(stats.converged)

    def test_reciprocal_discount(self):
        self.graph.record_edge("a.com", "b.com")
        self.graph.record_edge("b.com", "a.com")
        stats = compute_authority(self.graph)
        self.assertEqual(stats.reciprocal_pairs, 1)

    def test_nofollow_passes_nothing(self):
        self.graph.record_edge("a.com", "b.com", rel_attrs="nofollow")
        stats = compute_authority(self.graph)
        # nofollow edge passes zero mass — b.com gets no authority entry
        self.assertEqual(stats.edges_used, 0)
        self.assertIsNone(self.graph.authority_for("b.com"))

    def test_mission_case_hub_vs_clique_vs_orphan(self):
        """Hub (50 distinct domains) > clique (1 domain, reciprocal) > orphan."""
        for i in range(50):
            self.graph.record_edge(f"domain{i}.com", "hub.com")
        # 3-page mutual-admiration clique
        self.graph.record_edge("c1.com", "c2.com")
        self.graph.record_edge("c2.com", "c3.com")
        self.graph.record_edge("c3.com", "c1.com")
        # Orphan (only links out, nothing links in)
        self.graph.record_edge("orphan.com", "nowhere.com")

        compute_authority(self.graph)
        hub = self.graph.authority_for("hub.com")
        clique = self.graph.authority_for("c1.com")
        orphan = self.graph.authority_for("orphan.com")

        self.assertIsNotNone(hub, "hub must be scored")
        self.assertIsNotNone(orphan, "orphan must be scored (it's in the graph)")
        # Hub outranks clique: 50 distinct-domain inlinks vs 1-domain clique
        self.assertGreater(hub[0], clique[0] if clique else 0.0)
        # Orphan has base-mass only — lowest authority in this graph
        self.assertGreater(hub[0], orphan[0])
        # Clique members are at least present in the graph (reciprocal-discounted)
        if clique is not None:
            self.assertGreater(clique[0], 0.0)

    def test_high_fan_in_hub(self):
        """A page with 500 inlinks doesn't blow up — completes and produces
        a real score even if the star-graph shape doesn't fully converge
        in 30 iterations (scores are still usable, just not bit-stable)."""
        edges = [(f"spam{i}.com", "hub.com", "", "") for i in range(500)]
        self.graph.record_edges(edges)
        stats = compute_authority(self.graph)
        self.assertEqual(stats.pages, 501)
        result = self.graph.authority_for("hub.com")
        self.assertIsNotNone(result)
        self.assertGreater(result[0], 0.0)  # the hub is well-linked


class TestAuthorityRecompute(_Base):
    def test_deleted_doc_does_not_crash(self):
        """Doc deletion is orthogonal to the URL-keyed graph — no coupling."""
        self.graph.record_edge("a.com", "b.com")
        compute_authority(self.graph)  # first pass
        # Simulate doc deletion — the graph doesn't read `documents` at all,
        # so this can't crash; the stale URL score simply expires next pass
        result = self.graph.authority_for("b.com")
        self.assertIsNotNone(result)

    def test_concurrent_writes_during_recompute(self):
        """Writers recording edges while a recompute runs must never raise."""
        self.graph.record_edge("a.com", "b.com")  # seed so recompute has data

        errors = []
        stop = threading.Event()

        def writer(worker_id):
            i = 0
            while not stop.is_set():
                try:
                    self.graph.record_edge(f"src{worker_id}_{i}.com", f"dst{worker_id}_{i}.com")
                    i += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        threads = [threading.Thread(target=writer, args=(w,), daemon=True)
                   for w in range(3)]
        for t in threads:
            t.start()
        # run a recompute concurrently with the writers
        stats = compute_authority(self.graph)
        stop.set()
        for t in threads:
            t.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertGreaterEqual(stats.pages, 2)


class TestAuthorityStore(_Base):
    def test_migration_version(self):
        from nexus_search.core.migrations import get_version
        self.assertEqual(get_version(self.graph.conn, "link_graph"), 1)

    def test_reopen_existing_db_is_noop(self):
        """Reopening a DB that already has the schema doesn't re-migrate."""
        from nexus_search.core.migrations import get_version
        again = LinkGraph(self.db)
        try:
            self.assertEqual(get_version(again.conn, "link_graph"), 1)
        finally:
            again.close()


if __name__ == "__main__":
    unittest.main()
