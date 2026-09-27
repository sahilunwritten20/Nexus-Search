"""VectorStore multi-instance consistency: a second process' commit must not
be silently dropped from another instance's in-memory matrix when that
instance adds its own vector on top of a stale view."""

import shutil
import tempfile
import time
import unittest

import numpy as np

from nexus_search.core.vector_store import VectorStore


class FakeEmbedder:
    """VectorStore only needs name/dim from the embedder for add/search."""

    name = "test-model"
    dim = 4

    def embed_query(self, text):
        vec = np.ones(self.dim, dtype=np.float32)
        return vec / np.linalg.norm(vec)


class TestVectorStoreMultiInstance(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = f"{self.tmpdir}/vectors.db"
        self.a = VectorStore(self.db_path, embedder=FakeEmbedder())
        self.b = VectorStore(self.db_path, embedder=FakeEmbedder())

    def tearDown(self):
        self.a.close()
        self.b.close()
        for _ in range(10):
            try:
                shutil.rmtree(self.tmpdir)
                return
            except PermissionError:
                time.sleep(0.05)

    def test_add_picks_up_other_instances_writes(self):
        vec_x = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        vec_y = np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)

        # A commits first; B's in-memory matrix predates that commit.
        self.a.add("doc_X", vec_x, "hashX")
        self.assertEqual(self.a.get_content_hash("doc_X"), "hashX")

        # B adds its own vector. With the stale view this used to leave B
        # holding only doc_Y in memory until some later reload.
        self.b.add("doc_Y", vec_y, "hashY")

        # Observe B's in-memory view DIRECTLY (count/get_content_hash do not
        # reload): this is what pre-fix silently dropped A's commit.
        self.assertEqual(self.b.count(), 2)
        self.assertEqual(self.b.get_content_hash("doc_X"), "hashX")
        self.assertEqual(self.b.get_content_hash("doc_Y"), "hashY")

        # And search — which reloads anyway — agrees from both instances.
        seen_b = {r.doc_id for r in self.b.search(vec_x, top_k=10)}
        self.assertEqual(seen_b, {"doc_X", "doc_Y"})

        # And a fresh search from A sees Y too.
        seen_a = {r.doc_id for r in self.a.search(vec_y, top_k=10)}
        self.assertEqual(seen_a, {"doc_X", "doc_Y"})

    def test_schema_versioned_like_other_stores(self):
        from nexus_search.core.migrations import get_version
        self.assertEqual(get_version(self.a.conn, "doc_vectors"), 1)
        # reopening an existing DB is a no-op, not a re-migration
        again = VectorStore(self.db_path, embedder=FakeEmbedder())
        try:
            self.assertEqual(get_version(again.conn, "doc_vectors"), 1)
        finally:
            again.close()

    def test_concurrent_add_remove_search_consistency(self):
        """Stress test: writers and readers sharing one store. Every returned
        doc_id must be one that was ever written, and every score must be a
        valid cosine similarity — no torn doc_id/vector pairings (previously
        possible: search read live arrays outside the matrix lock)."""
        import threading

        errors = []
        written = set()
        written_lock = threading.Lock()
        stop = threading.Event()
        vec = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)

        def writer():
            i = 0
            while not stop.is_set():
                doc_id = f"w{i % 25}"
                try:
                    self.a.add(doc_id, vec.copy(), f"h{i}")
                    with written_lock:
                        written.add(doc_id)
                    if i % 7 == 0:
                        self.a.remove(doc_id)
                except Exception as exc:  # noqa: BLE001 - leaks are the bug
                    errors.append(exc)
                i += 1

        def reader():
            while not stop.is_set():
                try:
                    for r in self.a.search(vec, top_k=50):
                        with written_lock:
                            if r.doc_id not in written:
                                errors.append(f"phantom id {r.doc_id}")
                        if not (-1.0001 <= r.score <= 1.0001):
                            errors.append(f"score out of range {r.score}")
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        threads = ([threading.Thread(target=writer, daemon=True) for _ in range(2)]
                   + [threading.Thread(target=reader, daemon=True) for _ in range(3)])
        for t in threads:
            t.start()
        time.sleep(2.0)
        stop.set()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(errors, [])

    def test_update_in_place_still_works(self):
        vec = np.array([1.0, 1.0, 0.0, 0.0], dtype=np.float32)
        self.a.add("doc_1", vec, "h1")
        newer = vec * 1.0001
        self.a.add("doc_1", newer, "h2")  # same doc_id: replace, not append
        self.assertEqual(self.a.count(), 1)
        self.assertEqual(self.a.get_content_hash("doc_1"), "h2")


if __name__ == "__main__":
    unittest.main()
