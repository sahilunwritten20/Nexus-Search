"""Tests for Phase 1 build-out #8 — query result cache (API layer)."""
import importlib
import os
import shutil
import tempfile
import time
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")
os.environ.setdefault("NEXUS_ENV", "dev")

from nexus_search.core.query_cache import QueryCache

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None


class TestQueryCacheUnit(unittest.TestCase):
    def test_get_set_roundtrip(self):
        c = QueryCache(ttl_seconds=60)
        c.set(("q",), {"x": 1})
        self.assertEqual(c.get(("q",)), {"x": 1})

    def test_miss(self):
        c = QueryCache(ttl_seconds=60)
        self.assertIsNone(c.get(("nope",)))

    def test_ttl_expires(self):
        c = QueryCache(ttl_seconds=0.01)
        c.set(("q",), 1)
        time.sleep(0.02)
        self.assertIsNone(c.get(("q",)))

    def test_ttl_zero_disables(self):
        c = QueryCache(ttl_seconds=0)
        c.set(("q",), 1)
        self.assertIsNone(c.get(("q",)))

    def test_evicts_oldest_when_full(self):
        c = QueryCache(ttl_seconds=60, max_entries=2)
        c.set(("a",), 1)
        c.set(("b",), 2)
        c.set(("c",), 3)  # evicts a
        self.assertIsNone(c.get(("a",)))
        self.assertEqual(c.get(("c",)), 3)

    def test_concurrent_get_set_never_raises(self):
        # FastAPI serves sync endpoints on a threadpool: eviction + expiry
        # racing each other must never KeyError/500 the request.
        import threading

        c = QueryCache(ttl_seconds=0.05, max_entries=8)
        errors = []

        def hammer(i):
            try:
                for n in range(400):
                    key = (f"q{i}-{n % 20}",)
                    if c.get(key) is None:
                        c.set(key, n)
                c.clear()
            except Exception as exc:  # noqa: BLE001 - any leak is the bug
                errors.append(exc)

        threads = [threading.Thread(target=hammer, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

    def test_clear(self):
        c = QueryCache()
        c.set(("q",), 1)
        c.clear()
        self.assertIsNone(c.get(("q",)))

    def test_stats(self):
        c = QueryCache()
        c.set(("q",), 1)
        c.get(("q",))
        c.get(("miss",))
        s = c.stats()
        self.assertEqual((s["hits"], s["misses"], s["entries"]), (1, 1, 1))


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class TestQueryCacheApi(unittest.TestCase):
    """HTTP-level: repeat query is cached; a write invalidates it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.environ["NEXUS_DB"] = os.path.join(self.dir, "a.db")
        from nexus_search.core import api
        importlib.reload(api)
        self.api = api
        self.client = TestClient(api.app)
        self.client.post("/documents", json={"doc_id": "a", "content": "alpha beta"})

    def tearDown(self):
        for _ in range(10):
            try:
                shutil.rmtree(self.dir)
                break
            except PermissionError:
                time.sleep(0.05)

    def test_repeat_query_served_from_cache(self):
        r1 = self.client.get("/search", params={"q": "alpha"}).json()
        r2 = self.client.get("/search", params={"q": "alpha"}).json()
        self.assertEqual(r1["results"], r2["results"])
        self.assertGreaterEqual(self.api._query_cache.stats()["hits"], 1)

    def test_write_invalidates_cache(self):
        r1 = self.client.get("/search", params={"q": "alpha"}).json()
        self.assertEqual(r1["total_results"], 1)
        self.client.post("/documents", json={"doc_id": "b", "content": "alpha new"})
        r2 = self.client.get("/search", params={"q": "alpha"}).json()
        self.assertEqual(r2["total_results"], 2)  # NOT the stale cached page

    def test_delete_invalidates_cache(self):
        self.client.get("/search", params={"q": "alpha"})
        self.client.delete("/documents/a")
        r = self.client.get("/search", params={"q": "alpha"}).json()
        self.assertEqual(r["total_results"], 0)

    def test_different_params_dont_collide(self):
        for i in range(3):
            self.client.post("/documents", json={"doc_id": f"m{i}", "content": "alpha more"})
        a = self.client.get("/search", params={"q": "alpha", "top_k": 1}).json()
        b = self.client.get("/search", params={"q": "alpha", "top_k": 3}).json()
        # distinct keys: different result lengths, no false cache hit
        self.assertNotEqual(len(a["results"]), len(b["results"]))
        self.assertEqual(len(b["results"]), 3)

    def test_same_effective_params_share_key(self):
        a = self.client.get("/search", params={"q": "alpha"}).json()
        b = self.client.get("/search", params={"q": "alpha", "offset": 0}).json()
        self.assertEqual(a["results"], b["results"])


if __name__ == "__main__":
    unittest.main()
