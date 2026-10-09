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

    def test_empty_log_object_still_uses_trigram_fallback(self):
        # regression: a real-but-empty ExperimentLog used to suppress the
        # trigram fallback entirely (returned [] instead of similar terms)
        self.indexer.add_document("d1", "python python data science")
        self.indexer.add_document("d2", "pythagorean theorem")
        log = ExperimentLog(self.path)
        with_log = Suggester(self.storage).related_searches("python data", experiment_log=log)
        without_log = Suggester(self.storage).related_searches("python data", experiment_log=None)
        self.assertEqual(with_log, without_log)  # identical fallback behavior
        self.assertIn("pythagorean", with_log)   # trigram-similar to "python"
        log.close()


class TestRelatedSearchesScanCost(_Base):
    """WP13-3 (open-items ledger): the trigram fallback used to rebuild
    every vocabulary term's trigram set on EVERY request (O(vocab) CPU
    per request once the query log is empty). The fix keeps an inverted
    gram->terms index built once per vocabulary change; output must stay
    byte-identical to the naive scan."""

    def _corpus(self, n_terms: int = 200):
        for i in range(n_terms):
            self.indexer.add_document(
                f"d{i}", f"term{i:03d} shared{i % 7} filler{i % 13} content")

    def test_fallback_matches_naive_trigram_scan_exactly(self):
        from nexus_search.core.query_parser import parse_query
        from nexus_search.ranking.suggestions import _trigrams
        from collections import Counter
        self._corpus()
        s = Suggester(self.storage)
        for query in ("term005 shared", "content", "filler2"):
            with self.subTest(query=query):
                out = s.related_searches(query, experiment_log=None, limit=5)
                # the naive reference: same math the fallback must preserve
                # (same term parsing incl. stemming as the implementation)
                terms = set(parse_query(query).terms)
                qgrams = set().union(*(_trigrams(t) for t in terms)) if terms else set()
                scored = Counter()
                for term in self.storage.all_terms():
                    if term in terms:
                        continue
                    shared = len(_trigrams(term) & qgrams)
                    if shared:
                        scored[term] = shared
                expected = [t for t, _ in sorted(
                    scored.items(),
                    key=lambda kv: (-kv[1],
                                   -self.storage.document_frequency(kv[0]),
                                   kv[0]))[:5]]
                self.assertEqual(out, expected)

    def test_gram_index_rebuilt_only_when_documents_change(self):
        self._corpus()
        s = Suggester(self.storage)
        s.related_searches("term005 shared", experiment_log=None)
        index_first = s._gram_index
        s.related_searches("filler3 content", experiment_log=None)
        self.assertIs(s._gram_index, index_first)  # same vocab -> same index
        self.indexer.add_document("extra", "brandnewword appears")
        s.related_searches("brandnewword", experiment_log=None)
        self.assertIsNot(s._gram_index, index_first)  # vocab changed


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
