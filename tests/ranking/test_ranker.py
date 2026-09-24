"""Tests for Phase 5 Stage 3 — the re-ranker, LTR framework, A/B bucketing."""
import os
import shutil
import tempfile
import time
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.core.hybrid_search import HybridSearch, SearchMode
from nexus_search.core.indexer import Indexer
from nexus_search.core.storage import Storage
from nexus_search.core.vector_store import VectorStoreManager
from nexus_search.ranking.ab import ExperimentLog, assign_variant
from nexus_search.ranking.query import understand_query
from nexus_search.ranking.ranker import (
    RankedResult, RankingModel, RankingWeights, WeightedSumModel, rerank,
)


class _Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "t.db")
        self.storage = Storage(self.path)
        self.indexer = Indexer(self.storage)
        self.vs = VectorStoreManager(self.path)
        self.hybrid = HybridSearch(self.storage, vector_store=self.vs, db_path=self.path)

    def tearDown(self):
        self.vs.close()
        self.storage.close()
        for _ in range(10):
            try:
                shutil.rmtree(self.dir)
                break
            except PermissionError:
                time.sleep(0.05)

    def add(self, doc_id, content, title="", metadata=None):
        self.indexer.add_document(doc_id, content, title=title, metadata=metadata or {})
        doc = self.storage.get_document(doc_id)
        self.vs.upsert(doc_id, f"{doc.title} {doc.content}", doc.doc_type,
                       (doc.metadata or {}).get("language", "") or "")

    def page_debug(self, q, mode=SearchMode.HYBRID):
        return self.hybrid.search_page(q, mode=mode, debug=True).results


class TestFrameworkContracts(unittest.TestCase):
    def test_ranking_model_is_abstract(self):
        with self.assertRaises(TypeError):
            RankingModel()  # cannot instantiate the framework seam

    def test_weighted_sum_zero_weights_is_zero(self):
        from nexus_search.ranking.features import RankingFeatures
        m = WeightedSumModel(RankingWeights(**{f: 0.0 for f in
                                               RankingWeights().__dataclass_fields__}))
        self.assertEqual(m.score(RankingFeatures(title_match=1.0)), 0.0)

    def test_weighted_sum_math(self):
        from nexus_search.ranking.features import RankingFeatures
        w = RankingWeights(bm25_score=2.0, freshness=1.0,
                           title_match=0.5, **{f: 0.0 for f in
                                               ("semantic_similarity", "url_match",
                                                "phrase_match", "document_quality",
                                                "content_quality", "language_relevance",
                                                "source_authority", "popularity",
                                                "click_signal")})
        f = RankingFeatures(bm25_score=1.0, freshness=0.5, title_match=1.0)
        self.assertAlmostEqual(WeightedSumModel(w).score(f),
                               2.0 * 1.0 + 1.0 * 0.5 + 0.5 * 1.0)


class TestRerankNoOp(_Base):
    """CRITICAL: zero non-retrieval weights must reproduce hybrid EXACTLY."""

    def _zero_extra_weights(self):
        w = RankingWeights()
        for name in ("title_match", "url_match", "phrase_match", "freshness",
                     "document_quality", "content_quality", "language_relevance",
                     "source_authority", "popularity", "click_signal"):
            setattr(w, name, 0.0)
        return w

    def test_zero_weights_reproduce_hybrid_order(self):
        self.add("a", "python search engine alpha beta", "Python Alpha")
        self.add("b", "python gamma delta epsilon", "Python Gamma")
        self.add("c", "zzqqj python", "Rare")
        results = self.page_debug("python")
        ranked = rerank(results, "python", self._zero_extra_weights(), storage=self.storage)
        self.assertEqual([r.doc_id for r in results],
                         [r.doc_id for r in ranked])  # same ORDER, not just same set
        for orig, rer in zip(results, ranked):
            # blended score equals the fused score when extras are zeroed
            if orig.bm25_normalized is not None or orig.vector_normalized is not None:
                self.assertAlmostEqual(rer.final_score, orig.score)

    def test_rerank_returns_ranked_results_with_features(self):
        self.add("a", "python content", "Python Doc")
        ranked = rerank(self.page_debug("python"), "python", storage=self.storage)
        self.assertTrue(all(isinstance(r, RankedResult) for r in ranked))
        self.assertIsNotNone(ranked[0].features.title_match)


