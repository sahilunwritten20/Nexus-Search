"""Tests for Phase 5 Stage 1 — query understanding (offline heuristics)."""
import os
import shutil
import tempfile
import time
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.core.indexer import Indexer
from nexus_search.core.storage import Storage
from nexus_search.ranking.query import (
    DEFAULT_SYNONYMS, QueryUnderstanding, correct_spelling, detect_intent,
    detect_language, expand_synonyms, extract_entities, normalize_query,
    understand_query,
)


class _StorageBacked(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "t.db")
        self.storage = Storage(self.path)
        self.indexer = Indexer(self.storage)
        self.indexer.add_document("d1", "python programming language tutorial", title="Python")
        self.indexer.add_document("d2", "machine learning models in python", title="ML")

    def tearDown(self):
        self.storage.close()
        for _ in range(10):
            try:
                shutil.rmtree(self.dir)
                break
            except PermissionError:
                time.sleep(0.05)


class TestNormalize(unittest.TestCase):
    def test_lowercase_collapse_strip(self):
        self.assertEqual(normalize_query("  Hello,   WORLD!!  "), "hello world")

    def test_preserves_phrase_and_filters(self):
        out = normalize_query('"Machine Learning"  type:PDF lang:EN extra')
        self.assertIn('"machine learning"', out)
        self.assertIn("type:pdf", out)
        self.assertIn("lang:en", out)
        self.assertIn("extra", out)

    def test_empty(self):
        self.assertEqual(normalize_query(""), "")


class TestSpellCorrection(_StorageBacked):
    def test_corrects_zero_posting_term(self):
        # "pythom" has no postings; "python" does.
        u = understand_query("pythom tutorial", self.storage)
        self.assertEqual(u.corrected_terms, {"pythom": "python"})
        self.assertIn("python", u.effective_terms)

    def test_never_replaces_in_vocabulary_term(self):
        u = understand_query("python", self.storage)
        self.assertEqual(u.corrected_terms, {})
        self.assertEqual(u.effective_terms, ["python"])

    def test_no_candidate_within_distance_returns_none(self):
        self.assertIsNone(correct_spelling("zzzzqqqq", {"python", "machine"}))

    def test_vocab_miss_without_storage(self):
        u = understand_query("pythom", None)
        self.assertEqual(u.corrected_terms, {})


class TestSynonyms(unittest.TestCase):
    def test_expansion_appends_without_replacing(self):
        out = expand_synonyms(["car", "fast"], DEFAULT_SYNONYMS)
        self.assertIn("automobile", out)
        self.assertIn("quick", out)
        self.assertNotIn("car", out)  # expansions list only

    def test_pluggable_map(self):
        out = expand_synonyms(["car"], {"car": ["ride"]})
        self.assertEqual(out, ["ride"])

    def test_unknown_term_expands_to_nothing(self):
        self.assertEqual(expand_synonyms(["qwerty"], {}), [])

    def test_effective_terms_keep_originals(self):
        u = QueryUnderstanding(original="car", normalized="car",
                               expansion_terms=["automobile"])
        self.assertEqual(u.effective_terms, ["car", "automobile"])


class TestLanguage(unittest.TestCase):
    def test_english_detected(self):
        self.assertEqual(detect_language("what is the weather in the city"), "en")

    def test_french_detected(self):
        self.assertEqual(detect_language("le chat est dans la maison avec le chien"), "fr")

    def test_short_query_returns_unknown(self):
        self.assertEqual(detect_language("hi there"), "unknown")

    def test_no_stopword_hit_returns_unknown(self):
        self.assertEqual(detect_language("qwerty asdfg zxcvb poiuy"), "unknown")


class TestIntent(unittest.TestCase):
    def test_transactional(self):
        self.assertEqual(detect_intent("buy cheap laptop price"), "transactional")

    def test_navigational(self):
        self.assertEqual(detect_intent("github login"), "navigational")
        self.assertEqual(detect_intent("example.com"), "navigational")

    def test_informational(self):
        self.assertEqual(detect_intent("what is a hash index"), "informational")

    def test_default_is_informational(self):
        self.assertEqual(detect_intent("vector databases"), "informational")


class TestEntities(unittest.TestCase):
    def test_quoted_phrase_is_entity(self):
        self.assertIn("machine learning", extract_entities('"machine learning" models'))

    def test_capitalized_sequence_is_entity(self):
        self.assertIn("New York", extract_entities("flights to New York today"))

    def test_no_entities(self):
        self.assertEqual(extract_entities("just lowercase words"), [])


class TestHybridOptIn(_StorageBacked):
    """understand_query() is consumed by hybrid search ONLY when asked."""

    def setUp(self):
        super().setUp()
        from nexus_search.core.vector_store import VectorStoreManager
        from nexus_search.core.hybrid_search import HybridSearch
        self.vs = VectorStoreManager(self.path)
        self._extra_vs = self.vs
        self.hybrid = HybridSearch(self.storage, vector_store=self.vs, db_path=self.path)

    def tearDown(self):
        self.vs.close()
        super().tearDown()

    def test_opt_in_widens_recall(self):
        from nexus_search.core.hybrid_search import SearchMode
        # misspelled: nothing in index has "pythom"
        plain = self.hybrid.search_page("pythom", mode=SearchMode.KEYWORD)
        self.assertEqual(plain.total, 0)
        u = understand_query("pythom", self.storage)
        widened = self.hybrid.search_page("pythom", mode=SearchMode.KEYWORD, understanding=u)
        self.assertGreaterEqual(widened.total, 1)

    def test_default_off_unchanged(self):
        from nexus_search.core.hybrid_search import SearchMode
        a = self.hybrid.search_page("python", mode=SearchMode.KEYWORD)
        u = understand_query("python", self.storage)
        self.assertEqual(u.to_retrieval_query(), "python")  # nothing to change
        b = self.hybrid.search_page("python", mode=SearchMode.KEYWORD, understanding=u)
        self.assertEqual([r.doc_id for r in a.results], [r.doc_id for r in b.results])

    def test_expansion_never_replaces_snippet_text(self):
        # Retrieval widened via synonym ("car" -> "python"), but every snippet
        # must still be a raw substring of the stored document — expansions
        # widen the candidate set, they never rewrite what the user reads.
        u = understand_query("car", self.storage, synonyms={"car": ["python"]})
        page = self.hybrid.search_page("car", understanding=u)
        self.assertGreaterEqual(page.total, 1)  # matched only via the synonym
        for r in page.results:
            doc = self.storage.get_document(r.doc_id)
            self.assertIsNotNone(doc)
            clean = r.snippet.lstrip(".").rstrip(".")
            self.assertIn(clean, doc.content)


class TestUnderstandQuery(_StorageBacked):
    def test_full_composition(self):
        u = understand_query('"machine learning" pythom the and of', self.storage)
        self.assertIsInstance(u, QueryUnderstanding)
        self.assertEqual(u.corrected_terms.get("pythom"), "python")
        self.assertEqual(u.intent, "informational")
        self.assertIn("machine learning", u.entities)

    def test_excludes_filter_tokens_from_spelling(self):
        u = understand_query("type:pdf python", self.storage)
        self.assertNotIn("pdf", (t for pair in u.corrected_terms.items() for t in pair))


if __name__ == "__main__":
    unittest.main()
