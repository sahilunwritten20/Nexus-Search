"""WP12 R5 — (a) per-add O(n) doc_id scan; (b) search() stall during reload.

(a) _merge_in_memory does np.where(self._doc_ids == doc_id) on EVERY add:
    a bulk add of N docs costs O(N^2) doc_id comparisons. Timed at 2K,
    5K, 10K.
(b) _load_matrix fills the buffers INSIDE _matrix_lock at HEAD (P2-9),
    so a search() arriving during a reload stalls for the whole fill.
    Measures worst search latency during a forced reload at 50K rows.
"""
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))
os.environ["NEXUS_EMBEDDER"] = "hash:64"

import numpy as np  # noqa: E402
from nexus_search.core.vector_store import VectorStore  # noqa: E402
from nexus_search.core.embedders import get_embedder  # noqa: E402

emb = get_embedder()


def vec(i):
    x = np.zeros(emb.dim, dtype=np.float32)
    x[i % emb.dim] = 1
    return x


print("=== (a) bulk-add time vs N (per-add O(n) doc_id scan) ===")
for n in (2000, 5000, 10000):
    vs = VectorStore(os.path.join(tempfile.mkdtemp(), "a.db"), embedder=emb)
    t0 = time.perf_counter()
    for i in range(n):
        vs.add(f"doc{i}", vec(i), f"h{i}")
    dt = time.perf_counter() - t0
    print(f"N={n:6d} total={dt:7.2f}s per-add={dt/n*1000:6.2f} ms")
    vs.close()

print("=== (b) search() stall during forced reload at 50K rows ===")
vs = VectorStore(os.path.join(tempfile.mkdtemp(), "b.db"), embedder=emb)
vsm_cols = emb.dim
# Bulk-load 50K rows the fast way: direct SQL, then reload
import sqlite3  # noqa: E402
rows = []
for i in range(50_000):
    x = np.zeros(emb.dim, dtype=np.float32)
    x[i % emb.dim] = 1
    x /= np.linalg.norm(x)
    rows.append((f"doc{i}", "m", emb.dim, f"h{i}", x.tobytes(), 0.0))
with vs.lock:
    vs.conn.executemany(
        "INSERT OR REPLACE INTO doc_vectors (doc_id, model, dim, content_hash, vector, updated_at)"
        " VALUES (?,?,?,?,?,?)", rows)
    vs.conn.commit()
# bump data_version so _maybe_reload triggers on next search
vs._data_version = -1

q = vec(1)
latencies = []
stop = threading.Event()


def searcher():
    while not stop.is_set():
        t0 = time.perf_counter()
        vs.search(q, top_k=5)
        latencies.append((time.perf_counter() - t0) * 1000)


t = threading.Thread(target=searcher, daemon=True)
t.start()
time.sleep(0.3)                    # baseline samples
vs._maybe_reload()                 # the reload under observation
time.sleep(0.3)
stop.set(); t.join()
base = [x for x in latencies[:30]]
peak = max(latencies)
base_med = sorted(base)[len(base) // 2] if base else float("nan")
print(f"50K rows: median search={base_med:.2f} ms, PEAK search during reload={peak:.1f} ms "
      f"(ratio {peak/base_med:.0f}x)" if base else "no baseline samples")
vs.close()
