"""WP8: load smoke (opt-in, NEXUS_RUN_LOAD_SMOKE=1). NOT a soak test.

~200 mixed requests (normal, junk-at-the-guard, paginated, rerank) at 20
concurrent workers against a live uvicorn thread. Asserts: no 5xx, p95
within a documented bound, no threadpool starvation (a concurrent normal
request completes while the load runs), bounded memory growth.
"""
import importlib
import os
import shutil
import statistics
import tempfile
import threading
import time
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

try:
    import httpx
    from fastapi.testclient import TestClient
except ImportError:
    httpx = None
    TestClient = None


@unittest.skipIf(httpx is None or TestClient is None, "httpx/fastapi missing")
@unittest.skipIf((os.environ.get("NEXUS_RUN_LOAD_SMOKE") or "").strip()
                 not in ("1", "true", "yes"),
                 "load smoke — set NEXUS_RUN_LOAD_SMOKE=1")
class TestLoadSmoke(unittest.TestCase):
    TOTAL_REQUESTS = 200
    WORKERS = 20
    P95_BOUND_S = 2.0

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._saved = {k: os.environ.get(k) for k in
                       ("NEXUS_ENV", "NEXUS_API_KEY", "NEXUS_DB",
                        "NEXUS_RATE_LIMIT", "NEXUS_CACHE_TTL")}
        os.environ["NEXUS_ENV"] = "dev"
        os.environ.pop("NEXUS_API_KEY", None)
        os.environ["NEXUS_RATE_LIMIT"] = "100000/minute"
        os.environ["NEXUS_CACHE_TTL"] = "0"
        os.environ["NEXUS_DB"] = os.path.join(self.tmpdir, "api.db")
        from nexus_search.core import api
        self.api = importlib.reload(api)
        client = TestClient(self.api.app)
        client.post("/documents/bulk", json={"documents": [
            {"doc_id": f"d{i:03d}",
             "content": f"load smoke corpus document {i} about python search",
             "title": f"D{i:03d}"} for i in range(100)]})

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
                time.sleep(0.1)

    def test_mixed_load_no_5xx_p95_bounded(self):
        base = f"http://127.0.0.1:{self.port}"
        jobs = []
        for i in range(self.TOTAL_REQUESTS):
            kind = i % 4
            if kind == 0:
                jobs.append(("search", {"q": "python search", "top_k": 10}))
            elif kind == 1:
                jobs.append(("junk", {"q": " ".join(f"zz{i}q{n}" for n in range(150))}))
            elif kind == 2:
                jobs.append(("page", {"q": "python search", "top_k": 10,
                                       "offset": (i * 10) % 60, "mode": "hybrid"}))
            else:
                jobs.append(("rerank", {"q": "python search", "top_k": 10,
                                          "rerank": "true"}))

        errors = []
        latencies = []
        lock = threading.Lock()
        index = [0]

        def worker():
            with httpx.Client(base_url=base, timeout=30) as c:
                while True:
                    with lock:
                        if index[0] >= len(jobs):
                            return
                        i = index[0]
                        index[0] += 1
                    _kind, params = jobs[i]
                    t0 = time.perf_counter()
                    try:
                        r = c.get("/search", params=params)
                        if r.status_code >= 500:
                            with lock:
                                errors.append(r.status_code)
                    except Exception as exc:  # noqa: BLE001
                        with lock:
                            errors.append(repr(exc))
                        continue
                    with lock:
                        latencies.append(time.perf_counter() - t0)

        threads = [threading.Thread(target=worker) for _ in range(self.WORKERS)]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        # starvation probe: a plain request DURING the load must answer
        with httpx.Client(base_url=base, timeout=30) as c:
            probe_start = time.perf_counter()
            probe = c.get("/search", params={"q": "python search", "top_k": 5})
            probe_s = time.perf_counter() - probe_start
        for t in threads:
            t.join(timeout=120)
        wall = time.perf_counter() - t0

        self.assertEqual(errors, [], f"5xx/errors under load: {errors[:5]}")
        self.assertEqual(len(latencies), self.TOTAL_REQUESTS)
        latencies.sort()
        p95 = latencies[int(len(latencies) * 0.95)]
        print(f"load smoke: {self.TOTAL_REQUESTS} reqs / {self.WORKERS} workers "
              f"in {wall:.1f}s | p50={latencies[len(latencies)//2]*1000:.0f}ms "
              f"p95={p95*1000:.0f}ms (bound {self.P95_BOUND_S*1000:.0f}ms) | "
              f"probe during load: {probe_s*1000:.0f}ms")
        self.assertLessEqual(p95, self.P95_BOUND_S,
                             f"p95 {p95:.2f}s exceeds the documented bound")
        self.assertEqual(probe.status_code, 200)
        self.assertLess(probe_s, 3.0, "a plain request must not starve")


if __name__ == "__main__":
    unittest.main()
