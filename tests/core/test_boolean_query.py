"""Tests for boolean/field-scoped query syntax (Phase 1 build-out #4).

Grammar under test: NOT > AND > OR precedence, `-x` negation, `title:` field
scoping, `\"` escaping inside phrases. Defaults unchanged: bare adjacency is
still OR ("should") — the pre-existing tests in test_query_features.py pin that.
"""
import os
import shutil
import tempfile
import time
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.core.bm25 import BM25Search
from nexus_search.core.indexer import Indexer
from nexus_search.core.query_parser import parse_query
from nexus_search.core.storage import Storage


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "t.db")
        self.storage = Storage(self.path)
        self.indexer = Indexer(self.storage)
        self.search = BM25Search(self.storage)

    def tearDown(self):
        self.storage.close()
        for _ in range(10):
            try:
                shutil.rmtree(self.dir)
                break
            except PermissionError:
                time.sleep(0.05)

    def docs(self, *pairs):
        for doc_id, text in pairs:
            self.indexer.add_document(doc_id, text)


class TestParseBoolean(Base):
    def test_default_adjacency_is_or(self):
        q = parse_query("alpha beta")
        self.assertEqual(len(q.groups), 1)
        self.assertEqual(q.groups[0].optional, ["alpha", "beta"])
        self.assertEqual(q.groups[0].required, [])

    def test_and(self):
        q = parse_query("alpha AND beta")
        self.assertEqual(q.groups[0].required, ["alpha", "beta"])

    def test_or_splits_groups(self):
        q = parse_query("alpha OR beta")
        self.assertEqual(len(q.groups), 2)

    def test_or_binds_looser_than_and(self):
        # "a OR b AND c" == "a OR (b AND c)": 2 groups, second requires both
        q = parse_query("a OR b AND c")
        self.assertEqual(len(q.groups), 2)
        self.assertEqual(q.groups[0].optional, ["a"])
        self.assertEqual(q.groups[1].required, ["b", "c"])

    def test_not_keyword_and_dash(self):
        q = parse_query("python NOT snake")
        self.assertEqual(q.groups[0].optional, ["python"])
        self.assertEqual(q.groups[0].excluded, ["snake"])
        q2 = parse_query("python -snake")
        self.assertEqual(q2.groups[0].excluded, ["snake"])

    def test_not_binds_tightest(self):
        # NOT x AND y: NOT applies to x only, then AND gates the pair
        q = parse_query("x AND NOT y")
        self.assertEqual(q.groups[0].required, ["x"])
        self.assertEqual(q.groups[0].excluded, ["y"])

    def test_title_field(self):
        q = parse_query("title:python body")
        self.assertEqual(q.groups[0].title_terms, ["python"])
        self.assertEqual(q.groups[0].optional, ["body"])

    def test_filter_syntax_untouched(self):
        q = parse_query("x type:pdf lang:en")
        self.assertEqual(q.filters, {"doc_type": "pdf", "language": "en"})

    def test_phrase_with_escaped_quote(self):
        q = parse_query('"she said \\"hi\\" loudly"')
        self.assertEqual(q.phrases, ['she said "hi" loudly'])

    def test_backward_compat_flat_fields(self):
        q = parse_query('alpha "beta gamma" type:pdf')
        self.assertIn("alpha", q.terms)
        self.assertEqual(q.phrases, ["beta gamma"])
        self.assertEqual(q.filters, {"doc_type": "pdf"})


class TestSearchBoolean(Base):
    def _corpus(self):
        self.docs(
            ("d1", "python programming language tutorial"),
            ("d2", "python snake habitat guide"),
            ("d3", "java programming language"),
            ("d4", "python java comparison article"),
        )

    def test_and_requires_both(self):
        self._corpus()
        ids = [r.doc_id for r in self.search.search("python AND programming")]
        self.assertEqual(set(ids), {"d1"})

    def test_or_either(self):
        self._corpus()
        ids = set(r.doc_id for r in self.search.search("java OR snake"))
        self.assertEqual(ids, {"d2", "d3", "d4"})

    def test_not_excludes(self):
        self._corpus()
        ids = set(r.doc_id for r in self.search.search("python NOT snake"))
        self.assertEqual(ids, {"d1", "d4"})
        ids2 = set(r.doc_id for r in self.search.search("python -snake"))
        self.assertEqual(ids2, {"d1", "d4"})

    def test_precedence_not_over_and(self):
        self._corpus()
        ids = set(r.doc_id for r in self.search.search("python AND NOT snake"))
        self.assertEqual(ids, {"d1", "d4"})

    def test_not_only_query_matches_nothing(self):
        # A pure NOT has no positive evidence to rank; honest empty, not junk.
        self._corpus()
        self.assertEqual(self.search.search("NOT python"), [])

    def test_title_scope(self):
        self.indexer.add_document("t1", "content about security", title="Python Guide")
        self.indexer.add_document("t2", "python in the body", title="Security Guide")
        ids = set(r.doc_id for r in self.search.search("title:python"))
        self.assertEqual(ids, {"t1"})

    def test_title_phrase_scope(self):
        self.indexer.add_document("t1", "content", title="Deep Learning Guide")
        self.indexer.add_document("t2", "content", title="Deep Pockets Weekly")
        ids = set(r.doc_id for r in self.search.search('title:"deep learning"'))
        self.assertEqual(ids, {"t1"})

    def test_boolean_combines_with_filters(self):
        self.indexer.add_document("a", "python guide", doc_type="pdf")
        self.indexer.add_document("b", "python snake", doc_type="pdf")
        self.indexer.add_document("c", "python guide", doc_type="web")
        ids = set(r.doc_id for r in self.search.search("python NOT snake type:pdf"))
        self.assertEqual(ids, {"a"})


class TestHybridBooleanGate(Base):
    """The boolean gate must hold on the vector side too."""

    def setUp(self):
        super().setUp()
        from nexus_search.core.hybrid_search import HybridSearch, SearchMode
        from nexus_search.core.vector_store import VectorStoreManager
        self.vs = VectorStoreManager(self.path)
        self.hybrid = HybridSearch(self.storage, vector_store=self.vs, db_path=self.path)
        self.SearchMode = SearchMode

    def tearDown(self):
        self.vs.close()
        super().tearDown()

    def test_vector_side_respects_not(self):
        self.indexer.add_document("a", "python programming language")
        self.indexer.add_document("b", "python snake habitat")
        self.vs.upsert("a", "python programming language", "text", "")
        self.vs.upsert("b", "python snake habitat", "text", "")
        page = self.hybrid.search_page("python NOT snake", mode=self.SearchMode.HYBRID)
        self.assertEqual({r.doc_id for r in page.results}, {"a"})


if __name__ == "__main__":
    unittest.main()
