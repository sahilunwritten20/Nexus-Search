"""Dedicated tests for ingestion/history.py (content versioning).

Covers the module directly AND its wiring through ingest_one — the pipeline
is the only production caller, so both levels are checked here.
(Pipeline first-ingest / change / identical-reingest cases are also in
tests/ingestion/test_failures.py::TestContentHistory; this file adds the
per-doc_id ordering/isolation behavior those don't cover.)
"""
import os
import shutil
import tempfile
import time
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.core.indexer import Indexer
from nexus_search.core.storage import Storage
from nexus_search.ingestion.dedup import Deduplicator, content_hash
from nexus_search.ingestion.history import ContentHistory
from nexus_search.ingestion.pipeline import ingest_one
from nexus_search.ingestion.types import IngestDoc


def _doc(doc_id, content):
    return IngestDoc(doc_id, "T", content, "text", {})


class TestContentHistoryModule(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "t.db")
        self.history = ContentHistory(self.path)

    def tearDown(self):
        self.history.close()
        for _ in range(10):
            try:
                shutil.rmtree(self.dir)
                break
            except PermissionError:
                time.sleep(0.05)

    def test_record_and_query_roundtrip(self):
        self.history.record_change("d1", "h1", "old text", "h2")
        rows = self.history.history_for("d1")
        self.assertEqual(len(rows), 1)
        prev_hash, prev_content, new_hash, recorded_at = rows[0]
        self.assertEqual((prev_hash, prev_content, new_hash), ("h1", "old text", "h2"))
        self.assertGreater(recorded_at, 0)

    def test_per_doc_isolation_and_order(self):
        # interleaved writes to two docs; each doc's history must contain
        # only its own rows, in insertion order
        self.history.record_change("a", "h0", "a v1", "h1")
        self.history.record_change("b", "k0", "b v1", "k1")
        self.history.record_change("a", "h1", "a v2", "h2")

        rows_a = self.history.history_for("a")
        self.assertEqual(len(rows_a), 2)
        self.assertEqual([r[0] for r in rows_a], ["h0", "h1"])  # prev_hash chain
        self.assertEqual([r[2] for r in rows_a], ["h1", "h2"])  # new_hash chain
        self.assertLessEqual(rows_a[0][3], rows_a[1][3])        # recorded_at sane

        rows_b = self.history.history_for("b")
        self.assertEqual(len(rows_b), 1)
        self.assertEqual(self.history.count(), 3)
        self.assertEqual(self.history.count("a"), 2)
        self.assertEqual(self.history.count("b"), 1)


class TestContentHistoryPipelineOrder(unittest.TestCase):
    """Multiple successive edits through the real ingest path chain properly:
    each row's prev_hash equals the previous row's new_hash."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "t.db")
        self.storage = Storage(self.path)
        self.indexer = Indexer(self.storage)
        self.dedup = Deduplicator(self.path)
        self.history = ContentHistory(self.path)

    def tearDown(self):
        self.history.close()
        self.dedup.close()
        self.storage.close()
        for _ in range(10):
            try:
                shutil.rmtree(self.dir)
                break
            except PermissionError:
                time.sleep(0.05)

    def test_successive_edits_chain_in_order(self):
        versions = ["body one", "body two", "body three"]
        for body in versions:
            status = ingest_one(_doc("d", body), self.indexer, self.dedup,
                                history=self.history)
            self.assertEqual(status, "indexed")

        rows = self.history.history_for("d")
        self.assertEqual(len(rows), 2)  # two CHANGES; first ingest recorded nothing
        # row 1: one -> two; row 2: two -> three
        self.assertEqual(rows[0][1], "body one")   # prev_content preserved
        self.assertEqual(rows[1][1], "body two")
        self.assertEqual(rows[0][2], rows[1][0])   # new_hash of one edit is the
        self.assertEqual(rows[1][2], content_hash("body three"))  # next edit's prev
        self.assertEqual(rows[0][0], content_hash("body one"))


if __name__ == "__main__":
    unittest.main()
