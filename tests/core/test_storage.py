import os
import tempfile
import unittest

from nexus_search.core.storage import Storage


class TestStorage(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.storage = Storage(self.path)

    def tearDown(self):
        self.storage.close()
        os.remove(self.path)

    def test_upsert_and_get_document(self):
        self.storage.upsert_document("d1", "Title", "Content here", "text", 2, {"k": "v"})
        self.storage.commit()
        doc = self.storage.get_document("d1")
        self.assertEqual(doc.title, "Title")
        self.assertEqual(doc.metadata, {"k": "v"})

    def test_get_missing_document_returns_none(self):
        self.assertIsNone(self.storage.get_document("missing"))

    def test_document_count(self):
        self.assertEqual(self.storage.document_count(), 0)
        self.storage.upsert_document("d1", "T", "C", "text", 1, {})
        self.storage.commit()
        self.assertEqual(self.storage.document_count(), 1)

    def test_upsert_overwrites_existing(self):
        self.storage.upsert_document("d1", "Old", "old content", "text", 2, {})
        self.storage.commit()
        self.storage.upsert_document("d1", "New", "new content", "text", 2, {})
        self.storage.commit()
        self.assertEqual(self.storage.document_count(), 1)
        self.assertEqual(self.storage.get_document("d1").title, "New")

    def test_postings_and_document_frequency(self):
        self.storage.upsert_document("d1", "T", "C", "text", 3, {})
        self.storage.add_postings("d1", {"hello": 2, "world": 1})
        self.storage.commit()
        self.assertEqual(self.storage.document_frequency("hello"), 1)
        self.assertEqual(self.storage.postings_for_term("hello"), [("d1", 2)])

    def test_reupsert_clears_old_postings(self):
        self.storage.upsert_document("d1", "T", "C1", "text", 1, {})
        self.storage.add_postings("d1", {"alpha": 1})
        self.storage.commit()
        self.storage.upsert_document("d1", "T", "C2", "text", 1, {})  # re-index, new postings below
        self.storage.add_postings("d1", {"beta": 1})
        self.storage.commit()
        self.assertEqual(self.storage.document_frequency("alpha"), 0)
        self.assertEqual(self.storage.document_frequency("beta"), 1)

    def test_delete_document_removes_postings(self):
        self.storage.upsert_document("d1", "T", "C", "text", 1, {})
        self.storage.add_postings("d1", {"hello": 1})
        self.storage.commit()
        self.assertTrue(self.storage.delete_document("d1"))
        self.assertEqual(self.storage.postings_for_term("hello"), [])

    def test_delete_nonexistent_returns_false(self):
        self.assertFalse(self.storage.delete_document("missing"))

    def test_average_length_empty_corpus(self):
        self.assertEqual(self.storage.average_length(), 0.0)

    def test_upsert_with_postings_is_atomic(self):
        """A failure mid-postings-insert must roll back BOTH the document row
        and any postings rows already written in that transaction."""
        # A None term in the batch makes sqlite fail on the SECOND
        # executemany row — after the document row AND the first postings row
        # have already been written inside the transaction. Genuine failure,
        # nothing mocked.
        with self.assertRaises(Exception):
            self.storage.upsert_document_with_postings(
                "boom", "T", "content about ok", "text", 2, {},
                term_freqs={"ok": 1, None: 1},
            )
        # row must NOT be left behind half-written
        self.assertIsNone(self.storage.get_document("boom"))
        self.assertEqual(self.storage.postings_for_term("ok"), [])

        # sanity: the normal path still works, and DB is usable after rollback
        self.storage.upsert_document_with_postings(
            "ok", "T", "content", "text", 1, {}, {"ok": 1})
        self.assertIsNotNone(self.storage.get_document("ok"))
        self.assertEqual(self.storage.postings_for_term("ok"), [("ok", 1)])

    def test_average_length_computed_correctly(self):
        self.storage.upsert_document("d1", "T", "C", "text", 10, {})
        self.storage.upsert_document("d2", "T", "C", "text", 20, {})
        self.storage.commit()
        self.assertEqual(self.storage.average_length(), 15.0)


if __name__ == "__main__":
    unittest.main()
