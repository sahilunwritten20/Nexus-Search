"""WP5: link-graph analysis — is_internal, traversal, components, version
guard, anchor relevance, domain authority, read cache, caps on normalized URLs.
"""
import os
import shutil
import tempfile
import time
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.core.indexer import Indexer  # noqa: E402
from nexus_search.core.storage import Storage  # noqa: E402
from nexus_search.links.authority import compute_authority  # noqa: E402
from nexus_search.links.graph import LinkGraph  # noqa: E402
from nexus_search.ranking.features import build_context, extract_features  # noqa: E402
from nexus_search.ranking.signals import NEUTRAL  # noqa: E402


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


class TestInternalExternal(_GraphTest):
    def test_classification_at_write(self):
        self.graph.record_edge("http://site.com/a", "http://site.com/b", "nav", "")
        self.graph.record_edge("http://site.com/a", "http://other.com/x", "out", "")
        row = self.graph.conn.execute(
            "SELECT is_internal FROM link_edges WHERE to_url = 'http://site.com/b'"
        ).fetchone()
        self.assertEqual(row[0], 1)
        row = self.graph.conn.execute(
            "SELECT is_internal FROM link_edges WHERE to_url = 'http://other.com/x'"
        ).fetchone()
        self.assertEqual(row[0], 0)

    def test_counts_per_page(self):
        self.graph.record_edge("http://s.com/a", "http://s.com/b")
        self.graph.record_edge("http://s.com/a", "http://s.com/c")
        self.graph.record_edge("http://s.com/a", "http://ext.com/x")
        counts = self.graph.internal_external_counts("http://s.com/a")
        self.assertEqual(counts, {"internal": 2, "external": 1})

    def test_backfill_from_pre_v3_rows(self):
        now = time.time()
        with self.graph.lock:
            self.graph.conn.execute(
                "INSERT INTO link_edges (from_url, to_url, anchor_text, rel_attrs, "
                "from_domain, to_domain, is_internal, first_seen, last_seen) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                ("http://old.com/x", "http://old.com/y", "a", "",
                 "old.com", "old.com", -1, now, now))
            self.graph.conn.commit()
        reopened = LinkGraph(self.db)  # backfill at open
        try:
            row = reopened.conn.execute(
                "SELECT is_internal FROM link_edges WHERE to_url = 'http://old.com/y'"
            ).fetchone()
            self.assertEqual(row[0], 1)
        finally:
            reopened.close()


class TestTraversal(_GraphTest):
    def _chain(self):
        # a/ -> b/ -> c/ -> d/, plus a/ -> e  (paths avoid root-slash noise)
        for f, t in [("http://a.com/", "http://b.com/"),
                     ("http://b.com/", "http://c.com/"),
                     ("http://c.com/", "http://d.com/"),
                     ("http://a.com/", "http://e.com/")]:
            self.graph.record_edge(f, t)

    def test_neighbors_directions_and_limit(self):
        self._chain()
        out = self.graph.neighbors("http://a.com", direction="out")
        self.assertEqual(sorted(e.to_url for e in out),
                         ["http://b.com/", "http://e.com/"])
        incoming = self.graph.neighbors("http://b.com/", direction="in")
        self.assertEqual([e.from_url for e in incoming], ["http://a.com/"])
        both = self.graph.neighbors("http://b.com/", direction="both")
        self.assertEqual(len(both), 2)
        limited = self.graph.neighbors("http://a.com", direction="out", limit=1)
        self.assertEqual(len(limited), 1)
        with self.assertRaises(ValueError):
            self.graph.neighbors("http://a.com", direction="sideways")

    def test_bounded_bfs_depth_and_node_caps(self):
        self._chain()
        depth1 = self.graph.bounded_bfs("http://a.com", max_depth=1)
        self.assertEqual(depth1, ["http://b.com/", "http://e.com/"])
        depth3 = self.graph.bounded_bfs("http://a.com", max_depth=3)
        self.assertEqual(depth3, ["http://b.com/", "http://c.com/",
                                  "http://d.com/", "http://e.com/"])
        capped = self.graph.bounded_bfs("http://a.com", max_depth=3, max_nodes=1)
        self.assertEqual(capped, ["http://b.com/"])  # sorted expansion, bounded

    def test_bfs_is_iterative_and_deterministic(self):
        # a wide fan that would blow a naive recursive frontier: 300 outlinks
        for i in range(300):
            self.graph.record_edge("http://hub.com", f"http://t{i}.com")
        first = self.graph.bounded_bfs("http://hub.com", max_depth=1)
        second = self.graph.bounded_bfs("http://hub.com", max_depth=1)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 200)  # node cap default


