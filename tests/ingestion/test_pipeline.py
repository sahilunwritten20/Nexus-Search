import os
import tempfile
import unittest

from nexus_search.core.indexer import Indexer
from nexus_search.core.storage import Storage
from nexus_search.ingestion.dedup import Deduplicator, content_hash
from nexus_search.ingestion.pipeline import ingest_documents, make_crawler_ingest_fn
from nexus_search.ingestion.types import IngestDoc


class TestContentHash(unittest.TestCase):
    def test_stable_across_case_and_whitespace(self):
        self.assertEqual(content_hash("Hello   World"), content_hash("hello world"))

    def test_different_content_different_hash(self):
        self.assertNotEqual(content_hash("Hello"), content_hash("Goodbye"))


class TestDeduplicator(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.dedup = Deduplicator(self.path)

    def tearDown(self):
        self.dedup.close()
        os.remove(self.path)

    def test_new_content_is_not_duplicate(self):
        self.assertFalse(self.dedup.is_duplicate("some content"))

    def test_registered_content_is_duplicate(self):
        self.dedup.register("some content", "doc1")
        self.assertTrue(self.dedup.is_duplicate("some content"))

    def test_duplicate_check_is_case_and_whitespace_insensitive(self):
        self.dedup.register("Hello   World", "doc1")
        self.assertTrue(self.dedup.is_duplicate("hello world"))


class TestIngestDocuments(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.storage = Storage(self.path)
        self.indexer = Indexer(self.storage)
        self.dedup = Deduplicator(self.path)

    def tearDown(self):
        self.storage.close()
        self.dedup.close()
        os.remove(self.path)

    def test_indexes_new_documents(self):
        docs = [IngestDoc(doc_id="d1", title="T", content="unique content here", doc_type="file")]
        stats = ingest_documents(docs, self.indexer, self.dedup)
        self.assertEqual(stats, {"indexed": 1, "duplicates": 0})
        self.assertEqual(self.storage.document_count(), 1)

    def test_skips_duplicate_content_across_different_doc_ids(self):
        docs = [
            IngestDoc(doc_id="d1", title="T1", content="identical content", doc_type="file"),
            IngestDoc(doc_id="d2", title="T2", content="identical content", doc_type="web"),
        ]
        stats = ingest_documents(docs, self.indexer, self.dedup)
        self.assertEqual(stats, {"indexed": 1, "duplicates": 1})
        self.assertEqual(self.storage.document_count(), 1)


class TestCrawlerIngestAdapter(unittest.TestCase):
    """This is the actual Phase 3 <-> Phase 2 integration point."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.storage = Storage(self.path)
        self.indexer = Indexer(self.storage)
        self.dedup = Deduplicator(self.path)
        self.ingest_fn = make_crawler_ingest_fn(self.indexer, self.dedup)

    def tearDown(self):
        self.storage.close()
        self.dedup.close()
        os.remove(self.path)

    def test_matches_crawler_expected_signature(self):
        # CrawlPipeline calls ingest_fn(url, title, text, metadata) -> None
        self.ingest_fn("https://example.com/page", "Page Title", "page content here", {"language": "en"})
        self.assertEqual(self.storage.document_count(), 1)

    def test_indexed_document_is_searchable(self):
        self.ingest_fn("https://example.com/page", "Page Title", "unique searchable phrase", {})
        doc = self.storage.get_document("web:https://example.com/page")
        self.assertIsNotNone(doc)
        self.assertIn("unique searchable phrase", doc.content)

    def test_duplicate_crawled_page_is_skipped(self):
        self.ingest_fn("https://example.com/a", "A", "same content", {})
        self.ingest_fn("https://example.com/b", "B", "same content", {})
        self.assertEqual(self.storage.document_count(), 1)


if __name__ == "__main__":
    unittest.main()
