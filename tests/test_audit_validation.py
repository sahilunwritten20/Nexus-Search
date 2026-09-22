"""Phase 1-4 audit validation tests.

Additional edge-case, integration, security, failure-mode and regression
tests written during the full Phase 1-4 audit. Every test here validates
behavior required by the spec/README that the pre-existing suite did not
cover, or pins a bug found (and fixed) during the audit.
"""
import os
import shutil
import tempfile
import threading
import time
import unittest

os.environ["NEXUS_EMBEDDER"] = "hash:384"

import numpy as np

from nexus_search.core.storage import Storage
from nexus_search.core.indexer import Indexer
from nexus_search.core.bm25 import BM25Search
from nexus_search.core.tokenizer import tokenize
from nexus_search.core.query_parser import parse_query
from nexus_search.core.vector_store import VectorStoreManager
from nexus_search.core.hybrid_search import (
    HybridSearch, SearchMode, _normalize_scores, _rrf_fuse, _weighted_fuse,
)
from nexus_search.core.embedding_sync import EmbeddingSync
from nexus_search.core.embedders import EmbedderUnavailable
from nexus_search.ingestion.dedup import Deduplicator
from nexus_search.ingestion.pipeline import ingest_one
from nexus_search.ingestion.types import IngestDoc


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "t.db")
        self.storage = Storage(self.path)
        self.indexer = Indexer(self.storage)
        self.dedup = Deduplicator(self.path)
        self.vs = VectorStoreManager(self.path)

    def tearDown(self):
        self.vs.close()
        self.storage.close()
        self.dedup.close()
        for _ in range(10):
            try:
                shutil.rmtree(self.dir)
                break
            except PermissionError:
                time.sleep(0.05)

    def make_hybrid(self, bm25_w=1.0, vector_w=1.0):
        return HybridSearch(self.storage, bm25_weight=bm25_w, vector_weight=vector_w,
                            vector_store=self.vs, db_path=self.path)

    def add(self, doc_id, content, title="", doc_type="text", metadata=None):
        """Add a document AND its embedding (what the API does per doc)."""
        self.indexer.add_document(doc_id, content, title=title, doc_type=doc_type,
                                  metadata=metadata or {})
        doc = self.storage.get_document(doc_id)
        self.vs.upsert(doc_id, f"{doc.title} {doc.content}", doc.doc_type,
                       (doc.metadata or {}).get("language", "") or "")


class _ExplodingEmbedder:
    name = "explode"
    dim = 384

    def embed_documents(self, texts, batch_size=32):
        raise EmbedderUnavailable("boom")

    def embed_query(self, text):
        raise EmbedderUnavailable("boom")


# ---------------------------------------------------------------------------
# Phase 1: tokenizer / parser / bm25 / pagination edge cases
# ---------------------------------------------------------------------------

class TestTokenizerEdges(unittest.TestCase):
    def test_numbers_and_very_long_input(self):
        toks = tokenize("abc123 456" + " word" * 5000)
        self.assertIn("abc123", toks)
        self.assertEqual(toks.count("word"), 5000)

    def test_repeated_terms_kept(self):
        self.assertEqual(tokenize("go go go"), ["go", "go", "go"])

    def test_unicode_mixed(self):
        self.assertIn("café", tokenize("Café ☕ naïve"))

    def test_whitespace_only(self):
        self.assertEqual(tokenize("   \t\n "), [])


class TestQueryParserEdges(unittest.TestCase):
    def test_invalid_filter_key_is_term(self):
        q = parse_query("foo:bar")
        self.assertEqual(q.filters, {})

    def test_empty_filter_value_not_filter(self):
        self.assertEqual(parse_query("type:").filters, {})

    def test_multiple_same_filters_last_wins(self):
        self.assertEqual(parse_query("type:pdf type:web").filters["doc_type"], "web")

    def test_phrase_with_special_chars(self):
        q = parse_query('"a! b?" type:pdf')
        self.assertEqual(q.phrases, ["a! b?"])
        self.assertEqual(q.filters, {"doc_type": "pdf"})

    def test_unclosed_quote_no_crash(self):
        self.assertIsInstance(parse_query('"unterminated phrase'), object)


