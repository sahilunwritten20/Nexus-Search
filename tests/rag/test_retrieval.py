"""P7-1 — RagRetriever: shared-HybridSearch retrieval with
group_chunks=False, raw bm25/vector signals through fusion AND rerank
(including the None cases), ONE batched text fetch, /search-equivalent
bounds and filter semantics, honest degradation, language metadata."""
import os
import tempfile
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.core.bm25 import BM25Search
from nexus_search.core.hybrid_search import HybridSearch, SearchMode
from nexus_search.core.indexer import Indexer
from nexus_search.core.storage import Storage
from nexus_search.core.vector_store import VectorStoreManager
from nexus_search.core.embedding_sync import EmbeddingSync
from nexus_search.rag.retrieval import (
    MAX_QUERY_CHARS, MAX_TOP_K, MAX_QUERY_WORDS, RagRetriever, RagRetrieval,
    use_rerank_from_env,
)

# Deterministic corpus with TWO chunked parents and one plain doc.
PARENT_1 = ("bm25 lexical retrieval scores inverted index documents. "
            "term frequency and inverse document frequency drive ranking. ") * 12
PARENT_2 = ("vector embeddings map text into a dense space where cosine "
            "similarity approximates semantic relatedness. ") * 12
PLAIN = "A plain unchunked note about sitemaps and url canonicalization."


def _build(tmp_name="ragretrieval.db"):
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, tmp_name)
    storage = Storage(db)
    indexer = Indexer(storage)
    vs = VectorStoreManager(db)
    sync = EmbeddingSync(vs, batch_size=64)
    sync.attach(indexer)
    indexer.add_document("p1#chunk0", PARENT_1[:600], title="Lexical Part 1",
                         doc_type="text",
                         metadata={"parent_id": "p1", "chunk_index": 0,
                                   "chunk_count": 3, "url": "https://a.example/x"})
    indexer.add_document("p1#chunk1", PARENT_1[600:1200], title="Lexical Part 1",
                         doc_type="text",
                         metadata={"parent_id": "p1", "chunk_index": 1,
                                   "chunk_count": 3, "url": "https://a.example/x"})
    indexer.add_document("p1#chunk2", PARENT_1[1200:1800], title="Lexical Part 1",
                         doc_type="text",
                         metadata={"parent_id": "p1", "chunk_index": 2,
                                   "chunk_count": 3, "url": "https://a.example/x"})
    indexer.add_document("p2#chunk0", PARENT_2[:600], title="Vector Part 1",
                         doc_type="text",
                         metadata={"parent_id": "p2", "chunk_index": 0,
                                   "chunk_count": 2, "url": "https://b.example/y"})
    indexer.add_document("p2#chunk1", PARENT_2[600:1200], title="Vector Part 1",
                         doc_type="text",
                         metadata={"parent_id": "p2", "chunk_index": 1,
                                   "chunk_count": 2, "url": "https://b.example/y"})
    indexer.add_document("plain", PLAIN, title="Plain", doc_type="web",
                         metadata={"url": "https://c.example/z", "language": "en"})
    sync.flush()
    hybrid = HybridSearch(storage, vector_store=vs, db_path=db)
    return storage, hybrid, vs, sync