class TestComponents(_GraphTest):
    def test_two_disjoint_pairs(self):
        self.graph.record_edge("http://a1.com/x", "http://a2.com/x")
        self.graph.record_edge("http://b1.com/x", "http://b2.com/x")
        mapping, sizes = self.graph.connected_components()
        self.assertEqual(sorted(sizes.values()), [2, 2])
        self.assertEqual(mapping["http://a1.com/x"], mapping["http://a2.com/x"])
        self.assertNotEqual(mapping["http://a1.com/x"], mapping["http://b1.com/x"])

    def test_chain_is_one_component(self):
        for f, t in [("http://n1.com/x", "http://n2.com/x"),
                     ("http://n2.com/x", "http://n3.com/x"),
                     ("http://n3.com/x", "http://n4.com/x")]:
            self.graph.record_edge(f, t)
        mapping, sizes = self.graph.connected_components()
        self.assertEqual(sizes, {0: 4})
        self.assertEqual(len(set(mapping.values())), 1)

    def test_component_of(self):
        self.graph.record_edge("http://a1.com/x", "http://a2.com/x")
        comp = self.graph.component_of("http://a2.com/x")
        self.assertEqual(comp, (0, 2))
        self.assertIsNone(self.graph.component_of("http://unknown.com/x"))

    def test_deterministic_ids(self):
        self.graph.record_edge("http://z.com", "http://a.com")
        first = self.graph.connected_components()
        second = self.graph.connected_components()
        self.assertEqual(first, second)


class TestVersionGuard(_GraphTest):
    def test_recompute_skipped_when_unchanged(self):
        self.graph.record_edge("http://a.com", "http://b.com")
        first = compute_authority(self.graph)
        self.assertFalse(first.skipped)
        second = compute_authority(self.graph)
        self.assertTrue(second.skipped, "unchanged edge set must skip the pass")

    def test_new_edge_invalidates(self):
        self.graph.record_edge("http://a.com", "http://b.com")
        compute_authority(self.graph)
        self.graph.record_edge("http://c.com", "http://b.com")
        third = compute_authority(self.graph)
        self.assertFalse(third.skipped)

    def test_anchor_change_invalidates(self):
        self.graph.record_edge("http://a.com", "http://b.com", "old anchor", "")
        compute_authority(self.graph)
        self.graph.record_edge("http://a.com", "http://b.com", "new anchor", "")
        stats = compute_authority(self.graph)
        self.assertFalse(stats.skipped,
                         "an anchor change is semantic and must re-run")

    def test_last_seen_refresh_does_not_recompute(self):
        self.graph.record_edge("http://a.com", "http://b.com", "anchor", "")
        compute_authority(self.graph)
        self.graph.record_edge("http://a.com", "http://b.com", "anchor", "")
        stats = compute_authority(self.graph)
        self.assertTrue(stats.skipped,
                         "a pure last_seen refresh must not trigger a recompute")

    def test_force_overrides(self):
        self.graph.record_edge("http://a.com", "http://b.com")
        compute_authority(self.graph)
        forced = compute_authority(self.graph, force=True)
        self.assertFalse(forced.skipped)


class TestAnchorAggregation(_GraphTest):
    def test_anchors_built_at_recompute(self):
        self.graph.record_edge("http://a.com", "http://b.com", "machine learning guide", "")
        self.graph.record_edge("http://n.com", "http://b.com", "nofollow", "nofollow")
        self.graph.record_edge("http://c.com", "http://b.com", "python tutorial", "")
        compute_authority(self.graph)
        anchors = self.graph.anchor_text_for("http://b.com")
        self.assertIn("machine learning guide", anchors)
        self.assertIn("python tutorial", anchors)
        self.assertIn("nofollow", anchors, "nofollow anchors still describe the target")
        self.assertEqual(self.graph.anchor_text_for("http://nowhere.com"), "")

    def test_anchor_text_capped_at_512(self):
        for i in range(60):
            self.graph.record_edge(f"http://s{i}.com", "http://t.com",
                                   f"very long anchor number {i} " + "x" * 40, "")
        compute_authority(self.graph)
        self.assertLessEqual(len(self.graph.anchor_text_for("http://t.com")), 512)


