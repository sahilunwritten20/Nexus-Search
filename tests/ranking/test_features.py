"""Tests for Phase 5 Stage 2 — feature assembly + shared normalizers."""
import os
import shutil
import tempfile
import time
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.core.indexer import Indexer
from nexus_search.core.normalize import (
    min_max_normalize, min_max_normalize_list, zscore_normalize,
)
from nexus_search.core.storage import Storage
from nexus_search.ranking.features import (
    RankingFeatures, SignalContext, build_context, extract_features,
)


class TestSharedNormalize(unittest.TestCase):
    """The min-max hybrid_search.py and the ranker use MUST be this one."""

    def test_hybrid_search_uses_shared_module(self):
        from nexus_search.core import hybrid_search
        self.assertEqual(hybrid_search._normalize_scores,
                         min_max_normalize)  # one implementation, not two

    def test_min_max_dict(self):
        out = min_max_normalize({"a": 10.0, "b": 0.0, "c": 5.0})
        self.assertEqual((out["a"], out["b"]), (1.0, 0.0))
        self.assertAlmostEqual(out["c"], 0.5)

    def test_min_max_list_and_collapse(self):
        self.assertEqual(min_max_normalize_list([2.0, 2.0]), [1.0, 1.0])
        self.assertEqual(min_max_normalize_list([0.0, 0.0]), [0.0, 0.0])
        self.assertEqual(min_max_normalize_list([]), [])

    def test_zscore(self):
        out = zscore_normalize([1.0, 2.0, 3.0])
        self.assertAlmostEqual(sum(out), 0.0)
        self.assertAlmostEqual(out[2], -out[0])
        self.assertEqual(zscore_normalize([5.0, 5.0]), [0.0, 0.0])
        self.assertEqual(zscore_normalize([]), [])


class TestFeatureAssembly(unittest.TestCase):
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

    def test_extract_all_features(self):
        self.indexer.add_document("d", "python search tutorial content",
                                  title="Python Tutorial",
                                  metadata={"language": "en", "quality": 0.8,
                                            "url": "https://ex.com/python"})
        ctx = build_context("python tutorial")
        ctx.bm25_contribution = 0.9
        ctx.vector_contribution = 0.4
        feats = extract_features("d", self.storage, ctx)
        self.assertIsInstance(feats, RankingFeatures)
        d = feats.as_dict()
        self.assertEqual(set(d), {
            "bm25_score", "semantic_similarity", "title_match", "url_match",
            "phrase_match", "freshness", "document_quality", "content_quality",
            "language_relevance", "source_authority", "popularity", "click_signal"})
        self.assertEqual(d["bm25_score"], 0.9)
        self.assertEqual(d["title_match"], 1.0)
        self.assertEqual(d["content_quality"], 0.8)
        self.assertEqual(d["source_authority"], 0.5)  # placeholder neutral
        self.assertEqual(d["click_signal"], 0.5)

    def test_missing_document_never_crashes(self):
        ctx = build_context("anything")
        feats = extract_features("no-such-doc", self.storage, ctx)
        self.assertEqual(feats.bm25_score, 0.5)          # neutral
        self.assertEqual(feats.title_match, 0.0)         # match signal reads 0
        self.assertEqual(feats.freshness, 0.5)

    def test_fetch_once_semantics(self):
        # caller-provided doc must be used (no double read)
        self.indexer.add_document("d", "hello world", title="Hello")
        doc = self.storage.get_document("d")
        feats = extract_features("d", self.storage, build_context("hello"), doc=doc)
        self.assertEqual(feats.title_match, 1.0)  # title "Hello" contains query term


if __name__ == "__main__":
    unittest.main()
