"""WP12 A2 — P0-1 verification in a FRESH process.

Boots the API (uvicorn subprocess) with:
    NEXUS_ENV=production NEXUS_API_KEY=k NEXUS_REQUIRE_AUTH_FOR_READS=1

Expected behavior: /search returns 401 without a key, 200 with the
correct key (reads are opt-in gated, writes always gated).
"""
import httpx
import os
import subprocess
import sys
import time

PORT = 8932
BASE = f"http://127.0.0.1:{PORT}"


def wait_ready(client):
    for _ in range(60):
        try:
            r = client.get("/health")
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def main():
    env = dict(os.environ)
    env.update({
        "NEXUS_ENV": "production",
        "NEXUS_API_KEY": "k",
        "NEXUS_REQUIRE_AUTH_FOR_READS": "1",
        "NEXUS_DB": os.path.abspath("wp12_a2_test.db"),
        "NEXUS_EMBEDDER": "hash:8",
        "NEXUS_CACHE_TTL": "0",
    })
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "nexus_search.core.api:app",
         "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        with httpx.Client(base_url=BASE, timeout=10) as client:
            if not wait_ready(client):
                print("SERVER NEVER BECAME READY")
                sys.exit(2)
            no_key = client.get("/search", params={"q": "w", "mode": "keyword"})
            bad_key = client.get("/search", params={"q": "w", "mode": "keyword"},
                                 headers={"X-API-Key": "wrong"})
            good_key = client.get("/search", params={"q": "w", "mode": "keyword"},
                                  headers={"X-API-Key": "k"})
            write_key = client.post("/documents", json={
                "doc_id": "d1", "content": "hello world", "title": "t"},
                headers={"X-API-Key": "k"})
            print(f"/search no key:        {no_key.status_code}")
            print(f"/search wrong key:     {bad_key.status_code}")
            print(f"/search correct key:   {good_key.status_code}")
            print(f"/documents w/ key:      {write_key.status_code}")
            if (no_key.status_code == 401 and bad_key.status_code == 401
                    and good_key.status_code == 200 and write_key.status_code == 201):
                print("A2: PASS (P0-1 read-gating works in a fresh process)")
                return 0
            print("A2: FAIL")
            return 1
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(env["NEXUS_DB"] + suffix)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
