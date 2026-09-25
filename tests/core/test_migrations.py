"""Tests for the versioned migration runner (core/migrations.py)."""
import os
import shutil
import sqlite3
import tempfile
import time
import unittest

from nexus_search.core.migrations import apply_migrations, get_version
from nexus_search.core.storage import Storage

V1 = "CREATE TABLE IF NOT EXISTS t (id TEXT PRIMARY KEY)"
V2 = "ALTER TABLE t ADD COLUMN extra TEXT"


class TestMigrations(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "m.db")

    def tearDown(self):
        for _ in range(10):
            try:
                shutil.rmtree(self.dir)
                break
            except PermissionError:
                time.sleep(0.05)

    def _conn(self):
        return sqlite3.connect(self.path)

    def test_fresh_db_applies_all(self):
        conn = self._conn()
        v = apply_migrations(conn, "store", [(1, V1), (2, V2)])
        self.assertEqual(v, 2)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(t)").fetchall()}
        self.assertEqual({"id", "extra"}, cols)
        self.assertEqual(get_version(conn, "store"), 2)
        conn.close()

    def test_reopen_is_idempotent(self):
        conn = self._conn()
        apply_migrations(conn, "store", [(1, V1)])
        conn.close()
        conn = self._conn()
        # add a second migration later — only the pending one runs
        v = apply_migrations(conn, "store", [(1, V1), (2, V2)])
        self.assertEqual(v, 2)
        # running again changes nothing and does not error
        v = apply_migrations(conn, "store", [(1, V1), (2, V2)])
        self.assertEqual(v, 2)
        conn.close()

    def test_failed_migration_rolls_back_and_reraises(self):
        conn = self._conn()
        apply_migrations(conn, "store", [(1, V1)])
        with self.assertRaises(sqlite3.Error):
            # v2 references a nonexistent table -> must fail cleanly
            apply_migrations(conn, "store",
                             [(1, V1), (2, "INSERT INTO nope VALUES (1)")])
        self.assertEqual(get_version(conn, "store"), 1)  # not half-bumped
        conn.close()

    def test_independent_store_versions(self):
        conn = self._conn()
        apply_migrations(conn, "a", [(1, V1)])
        apply_migrations(conn, "b", [(1, V1), (2, V2)])
        self.assertEqual((get_version(conn, "a"), get_version(conn, "b")), (1, 2))
        conn.close()

    def test_storage_reports_its_version(self):
        s = Storage(self.path)
        self.assertEqual(s.schema_version, 1)
        self.assertEqual(get_version(s.conn, "storage"), 1)
        # existing behavior intact
        s.upsert_document("d", "t", "c", "text", 1, {})
        self.assertEqual(s.document_count(), 1)
        s.close()

    def test_storage_reopen_keeps_data_and_version(self):
        s = Storage(self.path)
        # upsert_document_with_postings commits; bare upsert_document is the
        # low-level primitive the indexer wraps in its own transaction.
        s.upsert_document_with_postings("d", "t", "c", "text", 1, {}, {"c": 1})
        s.close()
        s = Storage(self.path)
        self.assertEqual(s.schema_version, 1)
        self.assertEqual(s.document_count(), 1)
        s.close()


if __name__ == "__main__":
    unittest.main()
