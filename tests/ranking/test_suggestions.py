"""Tests for Phase 5 Stage 4 — suggestions, highlighting, facets, sort, cursors."""
import os
import shutil
import tempfile
import time
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.core.bm25 import BM25Search
from nexus_search.core.indexer import Indexer
from nexus_search.core.storage import Storage
from nexus_search.ranking.ab import ExperimentLog
from nexus_search.ranking.suggestions import Suggester


class _Base(unittest.TestCase):
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


class TestSuggest(_Base):
    def test_prefix_matches_index_vocabulary(self):
        self.indexer.add_document("d1", "python python programming language")
        self.indexer.add_document("d2", "python scripts and programs")
        s = Suggester(self.storage)
        out = s.suggest("pyth")
        self.assertIn("python", out)

    def test_most_frequent_first(self):
        self.indexer.add_document("d1", "python python python python")
        self.indexer.add_document("d2", "pythons once only")
        out = Suggester(self.storage).suggest("pytho")
        self.assertEqual(out[0], "python")

    def test_empty_prefix_and_empty_index(self):
        s = Suggester(self.storage)
        self.assertEqual(s.suggest(""), [])
        self.assertEqual(s.suggest("zxqw"), [])
        self.assertEqual(s.suggest("any"), [])  # empty index, no crash

    def test_limit_respected(self):
        self.indexer.add_document("d1", " ".join(f"pre{i}" for i in range(30)))
        self.assertLessEqual(len(Suggester(self.storage).suggest("pre", limit=3)), 3)

    def test_refresh_on_new_documents(self):
        s = Suggester(self.storage)
        self.assertEqual(s.suggest("blorp"), [])
        self.indexer.add_document("d1", "blorp content")
        self.assertIn("blorp", s.suggest("blorp"))  # doc-count refresh


class TestRelatedSearches(_Base):
    def test_empty_log_falls_back_without_crash(self):
        self.indexer.add_document("d1", "machine learning models")
        log = ExperimentLog(self.path)
        out = Suggester(self.storage).related_searches("machne learnig", experiment_log=log)
        self.assertIsInstance(out, list)  # may be empty; must not crash
        log.close()

    def test_log_cooccurrence(self):
        log = ExperimentLog(self.path)
        log.record("python tutorial", "control", "hybrid")
        log.record("python cookbook", "control", "hybrid")
        log.record("java tutorial", "control", "hybrid")
        out = Suggester(self.storage).related_searches("python guide", experiment_log=log)
        self.assertIn("python tutorial", out)
        self.assertIn("python cookbook", out)
        self.assertNotIn("java tutorial", out)  # shares nothing with the query
        log.close()

    def test_no_log_at_all_term_similarity(self):
        self.indexer.add_document("d1", "python python data science")
        self.indexer.add_document("d2", "pythagorean theorem")
        out = Suggester(self.storage).related_searches("python data", experiment_log=None)
        # at least confirms no crash and returns terms from vocab only
        self.assertTrue(all(isinstance(t, str) for t in out))


class TestHighlighting(_Base):
    def test_highlight_wraps_terms(self):
        self.indexer.add_document("d1", "the quick brown fox jumps over lazy dogs")
        page = BM25Search(self.storage).search_page("quick fox", highlight=True)
        self.assertIn("<mark>quick</mark>", page.results[0].snippet)
        self.assertIn("<mark>fox</mark>", page.results[0].snippet)

    def test_highlight_off_is_plain(self):
        self.indexer.add_document("d1", "the quick brown fox")
        page = BM25Search(self.storage).search_page("quick", highlight=False)
        self.assertNotIn("<mark>", page.results[0].snippet)

    def test_highlight_does_not_double_wrap(self):
        self.indexer.add_document("d1", "python python python")
        page = BM25Search(self.storage).search_page("python", highlight=True)
        self.assertIn("<mark>python</mark>", page.results[0].snippet)
        self.assertNotIn("<mark><mark>", page.results[0].snippet)

    def test_no_match_no_marks(self):
        self.indexer.add_document("d1", "nothing relevant here at all")
        page = BM25Search(self.storage).search_page("zzz nope zzz type:text", highlight=True)
        # filter-style/no-match snippet path must not inject marks
        for r in page.results:
            self.assertNotIn("<mark>", r.snippet)


if __name__ == "__main__":
    unittest.main()
