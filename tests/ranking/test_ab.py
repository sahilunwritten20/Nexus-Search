"""WP14 Item 5 — dedicated tests for ranking/ab.py (A/B instrumentation).

The Phase 7 plan cites `ExperimentLog.purge_older_than` as the PII-retention
precedent for RAG session memory, and `/search` buckets every session into a
variant via `assign_variant` — so this file pins BOTH contracts directly
(test_ranker.py exercises them indirectly; these are the dedicated cases).

What is deliberately NOT here: any "A/B analysis". No traffic exists; the
module docstring's honesty rule stands.
"""
import os
import tempfile
import time
import unittest

from nexus_search.ranking.ab import ExperimentLog, assign_variant


class TestAssignVariant(unittest.TestCase):
    def test_same_id_same_variant_across_calls(self):
        for uid in ("user-1", "session-42", "abc", "", "ünïcode-id"):
            with self.subTest(uid=uid):
                first = assign_variant(uid, ["control", "treatment"])
                self.assertEqual(first, assign_variant(uid, ["control", "treatment"]))

    def test_roughly_uniform_for_two_variants(self):
        n = 2000
        counts = {"control": 0, "treatment": 0}
        for i in range(n):
            counts[assign_variant(f"u{i}", ["control", "treatment"])] += 1
        # blake2b low bits: expect ~50/50; 45-55% band catches gross skew
        # while never failing on legitimate hash noise
        share = counts["control"] / n
        self.assertGreaterEqual(share, 0.45, counts)
        self.assertLessEqual(share, 0.55, counts)

    def test_variant_order_does_not_change_bucketing(self):
        # the hash bucket is position-independent of the label list
        a = assign_variant("u7", ["control", "treatment"])
        flipped = assign_variant("u7", ["treatment", "control"])
        labels = {"control": 0, "treatment": 1}
        self.assertEqual(labels[a] ^ 1, labels[flipped])

    def test_more_variants_all_reachable(self):
        seen = {assign_variant(f"u{i}", ["a", "b", "c", "d", "e"])
                for i in range(1000)}
        self.assertEqual(len(seen), 5)

    def test_empty_variant_list_raises(self):
        with self.assertRaises(ValueError):
            assign_variant("u1", [])


class TestExperimentLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wp14_ab_")
        self.path = os.path.join(self.tmp, "ab.db")
        self.log = ExperimentLog(self.path)

    def tearDown(self):
        self.log.close()

    def test_record_and_rows_for(self):
        self.log.record("alpha query", "treatment", "hybrid+rerank")
        self.log.record("alpha query", "treatment", "hybrid+rerank")
        self.log.record("other query", "control", "hybrid")
        rows = self.log.rows_for("alpha query")
        self.assertEqual(len(rows), 2)
        self.assertEqual({r[1] for r in rows}, {"treatment"})
        self.assertEqual({r[2] for r in rows}, {"hybrid+rerank"})
        self.assertEqual(self.log.count(), 3)

    def test_rows_for_unknown_query_empty(self):
        self.assertEqual(self.log.rows_for("never seen"), [])

    def test_purge_older_than_retention(self):
        # PHASE7_PLAN cites this as the PII-retention precedent: rows older
        # than the window are deleted, younger rows survive, idempotent.
        self.log.record("old query", "control", "hybrid")
        time.sleep(0.02)
        self.log.record("new query", "control", "hybrid")
        deleted = self.log.purge_older_than(days=1)   # everything is younger
        self.assertEqual(deleted, 0)
        self.assertEqual(self.log.count(), 2)
        # degenerate window must raise, never silently purge everything
        with self.assertRaises(ValueError):
            self.log.purge_older_than(days=0)

    def test_purge_deletes_only_aged_rows(self):
        # insert with an explicitly aged timestamp via direct SQL
        import sqlite3
        aged = time.time() - 40 * 86400
        with self.log.lock:
            self.log.conn.execute(
                "INSERT INTO query_experiments (query, variant, ranking_mode, created_at)"
                " VALUES ('ancient', 'control', 'hybrid', ?)", (aged,))
            self.log.conn.commit()
        self.log.record("fresh", "control", "hybrid")
        deleted = self.log.purge_older_than(days=30)
        self.assertEqual(deleted, 1)
        self.assertEqual(self.log.count(), 1)
        self.assertEqual(self.log.rows_for("fresh")[0][0], "fresh")
        self.assertEqual(self.log.rows_for("ancient"), [])


if __name__ == "__main__":
    unittest.main()
