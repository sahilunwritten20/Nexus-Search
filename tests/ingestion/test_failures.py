"""Tests for Phase 2 fix #10 — ingestion retry / dead-letter queue."""
import os
import shutil
import tempfile
import time
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.core.indexer import Indexer
from nexus_search.core.storage import Storage
from nexus_search.ingestion.dedup import Deduplicator
from nexus_search.ingestion.failures import FailureQueue
from nexus_search.ingestion.history import ContentHistory
from nexus_search.ingestion.pipeline import ingest_one
from nexus_search.ingestion.types import IngestDoc


def _doc(doc_id, content="some content here"):
    return IngestDoc(doc_id, "T", content, "text", {})


class TestFailureQueue(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "t.db")
        self.q = FailureQueue(self.path)

    def tearDown(self):
        self.q.close()
        for _ in range(10):
            try:
                shutil.rmtree(self.dir)
                break
            except PermissionError:
                time.sleep(0.05)

    def test_record_and_due(self):
        self.q.record(_doc("d1"), "fake error")
        self.assertEqual(self.q.count(), 1)
        now = time.time()
        self.assertEqual(self.q.due(now=now), [])       # backoff not elapsed
        self.assertEqual(len(self.q.due(now=now + 60)), 1)  # later: due

    def test_repeat_failure_bumps_attempts_and_backoff(self):
        self.q.record(_doc("d1"), "err")
        t0 = time.time()
        r1 = self.q.list_all()[0]
        self.q.record(_doc("d1"), "err again")
        r2 = self.q.list_all()[0]
        self.assertEqual(self.q.count(), 1)             # same row, not duplicate
        self.assertEqual(r2.attempts, 2)
        self.assertGreater(r2.next_retry_at, r1.next_retry_at)  # backoff grew

    def test_exhausted_rows_not_due(self):
        q = FailureQueue(self.path, max_attempts=2)
        self.q.close()
        self.q = q
        self.q.record(_doc("d1"), "err")
        self.q.record(_doc("d1"), "err")   # attempts=2 == max
        self.assertEqual(self.q.due(now=time.time() + 9999), [])
        self.assertEqual(len(self.q.list_all()), 1)     # still listed for ops

    def test_replay_recovers_success(self):
        self.q.record(_doc("ok-doc", "a body that works"), "x")
        recovered = []

        def ok_ingest(d):
            recovered.append(d.doc_id)

        stats = self.q.replay(ok_ingest, now=time.time() + 9999)
        self.assertEqual(stats["recovered"], 1)
        self.assertEqual(recovered, ["ok-doc"])
        self.assertEqual(self.q.count(), 0)

    def test_replay_records_repeat_failure(self):
        self.q.record(_doc("bad-doc"), "x")

        def boom(d):
            raise RuntimeError("still broken")

        stats = self.q.replay(boom, now=time.time() + 9999)
        self.assertEqual(stats["failed_again"], 1)
        row = self.q.list_all()[0]
        self.assertEqual(row.attempts, 2)

    def test_delete(self):
        self.q.record(_doc("a"), "x")
        row = self.q.list_all()[0]
        self.assertTrue(self.q.delete(row.id))
        self.assertEqual(self.q.count(), 0)


class TestPipelineFailureQueue(unittest.TestCase):
    """ingest_one must dead-letter a failing doc but keep behavior otherwise."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "t.db")
        self.storage = Storage(self.path)
        self.indexer = Indexer(self.storage)
        self.dedup = Deduplicator(self.path)
        self.q = FailureQueue(self.path)

    def tearDown(self):
        self.q.close()
        self.dedup.close()
        self.storage.close()
        for _ in range(10):
            try:
                shutil.rmtree(self.dir)
                break
            except PermissionError:
                time.sleep(0.05)

    def test_failure_is_recorded_and_reraised(self):
        class Boom:
            def __init__(self):
                self.doc_id = "bad"
                self.metadata = {}

        # force a genuine failure inside the pipeline: content that breaks
        # quality scoring (None) — nothing mocked, the bug is in the input
        bad = IngestDoc("bad", "T", None, "text", {})
        with self.assertRaises(Exception):
            ingest_one(bad, self.indexer, self.dedup, failure_queue=self.q)
        self.assertEqual(self.q.count(), 1)
        self.assertIn("bad", self.q.list_all()[0].doc_id)

    def test_success_path_unaffected_by_queue(self):
        status = ingest_one(_doc("good"), self.indexer, self.dedup, failure_queue=self.q)
        self.assertEqual(status, "indexed")
        self.assertEqual(self.q.count(), 0)

    def test_no_queue_still_propagates(self):
        bad = IngestDoc("bad", "T", None, "text", {})
        with self.assertRaises(Exception):
            ingest_one(bad, self.indexer, self.dedup)  # no queue: same as before


class TestContentHistory(unittest.TestCase):
    """Content versioning: overwrites record history; first ingests don't."""

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

    def test_change_records_previous_content(self):
        ingest_one(_doc("d", "version one body"), self.indexer, self.dedup,
                   history=self.history)
        ingest_one(_doc("d", "version two body"), self.indexer, self.dedup,
                   history=self.history)
        rows = self.history.history_for("d")
        self.assertEqual(len(rows), 1)  # only the CHANGE, not the first write
        prev_hash, prev_content, new_hash, recorded_at = rows[0]
        self.assertEqual(prev_content, "version one body")
        self.assertNotEqual(prev_hash, new_hash)

    def test_first_ingest_not_recorded(self):
        ingest_one(_doc("d", "first body"), self.indexer, self.dedup,
                   history=self.history)
        self.assertEqual(self.history.count("d"), 0)

    def test_unchanged_reingest_not_recorded(self):
        ingest_one(_doc("d", "same body"), self.indexer, self.dedup,
                   history=self.history)
        ingest_one(_doc("d", "same body"), self.indexer, self.dedup,
                   history=self.history)
        self.assertEqual(self.history.count("d"), 0)

    def test_history_optional(self):
        # without history, nothing is recorded — no crash, no table writes
        ingest_one(_doc("d", "v1"), self.indexer, self.dedup)
        ingest_one(_doc("d", "v2"), self.indexer, self.dedup)
        self.assertEqual(self.history.count("d"), 0)


if __name__ == "__main__":
    unittest.main()
