"""WP12 R5b — instrumented: how long does a 50K-row _load_matrix hold
_matrix_lock (HEAD) vs the whole reload, and how long do concurrent
searches stall.

HEAD fills buffers INSIDE the lock; cb64c36 built them outside. This
script measures HEAD directly: reload wall time + max search latency of a
polling searcher thread during the reload.
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
vs = VectorStore(os.path.join(tempfile.mkdtemp(), "b.db"), embedder=emb)

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
vs._data_version = -1  # force one reload on next _maybe_reload

# --- instrument the matrix lock (how long the reload holds it) ---
class TimedMatrixLock:
    def __init__(self, inner):
        self.inner = inner
        self._depth = 0
        self._enter = None
        self.held = 0.0

    def acquire(self, blocking=True, timeout=-1):
        t0 = time.perf_counter()
        r = self.inner.acquire(blocking, timeout)
        if r:
            if self._depth == 0:
                self._enter = t0
            self._depth += 1
        return r

    def release(self):
        self._depth -= 1
        if self._depth == 0 and self._enter is not None:
            self.held += time.perf_counter() - self._enter
        return self.inner.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()

    def __getattr__(self, name):
        return getattr(self.inner, name)


vs._matrix_lock = TimedMatrixLock(vs._matrix_lock)

# searcher that does NOT trigger reloads itself: read-only probe
q = np.zeros(emb.dim, dtype=np.float32); q[1] = 1
lat = []
stop = threading.Event()


def searcher():
    while not stop.is_set():
        t0 = time.perf_counter()
        with vs._matrix_lock:
            if len(vs._matrix):
                _ = vs._matrix @ q
        lat.append((time.perf_counter() - t0) * 1000)


t = threading.Thread(target=searcher, daemon=True)
t.start()
time.sleep(0.2)

t0 = time.perf_counter()
vs._maybe_reload()  # full reload from the main thread
reload_ms = (time.perf_counter() - t0) * 1000
time.sleep(0.2)
stop.set(); t.join()

base = sorted(lat)[: max(1, len(lat) // 10)]
print(f"reload wall time (50K rows):        {reload_ms:8.1f} ms")
print(f"matrix-lock HELD during reload:    {vs._matrix_lock.held*1000:8.1f} ms")
print(f"searcher median latency:           {base[len(base)//2]:8.3f} ms")
print(f"searcher PEAK latency in reload:   {max(lat):8.1f} ms")
vs.close()