class TestRerankEffect(_Base):
    def test_title_weight_changes_order(self):
        # "x": term in TITLE, weak body. "y": strong term frequency, plain title.
        self.add("x", "weak body with a stray word", "Python Weekly")
        self.add("y", "python python python python python", "Generic")
        kw = RankingWeights(**{f: 0.0 for f in RankingWeights().__dataclass_fields__})
        kw.bm25_score = 1.0
        kw.semantic_similarity = 1.0
        kw.title_match = 5.0  # strong title preference
        results = self.page_debug("python")

        retrieval_only = RankingWeights(**{f: 0.0 for f in RankingWeights().__dataclass_fields__}
                                        | {"bm25_score": 1.0, "semantic_similarity": 1.0})
        plain = rerank(results, "python", retrieval_only, storage=self.storage)
        ranked = rerank(results, "python", kw, storage=self.storage)
        fx = next(r for r in ranked if r.doc_id == "x")
        fy = next(r for r in ranked if r.doc_id == "y")
        self.assertGreater(fx.features.title_match, fy.features.title_match)
        # title weight actually flips the order vs retrieval-only blending
        self.assertEqual(ranked[0].doc_id, "x")
        self.assertEqual(plain[0].doc_id, "y")

    def test_freshness_weight_prefers_newer(self):
        self.add("old", "python data", "Old", metadata={"quality": 0.9})
        self.add("new", "python data", "New", metadata={"quality": 0.9})
        self.storage.conn.execute("UPDATE documents SET added_at = ? WHERE doc_id = 'old'",
                                  (time.time() - 90 * 86400,))
        self.storage.conn.commit()
        kw = RankingWeights(**{f: 0.0 for f in RankingWeights().__dataclass_fields__})
        kw.freshness = 1.0
        ranked = rerank(self.page_debug("python"), "python", kw, storage=self.storage)
        new_feats = next(r for r in ranked if r.doc_id == "new").features
        self.assertGreater(new_feats.freshness,
                           next(r for r in ranked if r.doc_id == "old").features.freshness)

    def test_rerank_without_storage_never_crashes(self):
        self.add("a", "python content", "A")
        ranked = rerank(self.page_debug("python"), "python")  # storage=None
        self.assertTrue(ranked)  # degrades to neutral features, no crash


class TestAB(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "t.db")
        self.log = ExperimentLog(self.path)

    def tearDown(self):
        self.log.close()
        for _ in range(10):
            try:
                shutil.rmtree(self.dir)
                break
            except PermissionError:
                time.sleep(0.05)

    def test_variant_assignment_deterministic(self):
        for _ in range(3):
            self.assertEqual(assign_variant("user-42", ["a", "b"]),
                             assign_variant("user-42", ["a", "b"]))

    def test_variant_assignment_in_set(self):
        for i in range(200):
            self.assertIn(assign_variant(f"u{i}", ["control", "treatment"]),
                          {"control", "treatment"})

    def test_variants_roughly_balanced(self):
        counts = {"control": 0, "treatment": 0}
        for i in range(400):
            counts[assign_variant(f"u{i}", ["control", "treatment"])] += 1
        # ~50/50 split; allow very loose tolerance (deterministic hash isn't a PRNG)
        self.assertGreater(counts["control"], 120)
        self.assertGreater(counts["treatment"], 120)

    def test_empty_variants_rejected(self):
        with self.assertRaises(ValueError):
            assign_variant("u1", [])

    def test_log_records_and_reads_back(self):
        self.log.record("python", "treatment", "hybrid+rerank")
        rows = self.log.rows_for("python")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "treatment")
        self.assertEqual(rows[0][2], "hybrid+rerank")
        self.assertEqual(self.log.count(), 1)

    def test_empty_log_query_returns_empty(self):
        self.assertEqual(self.log.rows_for("nothing-logged"), [])


class TestApiRerankToggle(_Base):
    """API-level: rerank=False must be byte-identical to pre-Phase-5."""

    def setUp(self):
        super().setUp()
        import importlib
        os.environ["NEXUS_DB"] = os.path.join(self.dir, "api.db")
        os.environ["NEXUS_EMBEDDER"] = "hash:384"
        from nexus_search.core import embedders
        embedders.reset_embedder()
        from nexus_search.core import api
        importlib.reload(api)
        from fastapi.testclient import TestClient
        self.client = TestClient(api.app)
        self.api = api

    def test_rerank_false_identical(self):
        self.client.post("/documents", json={"doc_id": "a", "content": "python alpha", "title": "A"})
        self.client.post("/documents", json={"doc_id": "b", "content": "python beta beta", "title": "B"})
        base = self.client.get("/search", params={"q": "python"}).json()
        off = self.client.get("/search", params={"q": "python", "rerank": "false"}).json()
        self.assertEqual([r["doc_id"] for r in base["results"]],
                         [r["doc_id"] for r in off["results"]])
        self.assertEqual([r["score"] for r in base["results"]],
                         [r["score"] for r in off["results"]])
        self.assertFalse(off["metadata"]["reranked"])

    def test_rerank_true_reports_flag(self):
        self.client.post("/documents", json={"doc_id": "a", "content": "python alpha", "title": "A"})
        body = self.client.get("/search", params={"q": "python", "rerank": "true"}).json()
        self.assertTrue(body["metadata"]["reranked"])

    def test_session_buckets_and_logs(self):
        self.client.post("/documents", json={"doc_id": "a", "content": "python alpha", "title": "A"})
        self.client.get("/search", params={"q": "python", "session": "sess-1"})
        # experiment log lives in the same DB the API is pointing at
        rows = self.api.experiment_log.rows_for("python")
        self.assertEqual(len(rows), 1)
        self.assertIn(rows[0][1], ("control", "treatment"))

    def test_explicit_rerank_overrides_bucket(self):
        self.client.post("/documents", json={"doc_id": "a", "content": "python alpha", "title": "A"})
        body = self.client.get("/search", params={"q": "python", "session": "sess-1",
                                                  "rerank": "false"}).json()
        self.assertFalse(body["metadata"]["reranked"])


if __name__ == "__main__":
    unittest.main()
