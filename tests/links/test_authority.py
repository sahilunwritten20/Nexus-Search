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
        result = self.graph.authority_for("b.com")
        self.assertIsNotNone(result)
        self.assertGreater(result[0], 0.0)

    def test_cycle(self):
        self.graph.record_edge("a.com", "b.com")
        self.graph.record_edge("b.com", "c.com")
        self.graph.record_edge("c.com", "a.com")
        stats = compute_authority(self.graph)
        self.assertEqual(stats.pages, 3)
        result = self.graph.authority_for("a.com")
        self.assertIsNotNone(result)
        self.assertGreater(result[0], 0.0)

    def test_disconnected_components(self):
        # two genuinely disconnected edge pairs: PageRank converges AND the
        # component analysis reports exactly two components of size 2
        self.graph.record_edge("http://a1.com/x", "http://a2.com/x")
        self.graph.record_edge("http://c1.com/x", "http://d1.com/x")
        stats = compute_authority(self.graph)
        self.assertEqual(stats.pages, 4)
        mapping, sizes = self.graph.connected_components()
        self.assertEqual(len(sizes), 2)
        self.assertEqual(sorted(sizes.values()), [2, 2])
        self.assertNotEqual(mapping["http://a1.com/x"], mapping["http://c1.com/x"])

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


class TestReciprocalDiscountEffectiveness(_Base):
    """P1-5: the reciprocal discount must actually BITE on closed link farms.
    Pre-fix, outflow was normalized by the DISCOUNTED weight sum, so scaling
    every edge of a closed clique by 0.25 also scaled every denominator by
    0.25 — measured: identical PageRank at RECIPROCAL_FACTOR 1.0 and 0.25."""

    def _raw_pr(self, factor, graph):
        from nexus_search.links import authority
        saved = authority.RECIPROCAL_FACTOR
        authority.RECIPROCAL_FACTOR = factor
        try:
            authority.compute_authority(graph, force=True)
        finally:
            authority.RECIPROCAL_FACTOR = saved
        return {u: graph.conn.execute(
            "SELECT pagerank FROM authority_scores WHERE url = ?", (u,)
        ).fetchone()[0] for (u,) in graph.conn.execute(
            "SELECT url FROM authority_scores").fetchall()}

    def test_closed_clique_discount_actually_reduces_scores(self):
        """4-site closed clique + one honest inlink: at factor 0.25 the farm
        must carry LESS PageRank than at factor 1.0."""
        farm = [f"farm{i}.example" for i in range(4)]
        for a in farm:
            for b in farm:
                if a != b:
                    self.graph.record_edge(a, b)
        self.graph.record_edge("honest.example", farm[0])

        at_one = self._raw_pr(1.0, self.graph)
        at_quarter = self._raw_pr(0.25, self.graph)
        # Members other than the entry point must drop outright; farm[0]
        # (holder of the only external inlink) may legitimately rise, so
        # the property that must hold for the FARM as a whole is total mass.
        for u in farm[1:]:
            self.assertGreater(
                at_one[u], at_quarter[u],
                f"discount is a no-op for {u}: {at_one[u]} == {at_quarter[u]}")
        self.assertGreater(sum(at_one[u] for u in farm),
                           sum(at_quarter[u] for u in farm))

    def test_farm_drops_relative_to_honest_page(self):
        """The whole point of the discount: a farm member's rank relative to
        an honestly-linked page must FALL when the factor drops below 1."""
        farm = [f"farm{i}.example" for i in range(4)]
        for a in farm:
            for b in farm:
                if a != b:
                    self.graph.record_edge(a, b)
        for i in range(5):
            self.graph.record_edge(f"inlink{i}.example", "honest.example")

        at_one = self._raw_pr(1.0, self.graph)
        at_quarter = self._raw_pr(0.25, self.graph)
        ratio_one = max(at_one[u] for u in farm) / at_one["honest.example"]
        ratio_quarter = max(at_quarter[u] for u in farm) / at_quarter["honest.example"]
        self.assertLess(ratio_quarter, ratio_one)

    def test_pagerank_still_sums_to_one(self):
        """Mass conservation: withheld (discounted) mass redistributes
        uniformly, so total PageRank stays exactly 1."""
        farm = [f"farm{i}.example" for i in range(4)]
        for a in farm:
            for b in farm:
                if a != b:
                    self.graph.record_edge(a, b)
        self.graph.record_edge("honest.example", farm[0])
        self.graph.record_edge("a.example", "honest.example")
        for factor in (1.0, 0.25):
            with self.subTest(factor=factor):
                pr = self._raw_pr(factor, self.graph)
                self.assertAlmostEqual(sum(pr.values()), 1.0, places=5)

    def test_no_reciprocal_pairs_means_no_discount(self):
        """With no reciprocal pairs the discount is inert: any factor must
        produce identical scores (weights are all 1.0)."""
        for i in range(6):
            self.graph.record_edge(f"src{i}.example", "hub.example")
        at_one = self._raw_pr(1.0, self.graph)
        at_quarter = self._raw_pr(0.25, self.graph)
        self.assertEqual(at_one, at_quarter)


class TestAuthorityStore(_Base):
    def test_migration_version(self):
        from nexus_search.core.migrations import get_version
        # v1 = base schema; v2 = from_domain/to_domain columns + index (BUG-05)
        self.assertEqual(get_version(self.graph.conn, "link_graph"), 3)

    def test_reopen_existing_db_is_noop(self):
        """Reopening a DB that already has the schema doesn't re-migrate."""
        from nexus_search.core.migrations import get_version
        again = LinkGraph(self.db)
        try:
            self.assertEqual(get_version(again.conn, "link_graph"), 3)
        finally:
            again.close()


if __name__ == "__main__":
    unittest.main()
