"""Differential test: the owned PageRank vs networkx.pagerank (mission B6).

The system's implementation (links/authority.py) reduces to STANDARD
PageRank — damping 0.85, uniform dangling redistribution — on graphs
with NO reciprocal link pairs (RECIPROCAL_FACTOR only fires on mutual
edges; with all weights 1.0 the withheld-mass term is identically zero).
On such graphs the per-node scores must match networkx to <5e-5, the
mass must sum to 1, and the pass must report convergence.

The intentional deviation (reciprocal discount) is pinned separately:
on a graph WITH mutual edges, factor 0.25 must produce different mass
flow than factor 1.0 (proving the anti-farm discount is active).
"""
import os
import random
import tempfile
import unittest

import networkx as nx

from nexus_search.links.authority import RECIPROCAL_FACTOR, compute_authority
from nexus_search.links.graph import LinkGraph


def _system_pagerank(edges):
    """Run compute_authority on the edge list; return {url: pagerank}."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    graph = LinkGraph(path)
    try:
        for a, b in edges:
            graph.record_edge(a, b)
        stats = compute_authority(graph, force=True)
        rows = graph.conn.execute(
            "SELECT url, pagerank FROM authority_scores").fetchall()
        return {url: pr for url, pr in rows}, stats
    finally:
        graph.close()
        os.remove(path)


class TestPageRankDifferential(unittest.TestCase):
    TOL = 5e-5

    def _assert_matches_networkx(self, edges):
        system, stats = _system_pagerank(edges)
        self.assertTrue(stats.converged,
                         "test graphs must converge within the iteration cap")
        self.assertEqual(stats.pages, len({u for e in edges for u in e}))
        g = nx.DiGraph()
        g.add_edges_from(edges)
        expected = nx.pagerank(g, alpha=0.85)
        self.assertEqual(set(system), set(expected))
        for url in expected:
            self.assertAlmostEqual(system[url], expected[url], delta=self.TOL,
                                    msg=f"pagerank mismatch at {url}")
        # mass conservation: PageRank is a probability distribution
        self.assertAlmostEqual(sum(system.values()), 1.0, places=5)

    def test_chain(self):
        self._assert_matches_networkx([
            (f"http://n{i}.com/p", f"http://n{i+1}.com/p") for i in range(6)
        ])

    def test_star(self):
        edges = [(f"http://hub{i}.com/l", "http://center.com/p") for i in range(8)]
        self._assert_matches_networkx(edges)

    def test_cycle(self):
        n = 10
        edges = [(f"http://n{i%10}.com/p", f"http://n{(i+1)%10}.com/p")
                 for i in range(n)]
        self._assert_matches_networkx(edges)

    def test_disconnected_components(self):
        # three separate chains; two of them end in dangling nodes
        edges = [("http://a1.com/p", "http://a2.com/p"),
                  ("http://a2.com/p", "http://a3.com/p"),
                  ("http://b1.com/p", "http://b2.com/p"),
                  ("http://b2.com/p", "http://b3.com/p"),
                  ("http://c1.com/p", "http://c2.com/p")]
        self._assert_matches_networkx(edges)

    def test_dangling_heavy(self):
        # most nodes have no out-links: dangling mass must redistribute
        # uniformly (networkx default) and match
        edges = [(f"http://src.com/p{i}", f"http://sink{i%4}.com/p")
                 for i in range(12)]
        self._assert_matches_networkx(edges)

    def test_dense_random_graph(self):
        rng = random.Random(42)
        nodes = [f"http://r{i}.com/p" for i in range(15)]
        edges = set()
        while len(edges) < 40:
            a, b = rng.sample(nodes, 2)
            if (b, a) in edges:
                continue  # keep the graph reciprocal-free for the vanilla diff
            edges.add((a, b))
        self._assert_matches_networkx(sorted(edges))

    def test_seeded_random_graphs(self):
        for seed in range(8):
            rng = random.Random(seed)
            n = rng.randint(5, 12)
            nodes = [f"http://s{seed}n{i}.com/p" for i in range(n)]
            edges = set()
            for _ in range(rng.randint(n, 2 * n)):
                a, b = rng.sample(nodes, 2)
                if (b, a) in edges:
                    continue
                edges.add((a, b))
            if not edges:
                continue
            self._assert_matches_networkx(sorted(edges))

    def test_reciprocal_discount_actually_deviates_from_vanilla(self):
        """The anti-farm semantics must be ACTIVE: on a graph with mutual
        edges, factor 0.25 must distribute mass differently than 1.0."""
        from unittest import mock
        import nexus_search.links.authority as authority_mod
        edges = [("http://x.com/a", "http://y.com/b"),
                 ("http://y.com/b", "http://x.com/a"),   # mutual pair
                 ("http://y.com/b", "http://z.com/c")]    # asymmetric extra
        discounted, _ = _system_pagerank(edges)
        with mock.patch.object(authority_mod, "RECIPROCAL_FACTOR", 1.0):
            undisc, _ = _system_pagerank(edges)
        self.assertNotEqual(discounted, undisc,
                            "reciprocal discount has no effect — the "
                            "anti-farm correction is inert")
        # sanity on the pin: the module constant is the shipped value
        self.assertEqual(RECIPROCAL_FACTOR, 0.25)


if __name__ == "__main__":
    unittest.main()
