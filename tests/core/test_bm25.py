import os
import tempfile
import unittest

from nexus_search.core.bm25 import BM25Search
from nexus_search.core.indexer import Indexer
from nexus_search.core.storage import Storage


class TestBM25(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.storage = Storage(self.path)
        self.indexer = Indexer(self.storage)
        self.search = BM25Search(self.storage)

    def tearDown(self):
        self.storage.close()
        os.remove(self.path)

    def test_empty_corpus_returns_no_results(self):
        self.assertEqual(self.search.search("anything"), [])

    def test_empty_query_returns_no_results(self):
        self.indexer.add_document("d1", "some content")
        self.assertEqual(self.search.search(""), [])

    def test_finds_matching_document(self):
        self.indexer.add_document("d1", "the quick brown fox jumps")
        self.indexer.add_document("d2", "a completely different sentence")
        results = self.search.search("fox")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].doc_id, "d1")

    def test_no_matching_term_returns_empty(self):
        self.indexer.add_document("d1", "hello world")
        self.assertEqual(self.search.search("nonexistentterm"), [])

    def test_term_frequency_increases_score(self):
        self.indexer.add_document("d1", "python python python programming")
        self.indexer.add_document("d2", "python programming language basics")
        results = {r.doc_id: r.score for r in self.search.search("python")}
        self.assertGreater(results["d1"], results["d2"])

    def test_rare_term_gets_positive_score(self):
        self.indexer.add_document("d1", "python is a language")
        self.indexer.add_document("d2", "python and zygote appear here")
        self.indexer.add_document("d3", "python is popular for scripting")
        results = self.search.search("zygote")
        self.assertEqual(len(results), 1)
        self.assertGreater(results[0].score, 0)

    def test_top_k_limits_results(self):
        for i in range(5):
            self.indexer.add_document(f"d{i}", "shared term appears here")
        results = self.search.search("shared", top_k=2)
        self.assertEqual(len(results), 2)

    def test_results_sorted_descending_by_score(self):
        self.indexer.add_document("d1", "cat cat cat dog")
        self.indexer.add_document("d2", "cat dog dog dog")
        self.indexer.add_document("d3", "cat dog cat dog")
        scores = [r.score for r in self.search.search("cat dog")]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_multi_term_query_scores_both(self):
        self.indexer.add_document("d1", "apple banana")
        self.indexer.add_document("d2", "apple only here")
        results = {r.doc_id: r.score for r in self.search.search("apple banana")}
        self.assertGreater(results["d1"], results["d2"])

    def test_snippet_is_truncated(self):
        self.indexer.add_document("d1", "word " * 100)
        results = self.search.search("word")
        self.assertLessEqual(len(results[0].snippet), 203)  # 200 chars + "..."

    def test_reindexed_document_uses_new_content_for_scoring(self):
        self.indexer.add_document("d1", "original content about cars")
        self.indexer.add_document("d1", "updated content about bicycles")
        self.assertEqual(self.search.search("cars"), [])
        self.assertEqual(len(self.search.search("bicycles")), 1)


if __name__ == "__main__":
    unittest.main()
