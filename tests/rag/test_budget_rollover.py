"""UTC day-key budget behavior (mission C-data: UTC rollover tests) +
429 Retry-After correctness (review RETRY-AFTER).

The budget day key is UTC (UTC-1 fix); these tests pin the rollover
contract: usage recorded on one UTC day never counts against the next,
and the 429's Retry-After tells the client when the budget actually
resets (seconds to next UTC midnight), not a flat under-estimate.
"""
import calendar
import os
import tempfile
import time
import unittest
from unittest import mock

from nexus_search.core.storage import Storage
from nexus_search.rag.budget import BudgetExceeded, daily_budget_from_env, precheck


class TestBudgetUtcRollover(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.storage = Storage(self.path)

    def tearDown(self):
        self.storage.close()
        os.remove(self.path)

    def test_usage_never_leaks_across_utc_day_boundary(self):
        bucket = "key:rol"
        # 23:30 UTC on 2026-03-10
        late = calendar.timegm((2026, 3, 10, 23, 30, 0, 0, 0, 0))
        with mock.patch("time.time", return_value=late):
            self.storage.rag_usage_add(bucket, 900)
            # budget 1000: 900 spent -> 101 more is refused at 23:30...
            with self.assertRaises(BudgetExceeded):
                precheck(self.storage, bucket, 101, budget=1000)
            # ...but the SAME request succeeds 40 minutes later (next day)
            early = calendar.timegm((2026, 3, 11, 0, 10, 0, 0, 0, 0))
            with mock.patch("time.time", return_value=early):
                precheck(self.storage, bucket, 101, budget=1000)
        # the old day's usage is still recorded (audit), just not current
        self.assertEqual(
            self.storage.rag_usage_get(bucket, day="2026-03-10"), 900)

    def test_day_key_is_the_utc_calendar_day(self):
        bucket = "key:daykey"
        # 2026-03-10 23:00 UTC = 2026-03-11 09:00 in UTC+10: the key must
        # be the UTC day even though local calendars moved on
        ts = calendar.timegm((2026, 3, 10, 23, 0, 0, 0, 0, 0))
        with mock.patch("time.time", return_value=ts):
            self.storage.rag_usage_add(bucket, 50)
            self.assertEqual(self.storage.rag_usage_get(bucket), 50)
        self.assertEqual(
            self.storage.rag_usage_get(bucket, day="2026-03-10"), 50)
        self.assertEqual(
            self.storage.rag_usage_get(bucket, day="2026-03-11"), 0)

    def test_zero_budget_disables_the_gate(self):
        with mock.patch.dict(os.environ, {"NEXUS_RAG_DAILY_TOKEN_BUDGET": "0"}):
            self.assertEqual(daily_budget_from_env(), 0)
            # budget=0 means unlimited — documented
            precheck(self.storage, "any", 10**9)


class TestRetryAfterIsUtcMidnight(unittest.TestCase):
    """Review RETRY-AFTER: /ask's budget 429 hard-coded Retry-After: 3600,
    but with UTC day keys the budget can reset up to 24h away — the
    header must state the actual seconds to next UTC midnight."""

    def _seconds_to_next_utc_day(self, now):
        return max(1, ((int(now) // 86400) + 1) * 86400 - int(now))

    @unittest.skipIf(os.environ.get("NEXUS_SKIP_API") == "1", "api test")
    def test_ask_429_retry_after_matches_utc_reset(self):
        import importlib
        from fastapi.testclient import TestClient

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        env = {
            "NEXUS_ENV": "production", "NEXUS_API_KEY": "secret",
            "NEXUS_LLM_PROVIDER": "stub", "NEXUS_RAG_ALLOW_STUB": "1",
            "NEXUS_RAG_DAILY_TOKEN_BUDGET": "1", "NEXUS_DB": path,
        }
        saved = {k: os.environ.get(k) for k in
                 list(env) + ["NEXUS_RATE_LIMIT", "NEXUS_CACHE_TTL"]}
        try:
            for k, v in env.items():
                os.environ[k] = v
            os.environ["NEXUS_RATE_LIMIT"] = "0"
            from nexus_search.core import api
            api = importlib.reload(api)
            client = TestClient(api.app)
            client.post("/documents", json={
                "doc_id": "bm25-doc", "title": "BM25",
                "content": "BM25 scores lexical matches over an inverted "
                           "index for ranking."})
            # the FIRST ask spends the 1-token budget (worst-case estimate
            # alone exceeds it), the SECOND must 429 with the real reset
            client.post("/ask", json={"query": "bm25 lexical ranking"},
                        headers={"X-API-Key": "secret"})
            r = client.post("/ask", json={"query": "bm25 lexical ranking"},
                            headers={"X-API-Key": "secret"})
            self.assertEqual(r.status_code, 429, r.text)
            got = int(r.headers["retry-after"])
            expected = self._seconds_to_next_utc_day(time.time())
            # within 3 seconds of drift tolerance, and NOT a flat 3600
            self.assertLessEqual(abs(got - expected), 3,
                                 f"Retry-After {got} != seconds to UTC "
                                 f"midnight ~{expected}")
            self.assertGreaterEqual(got, 1)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            from tests._api_reset import reset_api
            reset_api()
            for _ in range(10):
                try:
                    os.remove(path)
                    break
                except PermissionError:
                    time.sleep(0.2)


if __name__ == "__main__":
    unittest.main()
