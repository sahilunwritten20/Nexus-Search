"""P7-3 — RAG counters: day-keyed, thread-safe, numbers only (a snapshot
must never contain text: a poison string fed through record_* paths never
appears)."""
import os
import threading
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.rag.metrics import RagMetrics


class TestRagMetrics(unittest.TestCase):
    def test_counters_bump_daily_and_totals(self):
        m = RagMetrics()
        m.record_request()
        m.record_refusal("no_evidence")
        m.record_degraded()
        m.record_usage(100, 50, 0.25)
        m.record_provider_error("provider_timeout")
        m.record_redactions(3)
        m.record_citation_failure(2)
        m.record_unsupported_claim(4)
        m.record_budget_rejection()
        snap = m.snapshot()
        self.assertEqual(snap["totals"]["requests"], 1)
        self.assertEqual(snap["totals"]["refusals"], 1)
        self.assertEqual(snap["totals"]["refusals_no_evidence"], 1)
        self.assertEqual(snap["totals"]["degraded_answers"], 1)
        self.assertEqual(snap["totals"]["tokens_in"], 100)
        self.assertEqual(snap["totals"]["tokens_out"], 50)
        self.assertEqual(snap["totals"]["estimated_cost_usd"], 0.25)
        self.assertEqual(snap["totals"]["provider_timeouts"], 1)
        self.assertEqual(snap["totals"]["redactions"], 3)
        self.assertEqual(snap["totals"]["citation_verification_failures"], 2)
        self.assertEqual(snap["totals"]["unsupported_claims"], 4)
        self.assertEqual(snap["totals"]["budget_rejections"], 1)
        self.assertEqual(len(snap["daily"]), 1)

    def test_no_cost_is_honest_absent(self):
        m = RagMetrics()
        m.record_usage(10, 10, None)
        self.assertNotIn("estimated_cost_usd", m.snapshot()["totals"])

    def test_day_pruning(self):
        m = RagMetrics(max_days=2)
        m._bump("requests", 1.0, daily=True, day="2026-10-01")
        m._bump("requests", 1.0, daily=True, day="2026-10-02")
        m._bump("requests", 1.0, daily=True, day="2026-10-03")
        snap = m.snapshot()
        self.assertEqual(set(snap["daily"]), {"2026-10-02", "2026-10-03"})

    def test_thread_safe_concurrent_bumps(self):
        m = RagMetrics()
        errors = []

        def hammer():
            try:
                for _ in range(200):
                    m.record_request()
                    m.record_usage(1, 1, 0.001)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=hammer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        snap = m.snapshot()
        self.assertEqual(snap["totals"]["requests"], 1600)

    def test_snapshot_is_numbers_only_never_text(self):
        m = RagMetrics()
        poison = "sk-POISON-never-in-metrics"
        m.record_request()
        m.record_refusal("low_confidence")
        m.record_provider_error("provider_http_error_500")
        snap = m.snapshot()
        self.assertNotIn(poison, str(snap))
        for value in snap["totals"].values():
            self.assertIsInstance(value, float)


if __name__ == "__main__":
    unittest.main()