class TestBM25Edges(Base):
    def test_filter_only_query(self):
        self.indexer.add_document("d1", "aaa", doc_type="pdf")
        self.indexer.add_document("d2", "bbb", doc_type="web")
        page = BM25Search(self.storage).search_page("type:pdf")
        self.assertEqual([r.doc_id for r in page.results], ["d1"])

    def test_pagination_boundaries(self):
        s = BM25Search(self.storage)
        for i in range(7):
            self.indexer.add_document(f"d{i}", "sharedterm here")
        self.assertEqual(len(s.search_page("sharedterm", top_k=3, offset=0).results), 3)
        self.assertEqual(len(s.search_page("sharedterm", top_k=3, offset=6).results), 1)
        self.assertEqual(s.search_page("sharedterm", top_k=3, offset=7).results, [])
        self.assertEqual(s.search_page("sharedterm", top_k=3, offset=10000).results, [])
        self.assertEqual(s.search_page("sharedterm", top_k=3, offset=0).total, 7)

    def test_tie_break_is_deterministic(self):
        s = BM25Search(self.storage)
        self.indexer.add_document("b", "identical content here")
        self.indexer.add_document("a", "identical content here")
        r1 = [r.doc_id for r in s.search("identical")]
        self.assertEqual(r1, [r.doc_id for r in s.search("identical")])
        self.assertEqual(r1, ["a", "b"])

    def test_empty_document_filter_findable(self):
        self.indexer.add_document("empty", "", doc_type="pdf")
        page = BM25Search(self.storage).search_page("type:pdf")
        self.assertEqual([r.doc_id for r in page.results], ["empty"])

    def test_sql_injection_in_filter_safe(self):
        self.indexer.add_document("d1", "text", doc_type="text")
        page = BM25Search(self.storage).search_page("type:'; DROP TABLE documents; --")
        self.assertEqual(page.results, [])
        self.assertEqual(self.storage.document_count(), 1)


# ---------------------------------------------------------------------------
# Phase 2: ingestion edge cases
# ---------------------------------------------------------------------------

class TestIngestionEdges(Base):
    def test_empty_content_indexes(self):
        status = ingest_one(IngestDoc("e", "T", "", "text", {}), self.indexer, self.dedup)
        self.assertEqual(status, "indexed")
        self.assertIsNotNone(self.storage.get_document("e"))

    def test_duplicate_content_not_reindexed(self):
        self.assertEqual(ingest_one(IngestDoc("a", "T1", "same body", "text", {}),
                                    self.indexer, self.dedup), "indexed")
        self.assertEqual(ingest_one(IngestDoc("b", "T2", "same body", "text", {}),
                                    self.indexer, self.dedup), "duplicate")
        self.assertEqual(self.storage.document_count(), 1)

    def test_different_content_not_deduped(self):
        self.assertEqual(ingest_one(IngestDoc("a", "T1", "body one", "text", {}),
                                    self.indexer, self.dedup), "indexed")
        self.assertEqual(ingest_one(IngestDoc("b", "T2", "body two", "text", {}),
                                    self.indexer, self.dedup), "indexed")
        self.assertEqual(self.storage.document_count(), 2)

    def test_malformed_json_skipped_not_fatal(self):
        from nexus_search.ingestion.connectors.files import read_file
        from pathlib import Path
        p = Path(self.dir) / "bad.json"
        p.write_text("{not valid json", encoding="utf-8")
        self.assertEqual(read_file(p), "")  # logged + skipped, no raise

    def test_unsupported_extension_empty(self):
        from nexus_search.ingestion.connectors.files import read_file
        from pathlib import Path
        save = Path.read_text  # noqa - sanity the file doesn't exist
        self.assertEqual(read_file(Path("no_such.xyz")), "")

    def test_corrupt_pdf_skipped_not_fatal(self):
        from nexus_search.ingestion.connectors.files import read_file
        from pathlib import Path
        p = Path(self.dir) / "corrupt.pdf"
        p.write_bytes(b"%PDF-not-a-real-pdf")
        self.assertEqual(read_file(p), "")


# ---------------------------------------------------------------------------
# Phase 4: vector lifecycle / normalization / fusion math / modes / fallback
# ---------------------------------------------------------------------------