class TestAnchorSignal(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "s.db")
        self.storage = Storage(self.db)
        self.indexer = Indexer(self.storage)
        self.graph = LinkGraph(self.db)
        # identical content; only the graph treats them differently
        for doc_id, url in [("anchored", "http://anchored.com/page"),
                            ("bare", "http://bare.com/page")]:
            self.indexer.add_document(doc_id, "search topic content",
                                      title="Same", doc_type="web",
                                      metadata={"url": url})
        self.graph.record_edge("http://src.com", "http://anchored.com/page",
                               "search topic overview", "")
        compute_authority(self.graph)

    def tearDown(self):
        for closer in (self.graph.close, self.storage.close):
            try:
                closer()
            except Exception:
                pass
        for _ in range(10):
            try:
                shutil.rmtree(self.dir)
                break
            except PermissionError:
                time.sleep(0.05)

    def _feats(self, doc_id, query="search topic"):
        ctx = build_context(query, link_intel=self.graph)
        return extract_features(doc_id, self.storage, ctx,
                                doc=self.storage.get_document(doc_id))

    def test_anchor_relevance_values(self):
        anchored = self._feats("anchored")
        bare = self._feats("bare")
        self.assertEqual(anchored.anchor_relevance, 1.0)
        self.assertEqual(bare.anchor_relevance, NEUTRAL)  # no anchors: no info
        # a query sharing no anchor tokens with real anchors scores 0.0
        off = self._feats("anchored", query="kubernetes deployment")
        self.assertEqual(off.anchor_relevance, 0.0)

    def test_zero_weight_identity(self):
        """NEXUS_ANCHOR_WEIGHT ships at 0.0: with default weights the
        graph's presence cannot change the ranker output — byte-identical
        scores with link_intel=None vs the real graph."""
        from nexus_search.core.hybrid_search import HybridSearchResult
        from nexus_search.ranking.ranker import RankingWeights, rerank

        def run(link_intel):
            results = [HybridSearchResult(doc_id=d, score=1.0, title="Same",
                                           snippet="", doc_type="web",
                                           metadata={"url": f"http://{d}.com/page"})
                       for d in ("anchored", "bare")]
            ranked = rerank(results, "search topic",
                            weights=RankingWeights(),  # anchor_relevance=0.0
                            storage=self.storage, link_intel=link_intel)
            # 6dp: the golden suite's precision. (The 12th decimal would
            # wobble on freshness clock noise between the two runs, which
            # is not the graph's doing.)
            return [(r.doc_id, round(r.final_score, 6)) for r in ranked]

        self.assertEqual(run(self.graph), run(None),
                         "default weights: graph data must be inert")

    def test_anchor_weight_flips_order_toward_anchored_doc(self):
        saved = os.environ.get("NEXUS_ANCHOR_WEIGHT")
        os.environ["NEXUS_ANCHOR_WEIGHT"] = "0.8"
        try:
            # from_env() reads the environment at call time — no reload
            # (reloading the ranker module would rebind RankedResult in the
            # shared module dict and break isinstance in OTHER test files)
            from nexus_search.ranking.ranker import RankingWeights
            weights = RankingWeights.from_env()
            self.assertEqual(weights.anchor_relevance, 0.8)
        finally:
            if saved is None:
                os.environ.pop("NEXUS_ANCHOR_WEIGHT", None)
            else:
                os.environ["NEXUS_ANCHOR_WEIGHT"] = saved
        # identical-content docs, retrieval order bare-first: the anchor
        # signal must be able to move the anchored doc up
        from nexus_search.core.hybrid_search import HybridSearchResult
        from nexus_search.ranking.ranker import rerank
        results = [HybridSearchResult(doc_id="bare", score=1.0, title="Same",
                                       snippet="", doc_type="web",
                                       metadata={"url": "http://bare.com/page"}),
                   HybridSearchResult(doc_id="anchored", score=1.0, title="Same",
                                       snippet="", doc_type="web",
                                       metadata={"url": "http://anchored.com/page"})]
        ranked = rerank(results, "search topic", weights=weights,
                        storage=self.storage, link_intel=self.graph)
        self.assertEqual(ranked[0].doc_id, "anchored",
                         "anchor weight > 0 must reorder toward the anchored doc")


