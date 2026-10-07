"""WP12-B3 (audit R4): metadata-only filter pushdown for vector search.

The old pushdown called allowed(doc_id) -> Storage.get_document (full
content!) for EVERY vector in the matrix — O(corpus) SQL reads per
filtered query. The new path builds the allowed-id set with ONE
metadata-only read (doc_id, doc_type, metadata — no content column) and
masks with np.isin. Semantics must be IDENTICAL to
filters.matches_filters (lower-cased compare, not_filters).
"""

import os
import tempfile
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:8")

import numpy as np  # noqa: E402

from nexus_search.core.embedders import get_embedder  # noqa: E402
from nexus_search.core.filters import matches_filters  # noqa: E402
from nexus_search.core.hybrid_search import HybridSearch, SearchMode  # noqa: E402
from nexus_search.core.indexer import Indexer  # noqa: E402
from nexus_search.core.query_parser import parse_query  # noqa: E402
from nexus_search.core.storage import Storage  # noqa: E402
from nexus_search.core.vector_store import VectorStoreManager  # noqa: E402

# (doc_type, language) mix: pdf/en, html/de, web/fr, text/it
TYPES = ["pdf", "html", "web", "text"]
LANGS = ["en", "de", "fr", "it"]


def _embedder():
    return get_embedder()


class _HashEmbedder8:
    name = "hash-8"
    dim = 8

    def embed_query(self, text):
        from nexus_search.core.embedders import HashEmbedder
        return HashEmbedder(dim=8).embed_query(text)

    def embed_documents(self, texts, batch_size=32):
        return [self.embed_query(t) for t in texts]


class FilterPushdownCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db = os.path.join(self.tmpdir, "pushdown.db")
        self.storage = Storage(self.db)
        self.indexer = Indexer(self.storage)
        self.vsm = VectorStoreManager(self.db, embedder=_embedder())
        from nexus_search.core.embedding_sync import EmbeddingSync
        self.sync = EmbeddingSync(self.vsm, batch_size=64)
        self.sync.attach(self.indexer)
        for i in range(120):
            t, l = TYPES[i % 4], LANGS[i % 4]
            content = f"common words alpha beta gamma doc number {i} of the corpus"
            self.indexer.add_document(f"doc{i}", content=content, title=f"title {i}",
                                      doc_type=t, metadata={"language": l})
        self.sync.flush()
        self.hs = HybridSearch(self.storage, vector_store=self.vsm, db_path=self.db)

    def tearDown(self):
        self.sync.close()
        self.vsm.close()
        self.storage.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _expected_ids(self, query):
        parsed = parse_query(query)
        out = set()
        for doc_id in self.storage.all_doc_ids():
            doc = self.storage.get_document(doc_id)
            if matches_filters(doc, parsed.filters, parsed.not_filters):
                out.add(doc_id)
        return out

    def test_filtered_semantic_parity_and_pushdown_read_count(self):
        """(a) Filtered SEMANTIC search returns exactly the docs the old
        matches_filters semantics allow, and (b) the filter pushdown does
        ONE metadata-only read + ZERO full-document reads, regardless of
        corpus size (the old path did one get_document per vector)."""
        for query in ("alpha + type:pdf", "alpha + lang:de",
                      "alpha - type:pdf", "alpha NOT type:web"):
            counter = {"get_document": 0, "meta": 0}
            orig_get = Storage.get_document
            orig_meta = Storage.get_documents_meta

            def counting_get(self, doc_id, _c=counter, _o=orig_get):
                _c["get_document"] += 1
                return _o(self, doc_id)

            def counting_meta(self, doc_ids=None, _c=counter, _o=orig_meta):
                _c["meta"] += 1
                return _o(self, doc_ids)

            Storage.get_document = counting_get
            Storage.get_documents_meta = counting_meta
            try:
                allowed = self.hs._build_allowed_ids(parse_query(query))
                qv = self.vsm.embedder.embed_query("alpha")
                self.vsm.store.search(qv, top_k=500, allowed_ids=allowed)
            finally:
                Storage.get_document = orig_get
                Storage.get_documents_meta = orig_meta
            self.assertEqual(counter["meta"], 1, f"{query}: exactly ONE metadata read")
            self.assertEqual(counter["get_document"], 0,
                             f"{query}: pushdown must not read full docs")

            # end-to-end parity on the same query
            page = self.hs.search_page(query, top_k=100, mode=SearchMode.SEMANTIC,
                                       group_chunks=False)
            expected = {d for d in self._expected_ids(query)
                        if "alpha" in self.storage.get_document(d).content}
            got = {r.doc_id for r in page.results}
            self.assertEqual(got, expected, query)

    def test_allowed_ids_mask_matches_callable_path(self):
        """VectorStore.search with the precomputed id-set mask returns
        EXACTLY what the old per-doc callable returned (doc_id + score
        tuples identical), for positive and negated filters."""
        parsed = parse_query("alpha + type:pdf")
        expected_set = self._expected_ids("alpha + type:pdf")
        qv = self.vsm.embedder.embed_query("alpha")
        via_callable = self.vsm.store.search(
            qv, top_k=50, allowed=lambda d: d in expected_set)
        via_mask = self.vsm.store.search(qv, top_k=50, allowed_ids=expected_set)
        self.assertEqual([(r.doc_id, r.score) for r in via_callable],
                         [(r.doc_id, r.score) for r in via_mask])

        parsed_neg = parse_query("alpha - type:pdf")
        neg_set = self._expected_ids("alpha - type:pdf")
        via_callable_neg = self.vsm.store.search(
            qv, top_k=50, allowed=lambda d: d in neg_set)
        via_mask_neg = self.vsm.store.search(qv, top_k=50, allowed_ids=neg_set)
        self.assertEqual([(r.doc_id, r.score) for r in via_callable_neg],
                         [(r.doc_id, r.score) for r in via_mask_neg])


if __name__ == "__main__":
    unittest.main()
