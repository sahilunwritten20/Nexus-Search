"""WP3 / BUG-03 + BUG-04 regression tests: truthful pagination + top_k.

Pre-fix, the fused candidate pool was capped by `candidates` (API default
50) regardless of the requested window:
- BUG-03: hybrid offset=50 with default candidates returned 0 results while
  total_results=120 and has_more=true (reproduced in the audit)
- BUG-04: top_k=100 silently returned 50 results

The fixed contract: the effective pool always covers offset+top_k, bounded
by NEXUS_MAX_CANDIDATES (default 1000); requests whose window exceeds that
bound get a clear 400 from the API; paging a corpus never yields an empty
page while has_more claims more.
"""
import importlib
import os
import shutil
import tempfile
import time
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.core.hybrid_search import HybridSearch, SearchMode  # noqa: E402
from nexus_search.core.indexer import Indexer  # noqa: E402
from nexus_search.core.storage import Storage  # noqa: E402

N_DOCS = 120
Q = "alpha beta gamma"


class _Corpus(unittest.TestCase):
    """120 identical-relevance docs — deterministic fused order, so page
    windows are exactly checkable. Embeddings are generated through the
    same EmbeddingSync the production paths use (semantic mode needs
    vectors; hybrid's fused order includes vector similarity)."""

    mode = SearchMode.HYBRID

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "p.db")
        self.storage = Storage(self.db)
        from nexus_search.core.embedding_sync import EmbeddingSync
        from nexus_search.core.vector_store import VectorStoreManager
        self.vs = VectorStoreManager(self.db)
        self.sync = EmbeddingSync(self.vs, batch_size=32)
        ix = Indexer(self.storage)
        self.sync.attach(ix)
        for i in range(N_DOCS):
            ix.add_document(f"d{i:03d}", f"{Q} document number {i}",
                            title=f"Doc {i:03d}")
        self.sync.flush()
        self.hybrid = HybridSearch(self.storage, vector_store=self.vs,
                                   db_path=self.db)

    def tearDown(self):
        self.hybrid.close()
        self.sync.close()
        self.vs.close()
        self.storage.close()
        for _ in range(10):
            try:
                shutil.rmtree(self.dir)
                break
            except PermissionError:
                time.sleep(0.05)

    def _walk(self, top_k, candidates=None, mode=None):
        mode = mode or self.mode
        seen, offset, pages = [], 0, []
        while True:
            kwargs = {} if candidates is None else {"candidates": candidates}
            page = self.hybrid.search_page(Q, top_k=top_k, offset=offset,
                                           mode=mode, **kwargs)
            pages.append((offset, len(page.results), page.total,
                          page.pool_size))
            if not page.results:
                break
            seen.extend(r.doc_id for r in page.results)
            offset += len(page.results)
            if offset >= N_DOCS or offset > 2000:
                break
        return seen, pages


class TestHybridPagination(_Corpus):
    mode = SearchMode.HYBRID

    def test_bug03_offset_50_returns_ranks_51_60(self):
        # with embeddings in play the fused order is similarity-tinted; the
        # CONTRACT is a full, disjoint window: exactly the docs at fused
        # positions 51..60, which a fresh full-pool query identifies.
        full = self.hybrid.search_page(Q, top_k=60, offset=0,
                                        mode=SearchMode.HYBRID, candidates=50)
        expected = [r.doc_id for r in full.results][50:60]
        page = self.hybrid.search_page(Q, top_k=10, offset=50,
                                       mode=SearchMode.HYBRID, candidates=50)
        self.assertEqual(len(page.results), 10,
                         "offset=50 with default candidates must return a full page")
        self.assertEqual([r.doc_id for r in page.results], expected)
        self.assertEqual(page.total, N_DOCS)

    def test_bug04_topk_100_returns_100(self):
        page = self.hybrid.search_page(Q, top_k=100, offset=0,
                                       mode=SearchMode.HYBRID, candidates=50)
        self.assertEqual(len(page.results), 100)

    def test_walking_all_pages_no_gaps_no_duplicates(self):
        seen, pages = self._walk(top_k=10, candidates=50)
        self.assertEqual(len(seen), N_DOCS)
        self.assertEqual(len(set(seen)), N_DOCS, "paging must not duplicate")
        for offset, got, total, pool in pages:
            self.assertGreater(got, 0, f"empty page at offset={offset} "
                                       f"with total={total}")
            self.assertEqual(total, N_DOCS)

    def test_semantic_mode_pages_truthfully(self):
        # explicit candidates >= corpus: semantic mode's result universe IS
        # the pool, so strict page stability requires a fixed pool (documented)
        seen, pages = self._walk(top_k=10, candidates=N_DOCS, mode=SearchMode.SEMANTIC)
        self.assertEqual(len(seen), N_DOCS)
        self.assertEqual(len(set(seen)), N_DOCS)
        for offset, got, total, pool in pages:
            self.assertGreater(got, 0, f"empty semantic page at offset={offset}")

    def test_keyword_mode_stays_native_offset(self):
        # keyword mode never builds a pool for plain queries: BM25 slices
        # offset itself (byte-identical pre-existing behavior)
        page = self.hybrid.search_page(Q, top_k=10, offset=50,
                                       mode=SearchMode.KEYWORD, candidates=50)
        self.assertEqual(len(page.results), 10)
        full = self.hybrid.search_page(Q, top_k=60, offset=0,
                                        mode=SearchMode.KEYWORD, candidates=50)
        self.assertEqual([r.doc_id for r in page.results],
                         [r.doc_id for r in full.results][50:60])


