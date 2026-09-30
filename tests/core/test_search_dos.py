"""WP1 / BUG-01 API-level regression tests.

- A cache hit for a plain /search must not recompute query understanding.
- A flood of adversarial junk queries must not starve the worker pool: a
  normal request served DURING the flood answers in < 1 s. (Runs against a
  real uvicorn server thread so sync-endpoint threadpool behavior is the
  production one.)
"""
import importlib
import os
import shutil
import tempfile
import threading
import time
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None


def _boot_dev_api(tmpdir: str):
    """Reload the API module against a fresh temp DB in dev mode."""
    os.environ["NEXUS_ENV"] = "dev"
    os.environ.pop("NEXUS_API_KEY", None)
    os.environ["NEXUS_DB"] = os.path.join(tmpdir, "api.db")
    from nexus_search.core import api
    importlib.reload(api)
    return api


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class TestUnderstandingLazyOnCacheHit(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._saved = {k: os.environ.get(k) for k in
                       ("NEXUS_ENV", "NEXUS_API_KEY", "NEXUS_DB", "NEXUS_CACHE_TTL")}
        os.environ["NEXUS_CACHE_TTL"] = "30"
        self.api = _boot_dev_api(self.tmpdir)
        self.client = TestClient(self.api.app)
        self.client.post("/documents", json={
            "doc_id": "d1", "content": "python search engine document",
            "title": "Python Search"})

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        from nexus_search.core import api
        importlib.reload(api)  # back to a booting state for other tests
        for _ in range(10):
            try:
                shutil.rmtree(self.tmpdir)
                break
            except PermissionError:
                time.sleep(0.05)

    def test_cache_hit_does_not_recompute_understanding(self):
        calls = []
        real = self.api.understand_query

        def counting(query, storage=None, **kw):
            calls.append(query)
            return real(query, storage, **kw)

        self.api.understand_query = counting
        try:
            r1 = self.client.get("/search", params={"q": "python search"})
            r2 = self.client.get("/search", params={"q": "python search"})
        finally:
            self.api.understand_query = real
        self.assertEqual((r1.status_code, r2.status_code), (200, 200))
        self.assertEqual(len(calls), 1,
                         "cache hit must not recompute understand_query (BUG-01)")
        # and the second response is the same page
        self.assertEqual(r1.json()["results"], r2.json()["results"])


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class TestQueryWordGuard(unittest.TestCase):
    """MAX_QUERY_WORDS (128): absurd queries are rejected pre-parse with a
    clear 400 — the documented BUG-01 bound at the API boundary."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._saved = {k: os.environ.get(k) for k in
                       ("NEXUS_ENV", "NEXUS_API_KEY", "NEXUS_DB")}
        self.api = _boot_dev_api(self.tmpdir)
        self.client = TestClient(self.api.app)

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

    def test_over_word_bound_is_400(self):
        q = " ".join(f"word{i}" for i in range(129))
        r = self.client.get("/search", params={"q": q})
        self.assertEqual(r.status_code, 400)
        self.assertIn("words", r.json()["detail"])

    def test_at_word_bound_is_served(self):
        self.client.post("/documents", json={
            "doc_id": "d1", "content": "alpha beta gamma", "title": "ABG"})
        q = " ".join(f"word{i}" for i in range(127)) + " alpha"
        r = self.client.get("/search", params={"q": q})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["total_results"], 1)


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class TestJunkFloodDoesNotStarve(unittest.TestCase):
    """50 concurrent adversarial junk queries + a normal request that must
    answer in < 1 s. On the pre-fix code each junk query burned ~5-40 s CPU
    (see docs/baseline_repro.json); this is the acceptance test for WP1."""

    FLOOD = 50
    NORMAL_BOUND_S = 1.0

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._saved = {k: os.environ.get(k) for k in
                       ("NEXUS_ENV", "NEXUS_API_KEY", "NEXUS_DB",
                        "NEXUS_CACHE_TTL", "NEXUS_RATE_LIMIT")}
        os.environ["NEXUS_CACHE_TTL"] = "0"   # flood queries are unique anyway
        # a valid, effectively-unlimited string: slowapi cannot parse "0"
        # (it logs a config ERROR even though the limiter is disabled)
        os.environ["NEXUS_RATE_LIMIT"] = "10000/minute"
        self.api = _boot_dev_api(self.tmpdir)
        TestClient(self.api.app).post("/documents", json={
            "doc_id": "d1", "content": "python search engine document",
            "title": "Python Search"})

        import uvicorn
        self.server = uvicorn.Server(uvicorn.Config(
            self.api.app, host="127.0.0.1", port=0, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 15
        while not self.server.started and time.time() < deadline:
            time.sleep(0.05)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]

    def tearDown(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)
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

    def _junk(self, i: int) -> str:
        """The audit's exact attack shape: 2,000 chars of unique junk terms.
        ~150-170 words — deterministically past the documented
        MAX_QUERY_WORDS bound (128)."""
        q, n = "", 0
        while len(q) < 2000:
            q += f" zqvxj{i}x{n}"
            n += 1
        return q[:2000].strip()

    def _junk_under_guard(self, i: int) -> str:
        """100 junk words: passes the word guard, exercises the full
        bounded pipeline (parse cap, batched lookups, spell budget)."""
        return " ".join(f"qqzzx{i}w{n}" for n in range(100))

    def test_normal_request_survives_junk_flood(self):
        import httpx
        errors = []
        queries = [self._junk(i) for i in range(self.FLOOD)]  # built off the clock
        shared = httpx.Client(base_url=f"http://127.0.0.1:{self.port}",
                               timeout=30)  # one pool: measures the app, not client setup

        def flood(i):
            try:
                r = shared.get("/search", params={"q": queries[i]})
                if r.status_code >= 500:
                    errors.append(r.status_code)
                elif r.status_code != 400:
                    errors.append(f"expected 400 guard, got {r.status_code}")
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=flood, args=(i,), daemon=True)
                   for i in range(self.FLOOD)]
        for t in threads:
            t.start()
        # the acceptance: a normal request DURING the flood answers quickly
        t0 = time.perf_counter()
        with httpx.Client(base_url=f"http://127.0.0.1:{self.port}",
                          timeout=30) as c:
            r = c.get("/search", params={"q": "python search"})
        elapsed = time.perf_counter() - t0
        for t in threads:
            t.join(timeout=60)
        shared.close()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(errors, [], f"flood requests failed: {errors[:3]}")
        self.assertLess(elapsed, self.NORMAL_BOUND_S,
                        f"normal request took {elapsed:.2f}s during the flood")

    def test_under_guard_junk_flood_drains_without_5xx(self):
        """Junk that PASSES the word guard still runs the full pipeline —
        bounded, no 5xx, and the flood drains in a sane window."""
        import httpx
        errors = []
        queries = [self._junk_under_guard(i) for i in range(self.FLOOD)]
        shared = httpx.Client(base_url=f"http://127.0.0.1:{self.port}",
                              timeout=60)

        def flood(i):
            try:
                r = shared.get("/search", params={"q": queries[i]})
                if r.status_code >= 500:
                    errors.append(r.status_code)
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=flood, args=(i,), daemon=True)
                   for i in range(self.FLOOD)]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        elapsed = time.perf_counter() - t0
        shared.close()
        self.assertEqual(errors, [], f"under-guard flood failed: {errors[:3]}")
        self.assertLess(elapsed, 15.0,
                        f"under-guard junk flood took {elapsed:.1f}s to drain")


if __name__ == "__main__":
    unittest.main()
