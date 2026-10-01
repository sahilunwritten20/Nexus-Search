"""WP4 / BUG-06 regression tests: ONE shared HybridSearch serves the API.

Pre-fix, /search constructed a fresh HybridSearch per request, discarding
the BM25 doc-token memo (bounded 2048, keyed (doc_id, added_at)) across
requests — the memo the code comments celebrated only helped within a
single request. Per-request fusion weights now travel as search_page
arguments on the shared instance.
"""
import importlib
import os
import shutil
import tempfile
import threading
import time
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.core.hybrid_search import HybridSearch, SearchMode  # noqa: E402
from nexus_search.core.indexer import Indexer  # noqa: E402
from nexus_search.core.storage import Storage  # noqa: E402


class TestWeightOverrideEquivalence(unittest.TestCase):
    """search_page(bm25_weight=..., vector_weight=...) on a default instance
    must be exactly equivalent to a purpose-constructed instance."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "w.db")
        self.storage = Storage(self.db)
        ix = Indexer(self.storage)
        ix.add_document("a", "apple banana fruit", title="A")
        ix.add_document("b", "banana cherry pie", title="B")
        ix.add_document("c", "unrelated content entirely", title="C")
        self.shared = HybridSearch(self.storage, db_path=self.db)  # 1.0/1.0

    def tearDown(self):
        self.shared.close()
        self.storage.close()
        for _ in range(10):
            try:
                shutil.rmtree(self.dir)
                break
            except PermissionError:
                time.sleep(0.05)

    def _strip(self, results):
        return [(r.doc_id, round(r.score, 9)) for r in results]

    def test_override_equals_constructed_instance(self):
        dedicated = HybridSearch(self.storage, bm25_weight=0.2, vector_weight=0.9,
                                  db_path=self.db)
        try:
            shared_page = self.shared.search_page(
                "banana", mode=SearchMode.HYBRID, fusion="weighted",
                bm25_weight=0.2, vector_weight=0.9)
            dedicated_page = dedicated.search_page(
                "banana", mode=SearchMode.HYBRID, fusion="weighted")
            self.assertEqual(self._strip(shared_page.results),
                             self._strip(dedicated_page.results))
        finally:
            dedicated.close()

    def test_constructor_weights_untouched_by_override(self):
        self.shared.search_page("banana", mode=SearchMode.HYBRID,
                                 bm25_weight=0.0, vector_weight=1.0)
        self.assertEqual((self.shared.bm25_weight, self.shared.vector_weight),
                         (1.0, 1.0), "override must not mutate the shared instance")

    def test_invalid_override_rejected(self):
        with self.assertRaises(ValueError):
            self.shared.search_page("banana", bm25_weight=-1.0)
        with self.assertRaises(ValueError):
            self.shared.search_page("banana", bm25_weight=0.0, vector_weight=0.0)

    def test_explain_honors_override(self):
        a = self.shared.explain("banana", mode=SearchMode.HYBRID,
                                 bm25_weight=1.0, vector_weight=0.0)
        self.assertTrue(all(r["vector_normalized"] in (None, 0.0) for r in a["results"]))


class TestSharedInstanceConcurrency(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "c.db")
        self.storage = Storage(self.db)
        ix = Indexer(self.storage)
        for i in range(40):
            ix.add_document(f"d{i}", f"shared search stress document {i}",
                            title=f"D{i}")
        self.hybrid = HybridSearch(self.storage, db_path=self.db)

    def tearDown(self):
        self.hybrid.close()
        self.storage.close()
        for _ in range(10):
            try:
                shutil.rmtree(self.dir)
                break
            except PermissionError:
                time.sleep(0.05)

    def test_concurrent_mixed_weight_searches(self):
        errors = []
        per_worker = {}  # worker idx -> [(doc_ids...)] ; appended by that worker only

        def worker(idx):
            try:
                pages = per_worker.setdefault(idx, [])
                for _ in range(5):
                    page = self.hybrid.search_page(
                        "shared search", top_k=5, mode=SearchMode.HYBRID,
                        bm25_weight=idx % 2, vector_weight=(idx + 1) % 2 or 1)
                    pages.append(tuple(r.doc_id for r in page.results))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"worker {idx}: {exc!r}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])
        # deterministic ranking: a worker's own 5 pages (same weights) must
        # be identical to each other, under concurrency.
        for idx, pages in per_worker.items():
            self.assertEqual(len(set(pages)), 1,
                             f"worker {idx}: same query+weights ranked differently")


try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class TestApiSharedSearcher(unittest.TestCase):
    def setUp(self):
        import importlib
        self.tmpdir = tempfile.mkdtemp()
        self._saved = {k: os.environ.get(k) for k in
                       ("NEXUS_ENV", "NEXUS_API_KEY", "NEXUS_DB",
                        "NEXUS_RATE_LIMIT", "NEXUS_CACHE_TTL")}
        os.environ["NEXUS_ENV"] = "dev"
        os.environ.pop("NEXUS_API_KEY", None)
        os.environ["NEXUS_RATE_LIMIT"] = "10000/minute"
        os.environ["NEXUS_CACHE_TTL"] = "0"
        os.environ["NEXUS_DB"] = os.path.join(self.tmpdir, "api.db")
        from nexus_search.core import api
        self.api = importlib.reload(api)
        self.client = TestClient(self.api.app)
        self.client.post("/documents/bulk", json={"documents": [
            {"doc_id": f"d{i}", "content": f"memo persist probe {i} phrase text",
             "title": f"D{i}"} for i in range(12)]})

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

    def test_token_memo_persists_across_requests(self):
        # phrase/boolean gating tokenizes candidate docs through the memo;
        # a per-request HybridSearch (pre-fix) started every request cold.
        q = '"persist probe" memo'
        self.client.get("/search", params={"q": q, "mode": "hybrid"})
        cache = self.api._hybrid_searcher.bm25_search._token_cache
        self.assertGreater(len(cache), 0, "memo must be populated after a search")
        before = dict(cache)
        self.client.get("/search", params={"q": q, "mode": "hybrid"})
        # second request served by the SAME instance: cache entries unchanged
        self.assertEqual(len(cache), len(before))
        for key in before:
            self.assertIn(key, cache)

    def test_per_request_weights_still_honored(self):
        r1 = self.client.get("/search", params={
            "q": "memo phrase", "mode": "hybrid", "fusion": "weighted",
            "bm25_weight": 1.0, "vector_weight": 0.0})
        r2 = self.client.get("/search", params={
            "q": "memo phrase", "mode": "hybrid", "fusion": "weighted",
            "bm25_weight": 0.0, "vector_weight": 1.0})
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        m1 = r1.json()["metadata"]
        m2 = r2.json()["metadata"]
        self.assertEqual(m1["bm25_candidates"], 12)
        self.assertEqual(m1["vector_candidates"], 0)
        self.assertEqual(m2["bm25_candidates"], 0)
        self.assertGreater(m2["vector_candidates"], 0)


if __name__ == "__main__":
    unittest.main()