class TestCandidateCapBound(unittest.TestCase):
    """The pool is bounded by NEXUS_MAX_CANDIDATES (default 1000); windows
    beyond it are the caller's contract violation (the API returns 400)."""

    def test_env_cap_is_read(self):
        saved = os.environ.get("NEXUS_MAX_CANDIDATES")
        try:
            os.environ["NEXUS_MAX_CANDIDATES"] = "200"
            from nexus_search.core.hybrid_search import HybridSearch as HS
            # candidates=None -> default pool, grown only to cover the window
            self.assertEqual(HS._candidate_k(top_k=10, candidates=None, offset=150),
                             160)
            # explicit candidates below the window still cover it
            self.assertEqual(HS._candidate_k(top_k=10, candidates=50, offset=195),
                             200)
        finally:
            if saved is None:
                os.environ.pop("NEXUS_MAX_CANDIDATES", None)
            else:
                os.environ["NEXUS_MAX_CANDIDATES"] = saved

    def test_default_pool_covers_offset_plus_topk(self):
        from nexus_search.core.hybrid_search import HybridSearch as HS
        self.assertEqual(HS._candidate_k(top_k=10, candidates=50, offset=95),
                         105)
        self.assertEqual(HS._candidate_k(top_k=100, candidates=50, offset=0),
                         100)


try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class TestApiWindowContract(unittest.TestCase):
    def setUp(self):
        import importlib
        self.tmpdir = tempfile.mkdtemp()
        self._saved = {k: os.environ.get(k) for k in
                       ("NEXUS_ENV", "NEXUS_API_KEY", "NEXUS_DB",
                        "NEXUS_RATE_LIMIT", "NEXUS_CACHE_TTL")}
        os.environ["NEXUS_ENV"] = "dev"
        os.environ.pop("NEXUS_API_KEY", None)
        # the fixture indexes 120 docs in setUp: the default 60/min limiter
        # would 429 half of them (that is the limiter working, not a bug)
        os.environ["NEXUS_RATE_LIMIT"] = "10000/minute"
        os.environ["NEXUS_CACHE_TTL"] = "0"
        os.environ["NEXUS_DB"] = os.path.join(self.tmpdir, "api.db")
        from nexus_search.core import api
        self.api = importlib.reload(api)
        self.client = TestClient(self.api.app)
        self.client.post("/documents/bulk", json={"documents": [
            {"doc_id": f"d{i:03d}", "content": f"{Q} document number {i}",
             "title": f"Doc {i:03d}"}
            for i in range(N_DOCS)]})

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        from nexus_search.core import api
        importlib.reload(api)
        for _ in range(10):
            try:
                shutil.rmtree(self.tmpdir)
                break
            except PermissionError:
                time.sleep(0.05)

    def test_api_offset_beyond_cap_is_400(self):
        r = self.client.get("/search", params={
            "q": Q, "offset": 10000 - 100, "top_k": 200})
        # default cap 1000: offset+top_k = 9900+... wait: 9900+200 > 1000 -> 400
        self.assertEqual(r.status_code, 400)
        self.assertIn("NEXUS_MAX_CANDIDATES", r.json()["detail"])

    def test_api_offset_50_full_page_and_truthful_more(self):
        # NOTE: unlike the lib-level test, the API path HAS embeddings (hash
        # embedder), so fused order is similarity-tinted — assert the page
        # contract, not exact ids.
        r = self.client.get("/search", params={
            "q": Q, "offset": 50, "top_k": 10, "mode": "hybrid"})
        body = r.json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(body["results"]), 10,
                         "a full page must come back at offset=50 (BUG-03)")
        self.assertEqual(body["total_results"], N_DOCS)
        self.assertTrue(body["metadata"]["has_more"])

    def test_api_walking_all_pages_tiles_the_corpus(self):
        seen, offset = [], 0
        while True:
            r = self.client.get("/search", params={
                "q": Q, "offset": offset, "top_k": 10, "mode": "hybrid"})
            body = r.json()
            self.assertEqual(r.status_code, 200)
            if not body["results"]:
                break
            seen.extend(x["doc_id"] for x in body["results"])
            offset += len(body["results"])
            self.assertLess(offset, 500, "walk must terminate")
        self.assertEqual(len(seen), N_DOCS)
        self.assertEqual(len(set(seen)), N_DOCS,
                         "walking pages must tile the corpus without gaps/dups")

    def test_api_topk_100_returns_100(self):
        r = self.client.get("/search", params={
            "q": Q, "top_k": 100, "mode": "hybrid"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["results"]), 100)


if __name__ == "__main__":
    unittest.main()
