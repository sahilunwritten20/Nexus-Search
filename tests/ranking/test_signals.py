"""Tests for Phase 5 Stage 2 — ranking signals (one per signal:
normal case, missing metadata (must not crash), boundary values)."""
import os
import shutil
import tempfile
import time
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.core.indexer import Indexer
from nexus_search.core.storage import Storage
from nexus_search.ranking import signals
from nexus_search.ranking.features import SignalContext, build_context
from nexus_search.ranking.query import understand_query


class _Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "t.db")
        self.storage = Storage(self.path)
        self.indexer = Indexer(self.storage)
        self.ctx = build_context("python tutorial")

    def tearDown(self):
        self.storage.close()
        for _ in range(10):
            try:
                shutil.rmtree(self.dir)
                break
            except PermissionError:
                time.sleep(0.05)

    def add_doc(self, doc_id, content, title="", metadata=None, added_at=None):
        self.indexer.add_document(doc_id, content, title=title, metadata=metadata or {})
        if added_at is not None:
            self.storage.conn.execute("UPDATE documents SET added_at = ? WHERE doc_id = ?",
                                      (added_at, doc_id))
            self.storage.conn.commit()
        return self.storage.get_document(doc_id)


class TestBm25Signal(_Base):
    def test_normal_clamped(self):
        self.ctx.bm25_contribution = 0.7
        self.assertEqual(signals.compute_bm25_score(None, "q", self.ctx), 0.7)

    def test_missing_contribution_neutral(self):
        self.assertEqual(signals.compute_bm25_score(None, "q", self.ctx), 0.5)

    def test_boundaries(self):
        self.ctx.bm25_contribution = 0.0
        self.assertEqual(signals.compute_bm25_score(None, "q", self.ctx), 0.0)
        self.ctx.bm25_contribution = 5.0
        self.assertEqual(signals.compute_bm25_score(None, "q", self.ctx), 1.0)

    def test_semantic_neutral_and_value(self):
        self.assertEqual(signals.compute_semantic_similarity(None, "q", self.ctx), 0.5)
        self.ctx.vector_contribution = 0.3
        self.assertEqual(signals.compute_semantic_similarity(None, "q", self.ctx), 0.3)


class TestTitleMatch(_Base):
    def test_normal(self):
        doc = self.add_doc("t", "body text", title="Python Tutorial")
        self.assertEqual(signals.compute_title_match(doc, "python tutorial", self.ctx), 1.0)

    def test_partial(self):
        doc = self.add_doc("t", "body", title="Python Advanced")
        self.assertAlmostEqual(signals.compute_title_match(doc, "python tutorial", self.ctx), 0.5)

    def test_none_doc_no_crash(self):
        self.assertEqual(signals.compute_title_match(None, "python", self.ctx), 0.0)

    def test_empty_query_zero(self):
        doc = self.add_doc("t", "body", title="Python")
        self.assertEqual(signals.compute_title_match(doc, "", self.ctx), 0.0)


class TestUrlMatch(_Base):
    def test_normal(self):
        doc = self.add_doc("u", "body", metadata={"url": "https://x.com/python/tutorial"})
        self.assertEqual(signals.compute_url_match(doc, "python tutorial", self.ctx), 1.0)

    def test_missing_url_zero(self):
        doc = self.add_doc("u", "body")
        self.assertEqual(signals.compute_url_match(doc, "python", self.ctx), 0.0)

    def test_none_doc_no_crash(self):
        self.assertEqual(signals.compute_url_match(None, "python", self.ctx), 0.0)


class TestPhraseMatch(_Base):
    def test_phrase_present(self):
        u = understand_query('"machine learning"', self.storage)
        ctx = build_context('"machine learning"', understanding=u)
        doc = self.add_doc("p", "all about machine learning systems", "ML")
        self.assertEqual(signals.compute_phrase_match(doc, "q", ctx), 1.0)

    def test_phrase_absent(self):
        u = understand_query('"machine learning"', self.storage)
        ctx = build_context('"machine learning"', understanding=u)
        doc = self.add_doc("p", "learning about machines, separately", "ML")
        self.assertEqual(signals.compute_phrase_match(doc, "q", ctx), 0.0)

    def test_no_phrases_zero_not_crash(self):
        doc = self.add_doc("p", "anything", "T")
        self.assertEqual(signals.compute_phrase_match(doc, "q", self.ctx), 0.0)
        self.assertEqual(signals.compute_phrase_match(None, "q", self.ctx), 0.0)


