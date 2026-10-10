"""WP14 Item 1 — `min_score` (raw cosine floor) plumbed end-to-end.

Contract under test:
- `HybridSearch.search_page(min_score=...)` floors the VECTOR side ONLY (raw
  cosine); fused (RRF/weighted) scores are never floored.
- `None` (default) = unchanged behavior.
- A floor that empties the vector side takes the existing empty-vector path
  (hybrid -> BM25-only results, honest metadata; semantic -> empty page).
- Raw `bm25_score`/`vector_score` stay readable on HybridSearchResult (and on
  the API wire) after fusion AND after rerank, including the None cases.
- `/search?min_score=` validates [-1, 1], appears in OpenAPI, participates in
  the query-cache key, and reaches the facet pass.
"""
import importlib
import os
import tempfile
import unittest

os.environ.setdefault("NEXUS_ENV", "dev")
os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None

from nexus_search.core.hybrid_search import SearchMode, create_hybrid_search
from nexus_search.core.embedding_sync import EmbeddingSync
from nexus_search.core.indexer import Indexer
from nexus_search.core.storage import Storage
from nexus_search.core.vector_store import VectorStoreManager

DOCS = [
    ("d1", "alpha beta gamma delta", "Alpha doc"),
    ("d2", "completely unrelated words here", "Other doc"),
    ("d3", "alpha zeta", "Partial doc"),
    ("d4", "novel vocabulary nothing shared", "Distant doc"),
]


class MinScoreHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wp14_min_score_")
        self.db = os.path.join(self.tmp, "h.db")
        self.storage = Storage(self.db)
        self.indexer = Indexer(self.storage)
        self.vector_store = VectorStoreManager(self.db)
        # embed on write, exactly like the API path does
        self._sync = EmbeddingSync(self.vector_store, batch_size=10)
        self._sync.attach(self.indexer)
        for doc_id, content, title in DOCS:
            self.indexer.add_document(doc_id=doc_id, content=content, title=title)
        self._sync.flush()
        self.hybrid = create_hybrid_search(self.storage,
                                           vector_store=self.vector_store,
                                           db_path=self.db)

    def tearDown(self):
        self.hybrid.close()
        self.vector_store.close()
        self.storage.close()


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class TestMinScoreOnSearchPage(MinScoreHarness):
    Q = "alpha beta"

    def _vector_scores(self):
        page = self.hybrid.search_page(self.Q, top_k=10, mode=SearchMode.HYBRID)
        return {r.doc_id: r.vector_score for r in page.results}

    def test_param_exists_on_search_page(self):
        # FAILING pre-Item-1: search_page has no min_score parameter (H1)
        self.hybrid.search_page(self.Q, top_k=10, mode=SearchMode.HYBRID,
                                min_score=0.5)

    def test_floor_filters_vector_side_only(self):
        scores = self._vector_scores()
        ranked = sorted(v for v in scores.values() if v is not None)
        self.assertGreaterEqual(len(ranked), 2, "need at least 2 vector hits")
        mid = (ranked[0] + ranked[-1]) / 2.0
        floored = self.hybrid.search_page(self.Q, top_k=10,
                                          mode=SearchMode.HYBRID, min_score=mid)
        kept = {r.doc_id: r.vector_score for r in floored.results}
        for doc_id, score in kept.items():
            if score is not None:
                self.assertGreaterEqual(score, mid,
                                        "vector hit under the floor survived")
        gone = [d for d, s in scores.items()
                if s is not None and s < mid and d not in kept]
        self.assertTrue(gone, "the floor must actually remove low-cosine docs")

    def test_semantic_mode_floor_filters(self):
        scores = {r.doc_id: r.vector_score
                  for r in self.hybrid.search_page(
                      self.Q, top_k=10, mode=SearchMode.SEMANTIC).results}
        ranked = sorted(scores.values())
        mid = (ranked[0] + ranked[-1]) / 2.0
        floored = self.hybrid.search_page(self.Q, top_k=10,
                                          mode=SearchMode.SEMANTIC, min_score=mid)
        for r in floored.results:
            self.assertGreaterEqual(r.vector_score, mid)

    def test_floor_emptying_vector_side_takes_bmtree_path(self):
        # A floor no cosine can pass -> hybrid must degrade to the existing
        # vector-side-empty path: BM25-only results, no crash, honest metadata.
        floored = self.hybrid.search_page(self.Q, top_k=10,
                                          mode=SearchMode.HYBRID, min_score=2.0)
        keyword = self.hybrid.search_page(self.Q, top_k=10, mode=SearchMode.KEYWORD)
        self.assertEqual(floored.metadata["vector_candidates"], 0)
        self.assertFalse(floored.metadata["fallback"])
        self.assertEqual([r.doc_id for r in floored.results],
                         [r.doc_id for r in keyword.results])
        self.assertEqual(floored.total, keyword.total)

    def test_floor_emptying_semantic_page(self):
        floored = self.hybrid.search_page(self.Q, top_k=10,
                                          mode=SearchMode.SEMANTIC, min_score=2.0)
        self.assertEqual(floored.results, [])
        self.assertEqual(floored.total, 0)
        self.assertEqual(floored.metadata["vector_candidates"], 0)

    def test_none_is_unchanged(self):
        absent = self.hybrid.search_page(self.Q, top_k=10, mode=SearchMode.HYBRID)
        none_ = self.hybrid.search_page(self.Q, top_k=10, mode=SearchMode.HYBRID,
                                        min_score=None)
        self.assertEqual([r.doc_id for r in absent.results],
                         [r.doc_id for r in none_.results])
        self.assertEqual([r.score for r in absent.results],
                         [r.score for r in none_.results])

    def test_keyword_mode_ignores_floor(self):
        plain = self.hybrid.search_page(self.Q, top_k=10, mode=SearchMode.KEYWORD)
        floored = self.hybrid.search_page(self.Q, top_k=10,
                                          mode=SearchMode.KEYWORD, min_score=2.0)
        self.assertEqual([r.doc_id for r in plain.results],
                         [r.doc_id for r in floored.results])

    def test_floor_never_touches_fused_scores(self):
        # The returned `score` is RRF rank math (1/(k+rank)-shaped), not the
        # cosine: flooring changes WHO is in the pool, never the score formula.
        page = self.hybrid.search_page(self.Q, top_k=10, mode=SearchMode.HYBRID,
                                       min_score=-1.0)
        for r in page.results:
            if r.source == "bm25+vector":
                # RRF: both contributions are weight/(60+rank) <= 2/61
                self.assertLessEqual(r.score, 2.0 / 61 + 1e-9)
                self.assertIsNotNone(r.bm25_score)
                self.assertIsNotNone(r.vector_score)

    def test_raw_scores_survive_fusion_with_none_cases(self):
        page = self.hybrid.search_page(self.Q, top_k=10, mode=SearchMode.HYBRID)
        by_src = {r.source: r for r in page.results}
        both = by_src.get("bm25+vector")
        if both is not None:
            self.assertIsNotNone(both.bm25_score)
            self.assertIsNotNone(both.vector_score)
        if "vector" in by_src:  # doc found by the vector side only
            self.assertIsNone(by_src["vector"].bm25_score)
            self.assertIsNotNone(by_src["vector"].vector_score)
        if "bm25" in by_src:    # doc found by BM25 only
            self.assertIsNone(by_src["bm25"].vector_score)
            self.assertIsNotNone(by_src["bm25"].bm25_score)


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class TestMinScoreOnApi(unittest.TestCase):
    """Boot the real app in dev mode against a temp DB (test_api.py pattern)."""

    @classmethod
    def setUpClass(cls):
        cls._saved_db = os.environ.get("NEXUS_DB")
        os.environ["NEXUS_DB"] = os.path.join(tempfile.mkdtemp(prefix="wp14_api_"),
                                              "api.db")
        os.environ.setdefault("NEXUS_ENV", "dev")
        from nexus_search.core import api
        importlib.reload(api)
        cls.api = api
        cls.client = TestClient(api.app)
        for doc_id, content, title in DOCS:
            r = cls.client.post("/documents", json={
                "doc_id": doc_id, "content": content, "title": title})
            assert r.status_code == 201, r.text

    @classmethod
    def tearDownClass(cls):
        if cls._saved_db is None:
            os.environ.pop("NEXUS_DB", None)
        else:
            os.environ["NEXUS_DB"] = cls._saved_db
        importlib.reload(cls.api)

    def test_search_accepts_min_score(self):
        r = self.client.get("/search", params={"q": "alpha beta", "min_score": 0.5})
        self.assertEqual(r.status_code, 200, r.text)
        for hit in r.json()["results"]:
            if hit["vector_score"] is not None:
                self.assertGreaterEqual(hit["vector_score"], 0.5)

    def test_min_score_bounds_validated(self):
        r = self.client.get("/search", params={"q": "alpha", "min_score": 1.5})
        self.assertEqual(r.status_code, 422)
        r = self.client.get("/search", params={"q": "alpha", "min_score": -1.5})
        self.assertEqual(r.status_code, 422)
        r = self.client.get("/search", params={"q": "alpha", "min_score": 1.0})
        self.assertEqual(r.status_code, 200)
        r = self.client.get("/search", params={"q": "alpha", "min_score": "abc"})
        self.assertEqual(r.status_code, 422)

    def test_min_score_in_openapi(self):
        spec = self.client.get("/openapi.json").json()
        names = [p["name"] for p in spec["paths"]["/search"]["get"]["parameters"]]
        self.assertIn("min_score", names)

    def test_min_score_participates_in_cache_key(self):
        # Within the TTL window: an unfloored page must never be served for a
        # floored request (and vice versa) — min_score must be in the key.
        unfloored = self.client.get(
            "/search", params={"q": "alpha beta", "mode": "semantic"}).json()
        self.assertGreater(len(unfloored["results"]), 0)
        top = max(h["vector_score"] for h in unfloored["results"])
        # a floor strictly above every cosine, within the API's [-1, 1] bound
        floor = min(1.0, top + 0.05)
        floored = self.client.get(
            "/search", params={"q": "alpha beta", "mode": "semantic",
                               "min_score": floor}).json()
        self.assertLess(len(floored["results"]), len(unfloored["results"]),
                        f"floor {floor} must drop hits (top cosine {top})")
        again = self.client.get(
            "/search", params={"q": "alpha beta", "mode": "semantic"}).json()
        self.assertEqual(len(again["results"]), len(unfloored["results"]),
                        "unfloored request must not inherit the floored page")

    def test_raw_scores_survive_rerank_on_the_wire(self):
        r = self.client.get("/search", params={"q": "alpha beta", "rerank": "true"})
        self.assertEqual(r.status_code, 200, r.text)
        results = r.json()["results"]
        self.assertTrue(results)
        for hit in results:
            # raw per-retriever scores are readable after the rerank, exactly
            # what the Phase 7 refuse-gate needs
            self.assertIn("bm25_score", hit)
            self.assertIn("vector_score", hit)
        fused = {h["doc_id"]: (h["bm25_score"], h["vector_score"])
                 for h in self.client.get(
                     "/search", params={"q": "alpha beta"}).json()["results"]}
        reranked = {h["doc_id"]: (h["bm25_score"], h["vector_score"]) for h in results}
        for doc_id in reranked:
            self.assertEqual(reranked[doc_id], fused[doc_id],
                             f"raw scores changed for {doc_id} after rerank")


if __name__ == "__main__":
    unittest.main()