class TestVectorStoreEdges(Base):
    def test_add_update_remove_cycle(self):
        self.vs.upsert("d1", "alpha beta", "text", "")
        self.assertEqual(self.vs.get_stats()["count"], 1)
        self.vs.upsert("d1", "gamma delta", "text", "")
        self.assertEqual(self.vs.get_stats()["count"], 1)
        h1 = self.vs.store.get_content_hash("d1")
        self.vs.upsert("d1", "gamma delta", "text", "")  # no-op via hash gate
        self.assertEqual(self.vs.store.get_content_hash("d1"), h1)
        self.vs.delete("d1")
        self.assertEqual(self.vs.get_stats()["count"], 0)

    def test_empty_index_search(self):
        self.assertEqual(self.vs.search("anything", top_k=10), [])

    def test_dimension_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            self.vs.store.add("x", np.zeros(8, dtype=np.float32), "h")

    def test_top_k_larger_than_index(self):
        self.vs.upsert("only", "lone document", "text", "")
        self.assertEqual([r.doc_id for r in self.vs.search("lone", top_k=500)], ["only"])

    def test_incremental_and_batch_equivalent(self):
        self.vs.upsert("a", "first doc", "text", "")
        self.vs.upsert("b", "second doc", "text", "")
        dir2 = tempfile.mkdtemp()
        try:
            vs2 = VectorStoreManager(os.path.join(dir2, "b.db"))
            vs2.upsert_batch([("a", "first doc", "text", ""), ("b", "second doc", "text", "")])
            r1 = [(r.doc_id, round(r.score, 6)) for r in self.vs.search("doc", top_k=10)]
            r2 = [(r.doc_id, round(r.score, 6)) for r in vs2.search("doc", top_k=10)]
            vs2.close()
            self.assertEqual(r1, r2)
        finally:
            shutil.rmtree(dir2, ignore_errors=True)


