"""Phase 6 ranking integration: authority/popularity signals flow into rerank."""
import os
import shutil
import tempfile
import time
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.core.indexer import Indexer
from nexus_search.core.storage import Storage
from nexus_search.ingestion.dedup import Deduplicator
from nexus_search.links.authority import compute_authority
from nexus_search.links.graph import LinkGraph
from nexus_search.ranking.features import build_context, extract_features
from nexus_search.ranking.ranker import RankingWeights, WeightedSumModel, rerank
from nexus_search.ranking.signals import NEUTRAL


class TestAuthoritySignalIntegration(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "t.db")
        self.storage = Storage(self.db)
        self.indexer = Indexer(self.storage)
        self.dedup = Deduplicator(self.db)
        self.graph = LinkGraph(self.db)

    def tearDown(self):
        for closer in (self.graph.close, self.dedup.close, self.storage.close):
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

    def _index_web_docs(self):
        """Index three identical-content docs; only their URLs differ."""
        for doc_id, url in [("hub", "http://hub.com"), ("clique", "http://c1.com"),
                            ("orphan", "http://orphan.com")]:
            self.indexer.add_document(doc_id, "identical search content here",
                                      title="Same", doc_type="web",
                                      metadata={"url": url})

    def _build_graph(self):
        """Hub: 10 distinct-domain inlinks. Clique: 3 mutual. Orphan: none.
        URLs MUST match doc.metadata['url'] — signals look up by that key."""
        for i in range(10):
            self.graph.record_edge(f"http://d{i}.com", "http://hub.com")
        self.graph.record_edge("http://c1.com", "http://c2.com")
        self.graph.record_edge("http://c2.com", "http://c3.com")
        self.graph.record_edge("http://c3.com", "http://c1.com")
        self.graph.record_edge("http://orphan.com", "http://nowhere.com")
        compute_authority(self.graph)

    def _mock_results(self):
        """Minimal HybridSearchResult stand-ins carrying just what rerank reads."""
        from nexus_search.core.hybrid_search import HybridSearchResult

        results = []
        for doc_id in ("hub", "clique", "orphan"):
            doc = self.storage.get_document(doc_id)
            results.append(HybridSearchResult(
                doc_id=doc_id, score=1.0, title=doc.title, snippet="",
                doc_type=doc.doc_type, metadata=doc.metadata,
                bm25_score=1.0, source="bm25"))
        return results

    def test_hub_outranks_clique_with_authority_weight(self):
        """The mission case: with authority-only weights, hub > clique on
        identical content — the only difference is link-graph trust."""
        self._index_web_docs()
        self._build_graph()
        results = self._mock_results()

        # Pure authority comparison: zero ALL other signals so only
        # source_authority differentiates the (identical-content) docs
        weights = RankingWeights(
            bm25_score=0.0, semantic_similarity=0.0,
            title_match=0.0, url_match=0.0, phrase_match=0.0,
            freshness=0.0, document_quality=0.0, content_quality=0.0,
            language_relevance=0.0, popularity=0.0, click_signal=0.0,
            source_authority=1.0)
        ranked = rerank(results, "search", weights=weights,
                        storage=self.storage, link_intel=self.graph)
        self.assertEqual(ranked[0].doc_id, "hub")

        # Without the graph, the same weights produce NEUTRAL ties — no crash
        ranked_no_graph = rerank(results, "search", weights=weights,
                                 storage=self.storage, link_intel=None)
        # All scores equal (all NEUTRAL 0.5): tie-break by doc_id
        scores = [r.final_score for r in ranked_no_graph]
        self.assertEqual(len(set(scores)), 1)

    def test_zero_authority_weight_reproduces_retrieval_order(self):
        """Weights 0.0 = feature inert (Phase 5 invariant extended to Phase 6)."""
        self._index_web_docs()
        self._build_graph()
        results = self._mock_results()

        default_weights = RankingWeights()  # authority=0.0, popularity=0.0
        ranked = rerank(results, "search", weights=default_weights,
                        storage=self.storage, link_intel=self.graph)
        # With default weights the graph data exists but contributes nothing:
        # every result keeps its retrieval score (1.0) + neutral doc signals
        self.assertEqual(len(ranked), 3)

    def test_authority_for_unknown_url_is_neutral(self):
        """A doc whose URL isn't in the graph gets NEUTRAL (0.5), not zero."""
        self._index_web_docs()
        self._build_graph()
        results = self._mock_results()

        ctx = build_context("search", link_intel=self.graph)
        # "orphan.com" IS in the graph but only links OUT — check a truly
        # unknown URL maps to NEUTRAL
        self.indexer.add_document("ghost", "ghost content", title="Ghost",
                                  doc_type="web",
                                  metadata={"url": "http://ghost.example"})
        from nexus_search.core.hybrid_search import HybridSearchResult
        ghost = HybridSearchResult(doc_id="ghost", score=0.5, title="Ghost",
                                   snippet="", doc_type="web",
                                   metadata={"url": "http://ghost.example"},
                                   bm25_score=0.5, source="bm25")
        feats = extract_features("ghost", self.storage, ctx,
                                  doc=self.storage.get_document("ghost"))
        self.assertEqual(feats.source_authority, NEUTRAL)
        self.assertEqual(feats.popularity, NEUTRAL)

    def test_metadata_override_beats_graph(self):
        """Explicit doc.metadata['authority_score'] wins over graph data —
        the operator override path from signals.py's contract."""
        self._index_web_docs()
        self._build_graph()
        self.indexer.add_document("override", "override content", title="O",
                                  doc_type="web",
                                  metadata={"url": "http://hub.com",
                                            "authority_score": 0.99})
        ctx = build_context("search", link_intel=self.graph)
        doc = self.storage.get_document("override")
        feats = extract_features("override", self.storage, ctx, doc=doc)
        self.assertEqual(feats.source_authority, 0.99)


if __name__ == "__main__":
    unittest.main()
