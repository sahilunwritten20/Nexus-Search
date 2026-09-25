"""Tests for Phase 1 build-out #6 — MMR result diversity."""
import os
import shutil
import tempfile
import time
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.core.bm25 import SearchResult, BM25Search
from nexus_search.core.diversity import mmr_select
from nexus_search.core.indexer import Indexer
from nexus_search.core.storage import Storage


class TestMMR(unittest.TestCase):
    def _mk(self, n, body):
        return [SearchResult(doc_id=f"d{i}", score=1.0 - i * 0.01, title=f"t{i}",
                             snippet="", doc_type="text") for i in range(n)]

    def test_lambda_one_is_input_order(self):
        results = self._mk(5, "same words here")
        out = mmr_select(results, lambda r: "same words here", lambda_=1.0,
                         similarity_threshold=1.0)
        self.assertEqual([s.result.doc_id for s in out], [f"d{i}" for i in range(5)])

    def test_similar_flooding_is_capped(self):
        # 4 copies of the same idea + 1 distinct doc
        results = [
            SearchResult(doc_id=f"copy{i}", score=1.0 - i * 0.05,
                         title="Mirror copy", snippet="", doc_type="text")
            for i in range(4)
        ]
        results.append(SearchResult(doc_id="unique", score=0.5, title="Unique piece",
                                    snippet="", doc_type="text"))

        def text(r):
            return "identical content about exactly this" if r.doc_id.startswith("copy") \
                else "entirely different topic words"

        out = mmr_select(results, text, lambda_=0.6, similarity_threshold=0.85)
        chosen_ids = [s.result.doc_id for s in out]
        self.assertEqual(len([c for c in chosen_ids if c.startswith("copy")]), 1)
        self.assertIn("unique", chosen_ids)

    def test_threshold_one_disables_hard_skip(self):
        results = self._mk(3, "x")
        out = mmr_select(results, lambda r: "identical", lambda_=0.5,
                         similarity_threshold=1.0)
        self.assertEqual(len(out), 3)  # penalty applied but nothing skipped


class TestDiversityInSearch(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "t.db")
        self.storage = Storage(self.path)
        self.indexer = Indexer(self.storage)
        from nexus_search.core.hybrid_search import HybridSearch, SearchMode
        from nexus_search.core.vector_store import VectorStoreManager
        self.vs = VectorStoreManager(self.path)
        self.hybrid = HybridSearch(self.storage, vector_store=self.vs, db_path=self.path)
        self.SearchMode = SearchMode

    def tearDown(self):
        self.vs.close()
        self.storage.close()
        for _ in range(10):
            try:
                shutil.rmtree(self.dir)
                break
            except PermissionError:
                time.sleep(0.05)

    def test_near_duplicate_family_does_not_flood_page(self):
        # 3 parent docs with near-identical content + 1 distinct; all match
        for i in range(3):
            self.indexer.add_document(f"parent{i}", "the brown fox jumps over the lazy dog today",
                                      title=f"Fox variant {i}")
        self.indexer.add_document("solo", "quantum computing research notes and python",
                                  title="Quantum", doc_type="pdf")
        # widen candidate pool with phrase so all 4 are in play
        plain = self.hybrid.search_page("brown fox quantum", mode=self.SearchMode.KEYWORD)
        self.assertGreaterEqual(len(plain.results), 3)  # without diversity: flood
        div = self.hybrid.search_page("brown fox quantum", mode=self.SearchMode.KEYWORD,
                                      diversity=0.7)
        foxes = [r for r in div.results if r.doc_id.startswith("parent")]
        self.assertLessEqual(len(foxes), 2)
        self.assertIn("solo", [r.doc_id for r in div.results])

    def test_diversity_zero_is_exactly_default(self):
        self.indexer.add_document("a", "python python python", title="A")
        self.indexer.add_document("b", "python java", title="B")
        a = self.hybrid.search_page("python", mode=self.SearchMode.KEYWORD)
        b = self.hybrid.search_page("python", mode=self.SearchMode.KEYWORD, diversity=0.0)
        self.assertEqual([(r.doc_id, r.score) for r in a.results],
                         [(r.doc_id, r.score) for r in b.results])


if __name__ == "__main__":
    unittest.main()