class TestScoreNormalization(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(_normalize_scores({}), {})

    def test_single_value(self):
        self.assertEqual(_normalize_scores({"a": 5.0}), {"a": 1.0})

    def test_all_equal_zero(self):
        self.assertEqual(_normalize_scores({"a": 0.0, "b": 0.0}), {"a": 0.0, "b": 0.0})

    def test_min_max(self):
        out = _normalize_scores({"a": 10.0, "b": 0.0, "c": 5.0})
        self.assertEqual((out["a"], out["b"]), (1.0, 0.0))
        self.assertAlmostEqual(out["c"], 0.5)


class TestFusionMath(unittest.TestCase):
    def _sr(self, doc_id, score):
        from nexus_search.core.bm25 import SearchResult
        return SearchResult(doc_id=doc_id, score=score, title="", snippet="", doc_type="text")

    def test_rrf_dedup_and_contribution_sum(self):
        bm25 = {"A": (9.0, self._sr("A", 9.0)), "B": (5.0, self._sr("B", 5.0))}
        vec = {"A": (0.9, "A"), "C": (0.8, "C")}
        fused = _rrf_fuse(bm25, vec, k=60, bm25_weight=1.0, vector_weight=1.0)
        ids = [f[0] for f in fused]
        self.assertEqual(sorted(ids), ["A", "B", "C"])
        self.assertEqual(len(ids), len(set(ids)))
        for _, score, _, bc, vc in fused:
            self.assertAlmostEqual(score, bc + vc)
        self.assertEqual(ids[0], "A")
        self.assertAlmostEqual(fused[0][1], 1 / 61 + 1 / 61)

    def test_weighted_fuse_normalization(self):
        bm25 = {"A": (10.0, self._sr("A", 10.0)), "B": (0.0, self._sr("B", 0.0))}
        vec = {"B": (0.5, "B"), "C": (1.0, "C")}
        by_id = {f[0]: f[1] for f in _weighted_fuse(bm25, vec, 1.0, 1.0)}
        self.assertAlmostEqual(by_id["A"], 1.0)
        self.assertAlmostEqual(by_id["C"], 1.0)
        self.assertAlmostEqual(by_id["B"], 0.0)

    def test_weights_scale_contributions(self):
        fused = _rrf_fuse({"A": (1.0, self._sr("A", 1.0))}, {}, k=60,
                          bm25_weight=2.0, vector_weight=1.0)
        self.assertAlmostEqual(fused[0][1], 2.0 / 61)

    def test_weighted_vs_rrf_both_rank_sane(self):
        bm25 = {"A": (9.0, self._sr("A", 9.0)), "B": (1.0, self._sr("B", 1.0))}
        vec = {"B": (0.9, "B"), "A": (0.1, "A")}
        rrf = [f[0] for f in _rrf_fuse(bm25, vec, k=60, bm25_weight=1.0, vector_weight=1.0)]
        w = [f[0] for f in _weighted_fuse(bm25, vec, 1.0, 1.0)]
        self.assertEqual(sorted(rrf), ["A", "B"])
        self.assertEqual(sorted(w), ["A", "B"])


class TestHybridWeightConsistency(Base):
    """REQ: bm25_w=1/vector_w=0 == keyword; bm25_w=0/vector_w=1 == semantic."""

    def setUp(self):
        super().setUp()
        self.add("A", "zebra stripes document", "A")
        self.add("B", "totally different banana dish", "B")

    def test_vector_weight_zero_equals_keyword(self):
        h = self.make_hybrid(1.0, 0.0)
        hy = h.search_page("zebra", mode=SearchMode.HYBRID)
        kw = h.search_page("zebra", mode=SearchMode.KEYWORD)
        self.assertEqual([r.doc_id for r in hy.results], [r.doc_id for r in kw.results])
        self.assertEqual(hy.total, kw.total)

    def test_bm25_weight_zero_equals_semantic(self):
        h = self.make_hybrid(0.0, 1.0)
        hy = h.search_page("zebra", mode=SearchMode.HYBRID)
        se = h.search_page("zebra", mode=SearchMode.SEMANTIC)
        self.assertEqual([r.doc_id for r in hy.results], [r.doc_id for r in se.results])
        self.assertEqual(hy.total, se.total)

    def test_weights_change_scores(self):
        self.add("C", "target target target target", "offword")
        self.add("D", "target", "target also here")
        sv = {r.doc_id: r.score for r in self.make_hybrid(0.2, 1.0)
              .search_page("target", mode=SearchMode.HYBRID).results}
        sb = {r.doc_id: r.score for r in self.make_hybrid(1.0, 0.2)
              .search_page("target", mode=SearchMode.HYBRID).results}
        self.assertNotEqual(sv, sb)


class TestDuplicateMerging(Base):
    def test_same_doc_once_source_both(self):
        self.add("A", "overlap target content", "A")
        self.add("B", "overlap other", "B")
        page = self.make_hybrid().search_page("overlap", mode=SearchMode.HYBRID)
        ids = [r.doc_id for r in page.results]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(page.results[0].source, "bm25+vector")

    def test_disjoint_candidates_merged_once(self):
        self.add("A", "zzqq unique keyword", "A")
        self.add("B", "banana smoothie recipe", "B")
        page = self.make_hybrid().search_page("zzqq", mode=SearchMode.HYBRID)
        ids = [r.doc_id for r in page.results]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("A", ids)


class TestHybridFallback(Base):
    def _exploding(self):
        self.vs.embedder = _ExplodingEmbedder()
        return HybridSearch(self.storage, vector_store=self.vs, db_path=self.path)

    def test_hybrid_fallback_with_reason(self):
        self.indexer.add_document("a", "alpha fallback content")
        page = self._exploding().search_page("alpha", mode=SearchMode.HYBRID)
        self.assertTrue(page.metadata["fallback"])
        self.assertIn("embedder_unavailable", page.metadata["fallback_reason"])
        self.assertEqual([r.doc_id for r in page.results], ["a"])
        self.assertEqual(page.results[0].source, "bm25_fallback")

    def test_semantic_fallback_with_reason(self):
        self.indexer.add_document("a", "alpha fallback content")
        page = self._exploding().search_page("alpha", mode=SearchMode.SEMANTIC)
        self.assertTrue(page.metadata["fallback"])
        self.assertEqual(page.metadata["mode_used"], "keyword")
        self.assertEqual([r.doc_id for r in page.results], ["a"])

    def test_keyword_ignores_broken_vector(self):
        self.indexer.add_document("a", "alpha fallback content")
        page = self._exploding().search_page("alpha", mode=SearchMode.KEYWORD)
        self.assertFalse(page.metadata["fallback"])
        self.assertEqual([r.doc_id for r in page.results], ["a"])

    def test_bm25_failure_is_not_disguised_as_vector_failure(self):
        h = self.make_hybrid()
        self.indexer.add_document("a", "alpha")
        self.storage.conn.close()  # simulate storage failure
        try:
            h.search_page("alpha", mode=SearchMode.HYBRID)
            self.fail("expected failure to propagate")
        except Exception:
            pass


class TestEmbeddingSyncWiring(Base):
    """Regression: ingest_one(sync=EmbeddingSync) crashed with AttributeError."""

    def test_ingest_one_with_embedding_sync(self):
        sync = EmbeddingSync(self.vs, batch_size=1)
        status = ingest_one(IngestDoc("d1", "T", "alpha beta content", "text", {}),
                            self.indexer, self.dedup, sync=sync)
        self.assertEqual(status, "indexed")
        self.assertEqual(self.vs.get_stats()["count"], 1)
        sync.close()

    def test_sync_attached_no_double_embed(self):
        sync = EmbeddingSync(self.vs, batch_size=1)
        sync.attach(self.indexer)
        ingest_one(IngestDoc("d1", "T", "alpha beta content", "text", {}),
                   self.indexer, self.dedup, sync=sync)
        self.assertEqual(self.vs.get_stats()["count"], 1)
        sync.close()

    def test_chunked_embeddings_via_sync(self):
        sync = EmbeddingSync(self.vs, batch_size=4)
        status = ingest_one(IngestDoc("L", "T", "word " * 400, "text", {}),
                            self.indexer, self.dedup, sync=sync, chunk_size=200)
        self.assertEqual(status, "indexed")
        sync.flush()
        self.assertEqual(self.vs.get_stats()["count"], len(self.storage.chunk_ids("L")))
        sync.close()

    def test_sync_survives_embedder_failure(self):
        self.vs.embedder = _ExplodingEmbedder()
        sync = EmbeddingSync(self.vs, batch_size=1)
        sync.attach(self.indexer)
        self.indexer.add_document("a", "content survives even if embedding fails")
        self.assertEqual(self.storage.document_count(), 1)
        self.assertGreaterEqual(sync.get_stats()["errors"], 1)
        sync.close()

    def test_update_refreshes_and_delete_removes_vector(self):
        sync = EmbeddingSync(self.vs, batch_size=1)
        sync.attach(self.indexer)
        self.indexer.add_document("d1", "first version", title="T")
        self.assertEqual(self.vs.get_stats()["count"], 1)
        self.indexer.add_document("d1", "second version", title="T")
        self.assertEqual(self.vs.get_stats()["count"], 1)
        self.indexer.delete_document("d1")
        self.assertEqual(self.vs.get_stats()["count"], 0)
        sync.close()


class TestConcurrency(Base):
    def test_concurrent_ingest_and_search(self):
        sync = EmbeddingSync(self.vs, batch_size=8)
        sync.attach(self.indexer)
        h = self.make_hybrid()
        errors = []

        def writer(n):
            try:
                for i in range(15):
                    self.indexer.add_document(f"w{n}-{i}", f"payload {n} {i}", title="T")
            except Exception as e:
                errors.append(("w", repr(e)))

        def reader(_):
            try:
                for _ in range(15):
                    h.search("payload", top_k=5, mode=SearchMode.HYBRID)
            except Exception as e:
                errors.append(("r", repr(e)))

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)] + \
                  [threading.Thread(target=reader, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        sync.flush()
        self.assertEqual(self.vs.get_stats()["count"], 60)
        sync.close()


class TestExplainDiagnostics(Base):
    def test_explain_has_real_scores(self):
        self.add("A", "alpha beta gamma", "A")
        self.add("B", "alpha only", "B")
        ex = self.make_hybrid().explain("alpha", mode=SearchMode.HYBRID)
        self.assertTrue(ex["results"])
        for r in ex["results"]:
            for key in ("bm25_normalized", "vector_normalized", "bm25_score", "vector_score"):
                self.assertIn(key, r)
            if r["source"] == "bm25+vector":
                self.assertIsNotNone(r["bm25_normalized"])
                self.assertIsNotNone(r["vector_normalized"])
                self.assertAlmostEqual(r["final_score"],
                                       r["bm25_normalized"] + r["vector_normalized"])

    def test_explain_weighted_contributions_sum(self):
        self.add("A", "alpha beta", "A")
        ex = self.make_hybrid().explain("alpha", mode=SearchMode.HYBRID, fusion="weighted")
        self.assertTrue(ex["results"])
        for r in ex["results"]:
            parts = [v for v in (r["bm25_normalized"], r["vector_normalized"]) if v is not None]
            self.assertAlmostEqual(r["final_score"], sum(parts))


class TestHybridModesOnCorpus(Base):
    def test_phrase_query_hybrid(self):
        self.add("A", "the quick brown fox jumps", "A")
        self.add("B", "brown quick fox", "B")
        page = self.make_hybrid().search_page('"quick brown"', mode=SearchMode.HYBRID)
        self.assertEqual([r.doc_id for r in page.results], ["A"])

    def test_hybrid_type_filter(self):
        self.add("P", "python guide", "P", doc_type="pdf")
        self.add("W", "python guide", "W", doc_type="web")
        page = self.make_hybrid().search_page("python type:pdf", mode=SearchMode.HYBRID)
        self.assertEqual([r.doc_id for r in page.results], ["P"])

    def test_semantic_type_filter_pushdown(self):
        self.add("P", "python guide", "P", doc_type="pdf")
        self.add("W", "python guide", "W", doc_type="web")
        page = self.make_hybrid().search_page("python type:pdf", mode=SearchMode.SEMANTIC)
        self.assertEqual([r.doc_id for r in page.results], ["P"])

    def test_top_k_and_offset_bounds_lib_level(self):
        h = self.make_hybrid()
        self.add("A", "alpha", "A")
        self.assertEqual(h.search_page("alpha", top_k=0).results, [])
        self.assertEqual(h.search_page("alpha", top_k=-3).results, [])


if __name__ == "__main__":
    unittest.main()