class TestDomainAuthority(_GraphTest):
    def test_domain_aggregate_computed(self):
        self.graph.record_edge("http://ext1.com", "http://d.com/a")
        self.graph.record_edge("http://ext2.com", "http://d.com/b")
        compute_authority(self.graph)
        row = self.graph.conn.execute(
            "SELECT authority, popularity, pages FROM domain_authority "
            "WHERE domain = 'd.com'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[2], 2)  # both pages of the domain

    def test_fallback_default_off(self):
        self.graph.record_edge("http://x.com", "http://d.com/a")
        compute_authority(self.graph)
        # unknown page on a KNOWN domain: no fallback by default
        self.assertIsNone(self.graph.authority_for("http://d.com/unknown"))

    def test_fallback_opt_in(self):
        saved = os.environ.get("NEXUS_DOMAIN_AUTHORITY_FALLBACK")
        os.environ["NEXUS_DOMAIN_AUTHORITY_FALLBACK"] = "1"
        try:
            self.graph.record_edge("http://x.com", "http://d.com/a")
            compute_authority(self.graph)
            found = self.graph.authority_for("http://d.com/unknown")
            self.assertIsNotNone(found, "opt-in fallback resolves domain scores")
            self.assertGreater(found[0], 0.0)
        finally:
            if saved is None:
                os.environ.pop("NEXUS_DOMAIN_AUTHORITY_FALLBACK", None)
            else:
                os.environ["NEXUS_DOMAIN_AUTHORITY_FALLBACK"] = saved


class TestAuthorityReadCache(_GraphTest):
    def test_in_process_recompute_invalidates_cache(self):
        self.graph.record_edge("http://a.com", "http://hub.com")
        compute_authority(self.graph)
        first = self.graph.authority_for("http://hub.com")
        # warm the cache, then change the graph and recompute in-process
        self.graph.authority_for("http://hub.com")
        for i in range(10):
            self.graph.record_edge(f"http://d{i}.com", "http://hub.com")
        compute_authority(self.graph, force=True)
        second = self.graph.authority_for("http://hub.com")
        self.assertGreater(second[0], first[0],
                           "cached value must refresh after in-process recompute")

    def test_cross_process_write_invalidates_cache(self):
        self.graph.record_edge("http://a.com", "http://hub.com")
        compute_authority(self.graph)
        first = self.graph.authority_for("http://hub.com")
        self.graph.authority_for("http://hub.com")  # warm
        other = LinkGraph(self.db)  # another connection = another "process"
        try:
            for i in range(10):
                other.record_edge(f"http://e{i}.com", "http://hub.com")
            compute_authority(other, force=True)
        finally:
            other.close()
        second = self.graph.authority_for("http://hub.com")
        self.assertGreater(second[0], first[0],
                           "external recompute must be visible without restart")


class TestCapsOnNormalizedUrls(_GraphTest):
    def test_variant_edges_count_once_toward_pair_cap(self):
        from nexus_search.links.graph import MAX_EDGES_PER_DOMAIN_PAIR
        # ONE source domain (many pages) -> ONE target domain, each target
        # written as a fragment variant: they all normalize into the single
        # (s.com -> t.com) pair, so the cap fires at exactly 500.
        for i in range(MAX_EDGES_PER_DOMAIN_PAIR):
            self.assertTrue(self.graph.record_edge(
                f"http://s.com/page{i}", f"http://t.com/p{i}#frag"))
        self.assertFalse(self.graph.record_edge(
            "http://s.com/extra", "http://t.com/overflow"))

    def test_reciprocal_discount_survives_normalization(self):
        self.graph.record_edge("http://a.com", "http://b.com/x")
        self.graph.record_edge("http://b.com/x", "http://a.com")
        stats = compute_authority(self.graph)
        self.assertEqual(stats.reciprocal_pairs, 1)


if __name__ == "__main__":
    unittest.main()
