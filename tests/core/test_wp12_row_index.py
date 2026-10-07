"""WP12-B5 (audit R5a): VectorStore add/remove must be O(1) lookups via a
doc_id -> row-index dict — _merge_in_memory's per-add np.where scan over
the object-dtype doc_ids array was O(n) per add (O(n^2) bulk ingest).
"""

import os
import shutil
import tempfile
import time
import unittest

import numpy as np

from nexus_search.core.embedders import HashEmbedder
from nexus_search.core.vector_store import VectorStore

# Generous end-to-end bound for 10K single-row adds (the per-add SQLite
# commit alone is ~1-2 ms; measured 23-27 s with the OLD O(n) scan path,
# ~15-25 s with the dict — the algorithmic pin is the merge test below).
BOUND_SECONDS = 120.0
# _merge_in_memory in-place updates on a 10K-row matrix: the O(n) scan
# path measured ~1.2 ms/call (=> ~2.4 s for 2000 calls); the dict path is
# microseconds. 0.5 s is >2x headroom while failing the scan path.
MERGE_BOUND_SECONDS = 0.5


class TestRowIndexDict(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.store = VectorStore(os.path.join(self.tmpdir, "idx.db"),
                                 embedder=HashEmbedder(dim=64))

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _vec(self, i):
        v = np.zeros(64, dtype=np.float32)
        v[i % 64] = 1.0
        return v

    def test_merge_in_memory_is_o1_at_10k_rows(self):
        """The audit's measured quantity, isolated from SQL commit cost:
        in-place merges on a 10K-row matrix must not scan the doc_ids
        array per call."""
        from nexus_search.core.vector_store import VectorStoreManager
        mgr = VectorStoreManager(os.path.join(self.tmpdir, "idx.db"),
                                 embedder=HashEmbedder(dim=64))
        try:
            mgr.upsert_batch([(f"d{i}", f"t {i}", "", "") for i in range(10_000)])
            store = mgr.store
            self.assertEqual(store.count(), 10_000)
            vec = self._vec(3)
            t0 = time.perf_counter()
            with store.lock:
                with store._matrix_lock:
                    for i in range(2_000):
                        store._merge_in_memory(f"d{i}", vec, "h-new", "", "")
            dt = time.perf_counter() - t0
        finally:
            mgr.close()
        self.assertLess(dt, MERGE_BOUND_SECONDS,
                        f"2000 in-place merges on a 10K matrix took {dt:.2f}s "
                        f"— the doc_id lookup is O(n) per call (audit R5a)")

    def test_bulk_add_10k_within_generous_bound(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            self.store.add(f"doc{i}", self._vec(i), f"h{i}")
        dt = time.perf_counter() - t0
        self.assertEqual(self.store.count(), 10_000)
        self.assertLess(dt, BOUND_SECONDS,
                        f"10K adds took {dt:.1f}s — gross per-add regression "
                        f"(audit R5a)")

    def test_index_dict_invariant_across_ops(self):
        """_id_to_idx must stay a faithful row map through adds, in-place
        updates, swap-removes, and external-write reloads."""
        for i in range(50):
            self.store.add(f"d{i}", self._vec(i % 64), f"h{i}")
        # in-place update keeps the row position
        self.store.add("d7", self._vec(7), "h7-new")
        # swap-remove: last row moves into the hole, its map entry moves too
        self.store.remove("d3")
        self.store.remove("d10")
        with self.store._matrix_lock:
            ids = list(self.store._doc_ids)
            mapping = dict(self.store._id_to_idx)
        self.assertEqual(len(ids), len(mapping))
        for doc_id, idx in mapping.items():
            self.assertEqual(ids[idx], doc_id)
        # reload (fresh instance over the same DB) rebuilds the same map
        other = VectorStore(os.path.join(self.tmpdir, "idx.db"),
                            embedder=HashEmbedder(dim=64))
        try:
            with other._matrix_lock:
                ids2 = list(other._doc_ids)
                mapping2 = dict(other._id_to_idx)
            self.assertEqual(len(ids2), len(mapping2))
            for doc_id, idx in mapping2.items():
                self.assertEqual(ids2[idx], doc_id)
        finally:
            other.close()


if __name__ == "__main__":
    unittest.main()
