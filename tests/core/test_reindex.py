"""Tests for Phase 1 build-out #7 — shadow reindex (zero-downtime swap)."""
import os
import shutil
import sqlite3
import tempfile
import time
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.core.bm25 import BM25Search
from nexus_search.core.indexer import Indexer
from nexus_search.core.reindex import main, reindex_shadow
from nexus_search.core.storage import Storage


class TestShadowReindex(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "t.db")
        self.storage = Storage(self.path)
        self.indexer = Indexer(self.storage)

    def tearDown(self):
        self.storage.close()
        for _ in range(10):
            try:
                shutil.rmtree(self.dir)
                break
            except PermissionError:
                time.sleep(0.05)

    def test_rebuild_produces_searchable_postings(self):
        self.indexer.add_document("a", "alpha beta beta", title="A")
        self.indexer.add_document("b", "gamma", title="B")
        # corrupt the live postings to prove the rebuild fixes them
        self.storage.conn.execute("DELETE FROM postings WHERE term = 'beta'")
        self.storage.conn.commit()
        stats = reindex_shadow(self.path)
        self.assertEqual(stats["documents"], 2)
        page = BM25Search(self.storage).search_page("beta")
        self.assertEqual([r.doc_id for r in page.results], ["a"])  # rebuilt

    def test_swap_is_atomic_under_failure(self):
        """If the build blows up, the live postings table is untouched."""
        self.indexer.add_document("a", "alpha beta", title="A")
        before = self.storage.postings_for_term("alpha")
        self.assertTrue(before)

        # Break the shadow build: lock the file against the build connection.
        killer = sqlite3.connect(self.path)
        killer.execute("PRAGMA journal_mode = WAL")
        killer.execute("BEGIN EXCLUSIVE")
        try:
            with self.assertRaises(sqlite3.OperationalError):
                reindex_shadow(self.path)
        finally:
            killer.execute("ROLLBACK")
            killer.close()
        # The original postings must still be fully intact and servable.
        self.assertEqual(self.storage.postings_for_term("alpha"), before)
        page = BM25Search(self.storage).search_page("alpha")
        self.assertEqual([r.doc_id for r in page.results], ["a"])

    def test_replays_writes_made_during_build(self):
        """The audit bug: a doc committed WHILE the shadow table was being
        built used to be silently dropped by the swap. Now the swap replays
        the delta atomically. Coordinated via a tokenizer hook that blocks
        the build mid-flight — the DB behavior itself is untouched."""
        import threading
        from unittest.mock import patch

        from nexus_search.core import reindex as reindex_mod

        self.indexer.add_document("a", "alpha", title="A")
        self.indexer.add_document("b", "beta", title="B")

        other = Storage(self.path)  # the "live API" process
        build_started = threading.Event()
        write_done = threading.Event()
        real_tokenize = reindex_mod.tokenize

        def blocking_tokenize(text):
            build_started.set()
            write_done.wait(5)
            return real_tokenize(text)

        with patch.object(reindex_mod, "tokenize", blocking_tokenize):
            worker = threading.Thread(target=reindex_mod.reindex_shadow,
                                      args=(self.path,), daemon=True)
            worker.start()
            self.assertTrue(build_started.wait(5))
            # the "live API" process writes DURING the shadow build
            from nexus_search.core.indexer import Indexer as Ix
            Ix(other).add_document("c", "gamma delta", title="C")
            write_done.set()
            worker.join(15)
        self.assertFalse(worker.is_alive())
        other.close()

        # The racing write survived the swap.
        page = BM25Search(self.storage).search_page("gamma")
        self.assertEqual([r.doc_id for r in page.results], ["c"])

    def test_cli_requires_shadow_flag(self):
        self.indexer.add_document("a", "alpha", title="A")
        self.assertEqual(main(["--db", self.path]), 2)      # refuses in-place
        self.assertEqual(main(["--db", self.path, "--shadow"]), 0)
        self.assertEqual(main(["--db", os.path.join(self.dir, "nope.db")]), 1)


if __name__ == "__main__":
    unittest.main()