class TestRagRetriever(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.storage, cls.hybrid, cls.vs, cls.sync = _build()
        cls.retriever = RagRetriever(cls.hybrid, cls.storage,
                                     link_intel=None)

    @classmethod
    def tearDownClass(cls):
        cls.sync.close(); cls.hybrid.close(); cls.vs.close()
        cls.storage.close()

    def test_retrieves_chunk_rows_not_grouped_parents(self):
        out = self.retriever.retrieve("bm25 lexical inverted index", k=10)
        self.assertIsInstance(out, RagRetrieval)
        self.assertTrue(out.results)
        for c in out.results:
            # chunk-granular ids, parent context via metadata
            self.assertTrue(c.chunk_id.startswith(("p1#", "p2#", "plain")))
            if c.chunk_id != c.parent_id:
                self.assertIn("#chunk", c.chunk_id)
                self.assertIsNotNone(c.chunk_index)
                self.assertIsNotNone(c.chunk_count)
            else:
                self.assertIsNone(c.chunk_index)
            self.assertTrue(c.text)  # full row content attached

    def test_raw_scores_survive_fusion_and_rerank(self):
        # rerank is ON by default (NEXUS_RAG_USE_RERANK) — raw scores must
        # still be present, including None when one retriever missed.
        out = self.retriever.retrieve("bm25 lexical inverted index", k=10)
        saw_bm25 = saw_vector = saw_none = False
        for c in out.results:
            if c.bm25_score is not None:
                saw_bm25 = True
            if c.vector_score is not None:
                saw_vector = True
            if c.bm25_score is None or c.vector_score is None:
                saw_none = True
        self.assertTrue(out.reranked)
        self.assertTrue(saw_bm25)
        self.assertTrue(saw_vector)
        self.assertTrue(saw_none, "None raw-score cases must be preserved")

    def test_rerank_disabled_via_env(self):
        storage, hybrid, vs, sync = _build("rerank_off.db")
        try:
            os.environ["NEXUS_RAG_USE_RERANK"] = "0"
            retriever = RagRetriever(hybrid, storage)
            out = retriever.retrieve("bm25 lexical inverted index", k=10)
            self.assertFalse(out.reranked)
            self.assertTrue(out.results)
            self.assertEqual(out.results[0].bm25_score, out.results[0].bm25_score)  # smoke
        finally:
            os.environ.pop("NEXUS_RAG_USE_RERANK", None)
            sync.close(); hybrid.close(); vs.close(); storage.close()

    def test_one_batched_text_fetch_no_n_plus_one(self):
        # The RAG layer adds EXACTLY ONE batched get_documents on top of the
        # shared hybrid pipeline's own (already batched, WP12-B4) reads.
        calls = {"n": 0}
        real = self.storage.get_documents

        def counting(doc_ids):
            calls["n"] += 1
            return real(doc_ids)

        self.storage.get_documents = counting
        try:
            out = self.retriever.retrieve("vector embeddings dense cosine", k=10)
            self.assertTrue(out.results)
            rag_total = calls["n"]
            # baseline: the same shared-hybrid retrieval + rerank, no RAG
            calls["n"] = 0
            from nexus_search.ranking.query import understand_query
            from nexus_search.ranking.ranker import RankingWeights, rerank
            u = understand_query("vector embeddings dense cosine", self.storage)
            page = self.hybrid.search_page(
                "vector embeddings dense cosine", top_k=10, offset=0,
                group_chunks=False, mode=SearchMode.HYBRID, understanding=u)
            if page.results:
                rerank(page.results, "vector embeddings dense cosine",
                       weights=RankingWeights(), storage=self.storage,
                       understanding=u, link_intel=None)
            baseline = calls["n"]
            self.assertEqual(rag_total, baseline + 1,
                             "RAG must add exactly ONE batched text fetch")
        finally:
            self.storage.get_documents = real

    def test_filters_render_through_search_filter_semantics(self):
        # doc_type filter through the /search parser path
        out = self.retriever.retrieve("sitemap canonicalization", k=10,
                                      filters={"doc_type": "web"})
        self.assertTrue(out.results)
        self.assertEqual({c.chunk_id for c in out.results}, {"plain"})
        # language filter
        out2 = self.retriever.retrieve("sitemap canonicalization", k=10,
                                        filters={"language": "en"})
        self.assertEqual({c.chunk_id for c in out2.results}, {"plain"})
        # unknown filter key is a loud error, not a silent second semantics
        with self.assertRaises(ValueError):
            self.retriever.retrieve("x", filters={"evil": "1"})

    def test_search_bounds_reused(self):
        with self.assertRaises(ValueError):
            self.retriever.retrieve("   ")
        with self.assertRaises(ValueError):
            self.retriever.retrieve("x" * (MAX_QUERY_CHARS + 1))
        with self.assertRaises(ValueError):
            self.retriever.retrieve(" ".join(["w"] * (MAX_QUERY_WORDS + 1)))
        # k clamped, never an error
        out = self.retriever.retrieve("bm25", k=10_000)
        self.assertLessEqual(len(out.results), MAX_TOP_K)

    def test_language_recorded_in_metadata(self):
        out = self.retriever.retrieve("how does the index rank the documents")
        self.assertEqual(out.language, "en")
        out2 = self.retriever.retrieve("zxq vkn")
        self.assertEqual(out2.language, "unknown")

    def test_embedder_identity_exposed(self):
        self.assertEqual(self.retriever.embedder_name, "hash:384")

    def test_retrieval_error_is_type_name_only(self):
        class Boom:
            def search_page(self, *a, **kw):
                raise RuntimeError("sqlite disk at /secret/path leaked")

        retriever = RagRetriever(Boom(), self.storage)
        out = retriever.retrieve("bm25 lexical", k=5)
        self.assertEqual(out.results, [])
        self.assertIsNone(out.degraded_reason)
        self.assertEqual(out.retrieval_error, "retrieval_error")
        # no internals in the object a service would serialize
        self.assertNotIn("/secret/path", repr(out))


class TestRagRetrieverDegraded(unittest.TestCase):
    def test_vector_subsystem_down_degrades_to_keyword_loudly(self):
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "degraded.db")
        storage = Storage(db)
        try:
            indexer = Indexer(storage)
            indexer.add_document("d1", "bm25 lexical retrieval scoring content",
                                 title="Kw")
            bm25 = BM25Search(storage)
            retriever = RagRetriever(None, storage, bm25_search=bm25)
            self.assertIsNone(retriever.embedder_name)
            out = retriever.retrieve("bm25 lexical retrieval", k=5)
            self.assertTrue(out.degraded)
            self.assertEqual(out.degraded_reason, "vector_subsystem_unavailable")
            self.assertTrue(out.results)
            for c in out.results:
                self.assertIsNotNone(c.bm25_score)
                self.assertIsNone(c.vector_score)  # raw honesty: vector absent
        finally:
            storage.close()

    def test_hybrid_midflight_fallback_is_flagged(self):
        storage, hybrid, vs, sync = _build("fallback.db")
        try:
            class DyingVectorStore:
                """Vector side raises EmbedderUnavailable -> HybridSearch's
                own bm25 fallback path fires; the retriever must surface it
                as degraded, never silently swap."""
                embedder = hybrid.vector_store.embedder

                def search(self, *a, **kw):
                    from nexus_search.core.embedders import EmbedderUnavailable
                    raise EmbedderUnavailable("model died")

                def close(self):
                    pass

            hybrid.vector_store = DyingVectorStore()
            retriever = RagRetriever(hybrid, storage)
            out = retriever.retrieve("bm25 lexical inverted index", k=5)
            self.assertTrue(out.degraded)
            self.assertIn("embedder_unavailable", str(out.degraded_reason))
        finally:
            sync.close(); hybrid.close(); vs.close(); storage.close()


if __name__ == "__main__":
    unittest.main()
