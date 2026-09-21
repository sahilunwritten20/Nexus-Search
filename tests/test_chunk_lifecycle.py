"""Chunk lifecycle + Phase-4 seams: grouping, cascade delete, crawl-path chunking,
index hooks, raw query text, shared filters."""
import os
import tempfile
import unittest

from nexus_search.core.bm25 import BM25Search
from nexus_search.core.filters import matches_filters
from nexus_search.core.indexer import Indexer
from nexus_search.core.query_parser import parse_query
from nexus_search.core.storage import Storage
from nexus_search.ingestion.dedup import Deduplicator
from nexus_search.ingestion.pipeline import ingest_documents, make_crawler_ingest_fn
from nexus_search.ingestion.types import IngestDoc

LONG = " ".join(f"sentence {i} about vector search and ranking." for i in range(120))


class Base(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.storage = Storage(self.path)
        self.indexer = Indexer(self.storage)
        self.dedup = Deduplicator(self.path)
        self.search = BM25Search(self.storage)

    def tearDown(self):
        self.storage.close()
        self.dedup.close()

    def ingest_long(self, doc_id="file:big.txt"):
        ingest_documents([IngestDoc(doc_id, "Big", LONG, "file", {"path": "big.txt"})],
                         self.indexer, self.dedup, chunk_size=400)


class TestChunkGrouping(Base):
    def test_hits_collapse_to_one_result_per_parent(self):
        self.ingest_long()
        page = self.search.search_page("vector search", top_k=10)
        self.assertEqual(page.total, 1)
        top = page.results[0]
        self.assertEqual(top.doc_id, "file:big.txt")  # parent id, not the chunk id
        self.assertTrue(top.chunk_id.startswith("file:big.txt#chunk"))
        self.assertGreater(top.matched_chunks, 1)
        self.assertEqual(top.metadata["parent_id"], "file:big.txt")
        self.assertEqual(top.metadata["path"], "big.txt")

    def test_ungrouped_search_returns_raw_chunks(self):
        self.ingest_long()
        page = self.search.search_page("vector search", top_k=50, group_chunks=False)
        self.assertGreater(page.total, 1)
        self.assertTrue(all("#chunk" in r.doc_id for r in page.results))

    def test_unchunked_docs_are_unaffected(self):
        self.indexer.add_document("a", "alpha beta", "A")
        r = self.search.search("alpha")[0]
        self.assertEqual((r.doc_id, r.chunk_id, r.matched_chunks), ("a", None, 1))


class TestCascadeDelete(Base):
    def test_deleting_parent_removes_its_chunks(self):
        self.ingest_long()
        self.assertGreater(len(self.storage.chunk_ids("file:big.txt")), 1)
        self.assertTrue(self.indexer.delete_document("file:big.txt"))
        self.assertEqual(self.storage.chunk_ids("file:big.txt"), [])
        self.assertEqual(self.search.search("vector"), [])

    def test_delete_does_not_touch_lookalike_ids(self):
        self.indexer.add_document("doc", "alpha")
        self.indexer.add_document("doc#chunk-notes", "alpha")  # not a real chunk id
        self.indexer.delete_document("doc")
        self.assertIsNotNone(self.storage.get_document("doc#chunk-notes"))

    def test_deleted_page_can_be_reingested(self):
        self.ingest_long()
        self.indexer.delete_document("file:big.txt")
        self.ingest_long()
        self.assertGreater(len(self.storage.chunk_ids("file:big.txt")), 1)


class TestCrawlPathChunks(Base):
    def test_crawled_page_is_chunked_like_files(self):
        ingest = make_crawler_ingest_fn(self.indexer, self.dedup, chunk_size=400)
        ingest("http://x.test/long", "Long", LONG, {"url": "http://x.test/long"})
        self.assertGreater(len(self.storage.chunk_ids("web:http://x.test/long")), 1)
        r = self.search.search("ranking")[0]
        self.assertEqual((r.doc_id, r.metadata["url"]), ("web:http://x.test/long", "http://x.test/long"))

    def test_quality_gate_applies_to_crawled_pages(self):
        ingest = make_crawler_ingest_fn(self.indexer, self.dedup, min_quality=0.9)
        ingest("http://x.test/junk", "J", "hi", {})
        self.assertEqual(self.storage.document_count(), 0)


class TestIndexHooks(Base):
    def test_listeners_fire_after_index_and_delete(self):
        seen = {"indexed": [], "deleted": []}
        self.indexer.subscribe(on_indexed=seen["indexed"].append, on_deleted=seen["deleted"].append)
        self.ingest_long()
        chunks = self.storage.chunk_ids("file:big.txt")
        self.assertEqual(sorted(seen["indexed"]), sorted(chunks))
        self.indexer.delete_document("file:big.txt")
        self.assertEqual(sorted(seen["deleted"]), sorted(chunks))

    def test_failing_listener_never_breaks_indexing(self):
        def boom(_):
            raise RuntimeError("embedder down")
        self.indexer.subscribe(on_indexed=boom)
        self.indexer.add_document("a", "alpha")
        self.assertIsNotNone(self.storage.get_document("a"))


class TestQuerySeams(unittest.TestCase):
    def test_parsed_query_keeps_raw_text_for_embedding(self):
        q = parse_query('"machine learning" type:pdf lang:en neural nets')
        self.assertEqual(q.text, "machine learning neural nets")
        self.assertEqual(q.filters, {"doc_type": "pdf", "language": "en"})

    def test_shared_filter_function(self):
        class D:
            doc_type, metadata = "pdf", {"language": "en"}
        self.assertTrue(matches_filters(D, {"doc_type": "pdf", "language": "en"}))
        self.assertFalse(matches_filters(D, {"doc_type": "web"}))


if __name__ == "__main__":
    unittest.main()