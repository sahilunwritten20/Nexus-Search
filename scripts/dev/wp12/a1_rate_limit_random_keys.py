"""WP12 A1 — P0-3 verification over real HTTP.

Boots the API (uvicorn subprocess) with a configured key, then fires 70
requests from one IP, each with a DIFFERENT random X-API-Key value.

Expected post-fix behavior:
  - random (unverifiable) keys all fall into the ONE per-IP bucket
  - requests beyond the 60/minute budget get 429
  - the VERIFIED key gets its own bucket and is never blocked by the
    attacker's traffic
"""
import httpx
import os
import random
import string
import subprocess
import sys
import time

PORT = 8931
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
        "NEXUS_API_KEY": "wp12-real-key",
        "NEXUS_DB": os.path.abspath("wp12_a1_test.db"),
        "NEXUS_EMBEDDER": "hash:8",
        "NEXUS_CACHE_TTL": "0",
        "NEXUS_RATE_LIMIT": "60/minute",
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
            statuses = []
            for i in range(70):
                bogus = "".join(random.choices(string.ascii_letters, k=20))
                r = client.get("/search", params={"q": "w", "mode": "keyword"},
                               headers={"X-API-Key": bogus})
                statuses.append(r.status_code)
            n429 = statuses.count(429)
            print(f"70 requests, each with a different random key: "
                  f"429 count = {n429} (last 10 statuses: {statuses[-10:]})")
            # The VERIFIED key has its own bucket: even after the IP bucket
            # is exhausted, it must still be served.
            r_ok = client.get("/search", params={"q": "w", "mode": "keyword"},
                              headers={"X-API-Key": "wp12-real-key"})
            print(f"verified key right after: {r_ok.status_code}")
            if n429 >= 1 and r_ok.status_code == 200:
                print("A1: PASS (P0-3 fix works over HTTP)")
                return 0
            print("A1: FAIL")
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
