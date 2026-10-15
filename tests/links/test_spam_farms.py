"""Adversarial link-farm benchmark (mission B6): measure how much each
farm shape manipulates authority, and quantify the KNOWN exact-hostname
domain-diversity limitation with data.

Farm shapes (ground truth: a "good" page with genuinely diverse inlinks
should outrank farm-manufactured authority):
  ring8        — 8-site reciprocal ring all linking the target
  wheel        — 10 spoke sites linking the target, spokes interlinked
  hub-spoke    — 1 hub with 20 one-way spokes linking the target
  subdomain10  — 10 SUBDOMAINS of one registrable domain (farm.test)
                 linking the target — the documented hole: exact-hostname
                 counting credits each subdomain as a distinct "domain",
                 so min(domains/10, 1) saturates exactly like true diversity
  diverse10    — 10 genuinely distinct registrable domains linking the
                 target (the legitimate control)

DECISION RECORD (data-driven, per the mission): the subdomain10 farm
earns the SAME diversity credit as diverse10 — measured below. Closing
that requires registrable-domain (public-suffix-list) counting, which
this codebase deliberately does not depend on (a hand-rolled suffix
table is wrong for exotic ccTLDs — the classic trap). DECISION: ACCEPT
the limitation, with this benchmark quantifying exposure, plus the
existing pair-cap (500 edges/domain-pair, exact host) as the bound; the
REMOVAL TRIGGER is adopting a maintained PSL (or moving diversity
counting to the graph layer where a PSL dep is reviewable).
"""
import os
import tempfile
import unittest

from nexus_search.links.authority import compute_authority
from nexus_search.links.graph import LinkGraph


def _authority_of(edges, url):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    graph = LinkGraph(path)
    try:
        for a, b in edges:
            graph.record_edge(a, b)
        compute_authority(graph, force=True)
        row = graph.conn.execute(
            "SELECT authority, popularity FROM authority_scores "
            "WHERE url = ?", (url,)).fetchone()
        return row
    finally:
        graph.close()
        try:
            os.remove(path)
        except PermissionError:
            pass


class TestSpamFarms(unittest.TestCase):
    TARGET = "http://target.test/page"
    GOOD = "http://good.test/page"

    def _ring_edges(self, n, extra=()):
        """n ring sites; each links the target AND links BOTH ring
        neighbors — genuinely MUTUAL pairs, so the reciprocal discount
        path actually runs."""
        edges = []
        for i in range(n):
            src = f"http://ring{i}.test/p{i}"
            edges.append((src, self.TARGET))
            edges.append((src, f"http://ring{(i + 1) % n}.test/p{(i + 1) % n}"))
            edges.append((f"http://ring{(i + 1) % n}.test/p{(i + 1) % n}", src))
        edges.extend(extra)
        return edges

    def test_reciprocal_ring_matters_less_than_clean_diversity(self):
        ring = self._ring_edges(8)
        ring_auth = _authority_of(ring, self.TARGET)
        diverse = [(f"http://d{i}.test/x{i}", self.TARGET) for i in range(8)]
        div_auth = _authority_of(diverse, self.TARGET)
        self.assertIsNotNone(ring_auth)
        self.assertIsNotNone(div_auth)
        # measured: ring authority <= clean-diversity authority (the
        # reciprocal discount on mutual ring links cannot INCREASE the
        # farm's leverage; equality is acceptable, advantage is not)
        self.assertLessEqual(ring_auth[0], div_auth[0] + 1e-9,
                             f"reciprocal ring authority {ring_auth[0]:.3f} "
                             f"must not EXCEED clean diversity "
                             f"{div_auth[0]:.3f}")

    def test_hub_and_spoke_and_wheel_shapes(self):
        hub = [(f"http://spoke{i}.test/s{i}", self.TARGET) for i in range(20)]
        hub_auth = _authority_of(hub, self.TARGET)
        wheel = self._ring_edges(10, extra=[
            (f"http://wheel{i}.test/w{i}", f"http://wheel{(i + 1) % 10}.test/w{(i + 1) % 10}")
            for i in range(10)])
        wheel_auth = _authority_of(wheel, self.TARGET)
        self.assertIsNotNone(hub_auth)
        self.assertIsNotNone(wheel_auth)
        # all shapes stay bounded within the [0,1] signal range
        for name, auth in (("hub-spoke", hub_auth), ("wheel", wheel_auth)):
            self.assertGreaterEqual(auth[0], 0.0)
            self.assertLessEqual(auth[0], 1.0)

    def test_subdomain_farm_saturation_vs_true_diversity(self):
        """The DOCUMENTED LIMITATION, quantified: 10 subdomains of ONE
        registrable domain earn the same diversity credit as 10 genuinely
        distinct domains under exact-hostname counting."""
        subdomain = [(f"http://s{i}.farm.test/p{i}", self.TARGET)
                     for i in range(10)]
        diverse = [(f"http://d{i}.test/x{i}", self.TARGET) for i in range(10)]
        sub_auth = _authority_of(subdomain, self.TARGET)
        div_auth = _authority_of(diverse, self.TARGET)
        # both saturate min(domains/10, 1) -> the diversity factors are equal;
        # PageRank mass differs slightly (subdomain pages carry no extra
        # inbound mass in this shape, diverse pages neither), so authority
        # is comparable — THE HOLE: the farm is NOT discounted.
        self.assertAlmostEqual(sub_auth[0], div_auth[0], delta=0.05,
                               msg="subdomain-farm saturation baseline: if "
                                   "this ever FAILS, the diversity counting "
                                   "changed — update the decision record")
        # popularity (log-scaled distinct-domain count) is also saturated
        self.assertGreaterEqual(sub_auth[1], div_auth[1] - 0.05)

    def test_good_page_outranks_farm_when_it_has_real_diversity(self):
        """End-to-end sanity: a good page with 10 diverse inlinks must
        outrank the SAME page boosted only by a reciprocal ring."""
        good_diverse = [(f"http://legit{i}.org/l{i}", self.GOOD)
                        for i in range(10)]
        ring_for_good = self._ring_edges(8)
        good_auth = _authority_of(good_diverse, self.GOOD)
        # the ring boosts its OWN target; compare the two targets' authority
        ring_auth = _authority_of(ring_for_good, self.TARGET)
        self.assertGreater(good_auth[0], ring_auth[0],
                           "genuine diversity must outrank a ring farm")


if __name__ == "__main__":
    unittest.main()
