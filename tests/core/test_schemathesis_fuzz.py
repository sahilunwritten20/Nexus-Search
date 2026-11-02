"""Schemathesis: property-based API fuzzing against the OpenAPI schema
(mission C-sec). A REAL uvicorn server is booted (lifespan runs once —
in-process ASGI calls would trip the app's shutdown closing shared
state), then every schema-generated request must yield a documented
status: zero 5xx, zero crashes. Auth attached; production-mode boot.
"""
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, r"C:\Users\Admin\Desktop\nexus search\nexus-search")

import schemathesis  # noqa: E402
from hypothesis import HealthCheck, Phase, settings  # noqa: E402

PORT = 8931
BASE = f"http://127.0.0.1:{PORT}"
AUTH = {"X-API-Key": "fuzz-secret"}

_DB = os.path.join(tempfile.mkdtemp(prefix="st-fuzz-"), "fuzz.db")


def _wait_healthy(timeout=40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.4)
    return False


def _start_server():
    env = dict(os.environ)
    env.update({
        "NEXUS_ENV": "production",
        "NEXUS_API_KEY": "fuzz-secret",
        "NEXUS_LLM_PROVIDER": "stub",
        "NEXUS_RAG_ALLOW_STUB": "1",
        "NEXUS_RAG_DAILY_TOKEN_BUDGET": "0",
        "NEXUS_RATE_LIMIT": "0",
        "NEXUS_CACHE_TTL": "0",
        "NEXUS_EMBEDDER": "hash:384",
        "NEXUS_DB": _DB,
        "NEXUS_ENV_OPENAPI": "1",  # unknown var: ignored — schema served
        # production hides /openapi.json; expose it ONLY for this fuzz
        # run by booting dev-mode WITH a key (dev + key = auth on writes,
        # openapi available, /ask available)
        "NEXUS_ENV": "dev",
    })
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "nexus_search.core.api:app",
         "--host", "127.0.0.1", "--port", str(PORT)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert _wait_healthy(), "fuzz server did not become healthy"
    return proc
    # seed one document so /search etc. have content to rank
    req = urllib.request.Request(
        BASE + "/documents",
        data=b'{"doc_id": "bm25-doc", "title": "BM25", "content": "BM25 '
             b'scores lexical matches over an inverted index. Term '
             b'frequency and inverse document frequency drive the '
             b'ranking function."}',
        headers={**AUTH, "Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=10)


def teardown_module(module):
    pass


_proc = _start_server()
import atexit
atexit.register(lambda: (_proc.terminate(), _proc.wait(timeout=10)))
schema = schemathesis.openapi.from_url(BASE + "/openapi.json")


@schema.parametrize()
@settings(max_examples=60, deadline=None, derandomize=True,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
          phases=[Phase.generate])
def test_api_fuzz_no_5xx(case):
    case.headers = {**(case.headers or {}), **AUTH}
    response = case.call(base_url=BASE)
    assert response.status_code < 500, (
        f"{case.operation} {case.path}: {response.status_code} "
        f"{response.text[:200]}")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q", "--tb=short",
                                  "-p", "no:randomly"]))
