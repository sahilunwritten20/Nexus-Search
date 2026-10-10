"""WP14-7 — vector_store.py was 84%-covered (min_score path closed by
WP14-1's tests); these pin the remaining real gaps: legacy-table drop on
first open, and the create_vector_store factory. The _load_matrix
contention-fallback loop (lines 200-219) is a deliberately-rare race
branch — documented as not unit-testable without invasive mocks."""
import os
import sqlite3
import tempfile
import unittest

from nexus_search.core.vector_store import VectorStore, VectorStoreManager, create_vector_store
from nexus_search.core.embedders import get_embedder


class TestLegacyTablesDropped(unittest.TestCase):
    def test_legacy_embeddings_and_vectors_tables_dropped_on_open(self):
        db = os.path.join(tempfile.mkdtemp(prefix="wp14_vs_"), "v.db")
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE embeddings (doc_id TEXT)")
        conn.execute("CREATE TABLE vectors (doc_id TEXT)")
        conn.commit()
        conn.close()
        vs = VectorStore(db)
        try:
            check = sqlite3.connect(db)
            tables = {r[0] for r in check.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            check.close()
            self.assertNotIn("embeddings", tables)
            self.assertNotIn("vectors", tables)
            self.assertIn("doc_vectors", tables)
        finally:
            vs.close()

    def test_opening_a_clean_db_twice_is_idempotent(self):
        db = os.path.join(tempfile.mkdtemp(prefix="wp14_vs_"), "v2.db")
        vs1 = VectorStore(db)
        vs1.close()
        vs2 = VectorStore(db)   # second open: no legacy, no error
        vs2.close()


class TestFactory(unittest.TestCase):
    def test_create_vector_store_returns_manager_wired_to_db(self):
        db = os.path.join(tempfile.mkdtemp(prefix="wp14_vs_"), "v3.db")
        mgr = create_vector_store(db)
        try:
            self.assertIsInstance(mgr, VectorStoreManager)
            self.assertEqual(mgr.store.db_path, db)
        finally:
            mgr.close()


class TestEmbedderWiring(unittest.TestCase):
    def test_store_uses_ambient_embedder(self):
        db = os.path.join(tempfile.mkdtemp(prefix="wp14_vs_"), "v4.db")
        emb = get_embedder()
        vs = VectorStore(db, embedder=emb)
        try:
            self.assertEqual(vs.model, emb.name)
            self.assertEqual(vs.dim, emb.dim)
        finally:
            vs.close()


if __name__ == "__main__":
    unittest.main()
