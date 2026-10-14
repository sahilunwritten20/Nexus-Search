"""Vector-store correctness vs brute-force ground truth (mission B4).

VectorStore.search is EXACT (full matmul + argpartition): its top-k
must be identical to a brute-force ranking over the same vectors —
recall@k = 1.0 by construction, including under allowed_ids filtering,
min_score floors, and tie-heavy random vectors. Any deviation is a bug
in the store's selection logic.
"""
import os
import tempfile
import unittest

import numpy as np

from nexus_search.core.vector_store import VectorStore


def _unit(v):
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


class TestVectorSearchExactness(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._stores = []

    def tearDown(self):
        for store in self._stores:
            store.close()
        os.remove(self.path)

    def _store(self, dim):
        store = VectorStore(self.path, embedder=_EmbedderStub(dim))
        self._stores.append(store)
        return store

    def _populate(self, store, n, dim, seed):
        rng = np.random.RandomState(seed)
        ids = [f"d{i:04d}" for i in range(n)]
        vecs = [_unit(rng.normal(size=dim).astype(np.float32))
                for _ in range(n)]
        for doc_id, v in zip(ids, vecs):
            store.add(doc_id, v, content_hash=doc_id)
        return ids, vecs

    def _brute_top_k(self, vecs, ids, q, k, min_score=-1.0, allowed=None):
        scores = np.array([float(np.dot(v, q)) for v in vecs])
        pairs = [(ids[i], scores[i]) for i in range(len(ids))
                 if (allowed is None or ids[i] in allowed)
                 and scores[i] >= min_score]
        pairs.sort(key=lambda p: (-p[1], p[0]))
        return pairs[:k]

    def test_recall_at_k_is_exact(self):
        store = self._store(32)
        ids, vecs = self._populate(store, 120, 32, seed=7)
        rng = np.random.RandomState(99)
        for trial in range(10):
            q = _unit(rng.normal(size=32).astype(np.float32))
            got = store.search(q, top_k=10)
            expected = self._brute_top_k(vecs, ids, q, 10)
            self.assertEqual([r.doc_id for r in got],
                             [d for d, _ in expected],
                             f"trial {trial}: store top-k != brute-force top-k")

    def test_allowed_ids_filter_is_exact(self):
        store = self._store(16)
        ids, vecs = self._populate(store, 80, 16, seed=3)
        rng = np.random.RandomState(4)
        allowed = set(ids[i] for i in rng.choice(80, size=30, replace=False))
        q = _unit(rng.normal(size=16).astype(np.float32))
        got = store.search(q, top_k=10, allowed_ids=allowed)
        expected = self._brute_top_k(vecs, ids, q, 10, allowed=allowed)
        self.assertEqual([r.doc_id for r in got], [d for d, _ in expected])

    def test_min_score_floor_is_exact(self):
        store = self._store(16)
        ids, vecs = self._populate(store, 60, 16, seed=11)
        rng = np.random.RandomState(12)
        q = _unit(rng.normal(size=16).astype(np.float32))
        got = store.search(q, top_k=50, min_score=0.2)
        expected = self._brute_top_k(vecs, ids, q, 50, min_score=0.2)
        self.assertEqual([r.doc_id for r in got], [d for d, _ in expected])

    def test_ties_break_deterministically(self):
        # duplicated vectors produce score ties. The store's tie ORDER is
        # positional (stable argsort reversed = reverse insertion) — the
        # contract that matters is DETERMINISM and set-equality with the
        # brute force, since all tied items carry identical scores.
        store = self._store(8)
        v = _unit(np.ones(8, dtype=np.float32))
        for i in range(20):
            store.add(f"t{i:03d}", v.copy(), content_hash=f"t{i}")
        got1 = store.search(v, top_k=20)
        got2 = store.search(v, top_k=20)
        self.assertEqual([r.doc_id for r in got1], [r.doc_id for r in got2],
                         "repeated identical searches must agree exactly")
        self.assertEqual(sorted(r.doc_id for r in got1),
                         sorted(f"t{i:03d}" for i in range(20)))
        self.assertEqual(len({round(r.score, 12) for r in got1}), 1,
                         "all tied vectors must carry the same score")

    def test_k_larger_than_pool(self):
        store = self._store(8)
        ids, vecs = self._populate(store, 5, 8, seed=13)
        q = np.zeros(8, dtype=np.float32)
        got = store.search(q, top_k=100)
        self.assertEqual(len(got), 5)


class _EmbedderStub:
    """Minimal embedder stub: fixed dim (the store only needs .name and
    .dim for bookkeeping)."""

    name = "hash:8"

    def __init__(self, dim=8):
        self.dim = dim


if __name__ == "__main__":
    unittest.main()