class TestFreshness(_Base):
    def test_now_is_one(self):
        doc = self.add_doc("f", "body")
        self.assertGreater(signals.compute_freshness(doc, "q", self.ctx), 0.95)

    def test_one_half_life_is_half(self):
        doc = self.add_doc("f", "body", added_at=time.time() - 30 * 86400)
        self.assertAlmostEqual(signals.compute_freshness(doc, "q", self.ctx), 0.5, places=1)

    def test_missing_timestamp_neutral(self):
        doc = self.add_doc("f", "body")
        self.storage.conn.execute("UPDATE documents SET added_at = 0 WHERE doc_id = 'f'")
        self.storage.conn.commit()
        doc = self.storage.get_document("f")
        self.assertEqual(signals.compute_freshness(doc, "q", self.ctx), 0.5)
        self.assertEqual(signals.compute_freshness(None, "q", self.ctx), 0.5)


class TestQualitySignals(_Base):
    def test_content_quality_reads_metadata_not_recomputes(self):
        doc = self.add_doc("q", "body", metadata={"quality": 0.9})
        self.assertEqual(signals.compute_content_quality(doc, "q", self.ctx), 0.9)

    def test_content_quality_missing_neutral(self):
        doc = self.add_doc("q", "body")
        self.assertEqual(signals.compute_content_quality(doc, "q", self.ctx), 0.5)
        self.assertEqual(signals.compute_content_quality(None, "q", self.ctx), 0.5)

    def test_document_quality_structure(self):
        titled = self.add_doc("a", "word " * 500, title="T")
        bare = self.add_doc("b", "x", title="")
        self.assertGreater(signals.compute_document_quality(titled, "q", self.ctx),
                           signals.compute_document_quality(bare, "q", self.ctx))

    def test_language_match_and_mismatch(self):
        u_fr = understand_query("le chat est dans la maison", self.storage)
        ctx_fr = build_context("le chat est dans la maison", understanding=u_fr)
        fr = self.add_doc("fr", "body", metadata={"language": "fr"})
        en = self.add_doc("en", "body", metadata={"language": "en"})
        none_ = self.add_doc("n", "body")
        self.assertEqual(signals.compute_language_relevance(fr, "q", ctx_fr), 1.0)
        self.assertEqual(signals.compute_language_relevance(en, "q", ctx_fr), 0.0)
        # missing metadata is NEUTRAL, never 0
        self.assertEqual(signals.compute_language_relevance(none_, "q", ctx_fr), 0.5)
        # unknown query language is NEUTRAL too
        self.assertEqual(signals.compute_language_relevance(fr, "q", self.ctx), 0.5)


class TestPlaceholderSignals(_Base):
    def test_defaults_are_neutral(self):
        doc = self.add_doc("s", "body")
        self.assertEqual(signals.compute_source_authority(doc, "q", self.ctx), 0.5)
        self.assertEqual(signals.compute_popularity(doc, "q", self.ctx), 0.5)
        self.assertEqual(signals.compute_click_signal(doc, "q", self.ctx), 0.5)

    def test_metadata_override_ready_for_phase6_7(self):
        doc = self.add_doc("s", "body", metadata={"authority_score": 0.9, "popularity": 0.1,
                                                  "click_score": 0.7})
        self.assertEqual(signals.compute_source_authority(doc, "q", self.ctx), 0.9)
        self.assertEqual(signals.compute_popularity(doc, "q", self.ctx), 0.1)
        self.assertEqual(signals.compute_click_signal(doc, "q", self.ctx), 0.7)

    def test_none_doc_returns_default(self):
        self.assertEqual(signals.compute_source_authority(None, "q", self.ctx), 0.5)
        self.assertEqual(signals.compute_popularity(None, "q", self.ctx), 0.5)
        self.assertEqual(signals.compute_click_signal(None, "q", self.ctx), 0.5)


if __name__ == "__main__":
    unittest.main()
