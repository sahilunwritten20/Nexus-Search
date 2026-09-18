import os
import tempfile
import unittest

from nexus_search.core.indexer import Indexer
from nexus_search.core.storage import Storage


class TestIndexer(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.storage = Storage(self.path)
        self.indexer = Indexer(self.storage)

    def tearDown(self):
        self.storage.close()
        os.remove(self.path)

    def test_add_document_creates_postings(self):
        self.indexer.add_document("d1", "the quick brown fox", title="Fox")
        self.assertEqual(self.storage.document_frequency("fox"), 1)
        self.assertEqual(self.storage.document_frequency("quick"), 1)

    def test_title_included_in_tokenization(self):
        self.indexer.add_document("d1", "some content", title="UniqueTitleWord")
        self.assertEqual(self.storage.document_frequency("uniquetitleword"), 1)

    def test_reindexing_replaces_postings(self):
        self.indexer.add_document("d1", "alpha beta")
        self.indexer.add_document("d1", "gamma delta")  # re-index same doc_id
        self.assertEqual(self.storage.document_frequency("alpha"), 0)
        self.assertEqual(self.storage.document_frequency("gamma"), 1)

    def test_document_length_matches_token_count(self):
        self.indexer.add_document("d1", "one two three", title="")
        doc = self.storage.get_document("d1")
        self.assertEqual(doc.length, 3)

    def test_metadata_stored(self):
        self.indexer.add_document("d1", "content", metadata={"source": "test"})
        doc = self.storage.get_document("d1")
        self.assertEqual(doc.metadata, {"source": "test"})

    def test_delete_document(self):
        self.indexer.add_document("d1", "content")
        self.assertTrue(self.indexer.delete_document("d1"))
        self.assertIsNone(self.storage.get_document("d1"))


if __name__ == "__main__":
    unittest.main()
