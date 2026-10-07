"""WP12-B4 (audit R3): hybrid search must not do per-result
Storage.get_document calls. Every stage (BM25 candidate gates + chunk
grouping, vector-candidate gates, merge, semantic loop, diversify) uses
ONE batched Storage.get_documents; a hybrid query therefore issues ZERO
get_document calls and a bounded number of batched reads regardless of
corpus size (reviewer measured 886/1,661 per-query calls).
"""

import os
import tempfile
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.core.embedders import HashEmbedder  # noqa: E402
from nexus_search.core.embedding_sync import EmbeddingSync  # noqa: E402
from nexus_search.core.hybrid_search import HybridSearch, SearchMode  # noqa: E402
from nexus_search.core.indexer import Indexer  # noqa: E402
from nexus_search.core.storage import Storage  # noqa: E402
from nexus_search.core.vector_store import VectorStoreManager  # noqa: E402

N_DOCS = 300
VOCAB = [f"w{i}" for i in range(400)]
WORDS = 120


class HybridNoNPlusOne(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import random
        cls.tmpdir = tempfile.mkdtemp()
        cls.db = os.path.join(cls.tmpdir, "n1.db")
        cls.storage = Storage(cls.db)
        cls.indexer = Indexer(cls.storage)
        cls.vsm = VectorStoreManager(cls.db, embedder=HashEmbedder(dim=384))
        cls.sync = EmbeddingSync(cls.vsm, batch_size=64)
        cls.sync.attach(cls.indexer)
        rng = random.Random(42)
        for i in range(N_DOCS):
            content = " ".join(rng.choice(VOCAB) for _ in range(WORDS))
            cls.indexer.add_document(f"doc{i}", content=content, title=f"t{i}")
        cls.sync.flush()
        cls.hs = HybridSearch(cls.storage, vector_store=cls.vsm, db_path=cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.sync.close()
        cls.vsm.close()
        cls.storage.close()
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _counted_search(self, query, **kw):
        counter = {"get_document": 0, "get_documents": 0, "meta": 0}
        orig_get = Storage.get_document
        orig_docs = Storage.get_documents
        orig_meta = Storage.get_documents_meta

        def counting_get(self, doc_id):
            counter["get_document"] += 1
            return orig_get(self, doc_id)

        def counting_docs(self, doc_ids):
            counter["get_documents"] += 1
            return orig_docs(self, doc_ids)

        def counting_meta(self, doc_ids=None):
            counter["meta"] += 1
            return orig_meta(self, doc_ids)

        Storage.get_document = counting_get
        Storage.get_documents = counting_docs
        Storage.get_documents_meta = counting_meta
        try:
            page = self.hs.search_page(query, top_k=10, **kw)
        finally:
            Storage.get_document = orig_get
            Storage.get_documents = orig_docs
            Storage.get_documents_meta = orig_meta
        return page, counter

    def test_hybrid_query_zero_get_document_bounded_batches(self):
        for q in ("w5", "w5 w6"):
            page, c = self._counted_search(q, mode=SearchMode.HYBRID)
            self.assertEqual(len(page.results), 10, q)
            self.assertEqual(c["get_document"], 0,
                             f"{q}: per-result get_document is the N+1 (audit R3)")
            self.assertLessEqual(c["get_documents"] + c["meta"], 5,
                                 f"{q}: batched reads must be stage-bounded: {c}")

    def test_semantic_and_diversity_paths_bounded(self):
        for kw in ({"mode": SearchMode.SEMANTIC},
                   {"mode": SearchMode.HYBRID, "diversity": 0.5},
                   {"mode": SearchMode.HYBRID, "sort": "title"}):
            page, c = self._counted_search("w5 w6", **kw)
            self.assertTrue(page.results, kw)
            self.assertEqual(c["get_document"], 0, kw)
            self.assertLessEqual(c["get_documents"] + c["meta"], 5, (kw, c))


if __name__ == "__main__":
    unittest.main()
