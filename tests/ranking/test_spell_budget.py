"""WP1 / BUG-01 regression tests: spell-correction work is bounded.

The pre-fix code expanded the FULL distance-2 edit frontier per
out-of-vocabulary term, so a 229-char junk query burned ~5 s CPU and a
2000-char one ~43 s — an availability hole on the open /search endpoint.

These tests pin the contract added with the fix:
- junk/repeated-OOV/unicode-junk inputs finish in bounded time
- terms longer than the max length are never corrected
- at most NEXUS_SPELL_MAX_CORRECTED_TERMS terms are corrected per query
- budget exhaustion returns the un-rewritten query with a flag, never raises
- real typo corrections still work (quality unchanged)
"""
import os
import shutil
import tempfile
import time
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.core.indexer import Indexer
from nexus_search.core.storage import Storage
from nexus_search.ranking.query import (
    QueryUnderstanding, correct_spelling, understand_query,
)
import nexus_search.ranking.query as query_mod


class _JunkStorage(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.storage = Storage(os.path.join(self.dir, "t.db"))
        Indexer(self.storage).add_document(
            "d1", "python programming language tutorial", title="Python")

    def tearDown(self):
        self.storage.close()
        for _ in range(10):
            try:
                shutil.rmtree(self.dir)
                break
            except PermissionError:
                time.sleep(0.05)

    @staticmethod
    def _junk(n_chars: int) -> str:
        q, i = "", 0
        while len(q) < n_chars:
            q += f" zqvxj{i}"
            i += 1
        return q[:n_chars].strip()


class TestSpellBudget(_JunkStorage):
    """Adversarial inputs must be bounded-time. Threshold is 0.35 s per query
    (audit measured 4.9-43 s pre-fix; CI variance gets ~350x headroom over the
    post-fix ~1-5 ms real cost, recorded in docs/AUDIT_REMEDIATION.md)."""

    BUDGET_S = 0.35

    def test_junk_query_bounded_at_audit_sizes(self):
        for n_chars in (229, 629, 1689, 2000):
            with self.subTest(n_chars=n_chars):
                t0 = time.perf_counter()
                understand_query(self._junk(n_chars), self.storage)
                self.assertLess(time.perf_counter() - t0, self.BUDGET_S)

    def test_repeated_oov_term_bounded(self):
        q = " ".join(["pythom"] * 150)
        t0 = time.perf_counter()
        understand_query(q, self.storage)
        self.assertLess(time.perf_counter() - t0, self.BUDGET_S)

    def test_unicode_junk_bounded(self):
        q = (" ".join(f"ｚｑｖｘｊ{i}" for i in range(60)) + " "
             + " ".join(f"发布{i}" for i in range(60)))
        t0 = time.perf_counter()
        understand_query(q, self.storage)
        self.assertLess(time.perf_counter() - t0, self.BUDGET_S)

    def test_long_term_never_corrected(self):
        term = "x" * 25  # default NEXUS_SPELL_MAX_TERM_LEN is 20
        t0 = time.perf_counter()
        self.assertIsNone(correct_spelling(term, {"xxxx"}))
        self.assertLess(time.perf_counter() - t0, 0.05)
        u = understand_query(f"{term} python", self.storage)
        self.assertEqual(u.corrected_terms, {})


class TestSpellTermCap(_JunkStorage):
    """At most NEXUS_SPELL_MAX_CORRECTED_TERMS (default 8) corrections, and
    the selection is deterministic (sorted term order)."""

    def _vocab_storage(self):
        # 12 in-vocabulary words w1..w12; query uses 1-edit variants of each.
        for i in range(12):
            self.storage.conn.execute(
                "INSERT OR REPLACE INTO postings (term, doc_id, term_freq) "
                "VALUES (?, 'v', 1)", (f"vword{i}",))
        self.storage.conn.commit()

    def test_corrected_terms_capped_and_deterministic(self):
        self._vocab_storage()
        q = " ".join(f"vword{i}x" for i in range(12))  # 12 OOV 1-edit variants
        first = understand_query(q, self.storage)
        second = understand_query(q, self.storage)
        self.assertLessEqual(len(first.corrected_terms), 8)
        self.assertEqual(first.corrected_terms, second.corrected_terms)
        # deterministic subset = the first 8 sorted OOV terms
        expected_terms = sorted(f"vword{i}x" for i in range(12))[:8]
        self.assertEqual(sorted(first.corrected_terms), expected_terms)

    def test_budget_exhaustion_returns_original_with_flag(self):
        # Budget unit = vocabulary comparisons in the distance window. The
        # fixture vocab's window for "pythom" holds 4 words; a budget of 3
        # aborts the scan mid-window -> the term stays uncorrected and the
        # exhaustion flag is set. Never an error.
        saved = os.environ.get("NEXUS_SPELL_CANDIDATE_BUDGET")
        os.environ["NEXUS_SPELL_CANDIDATE_BUDGET"] = "3"
        try:
            u = understand_query("pythom tutorial", self.storage)
            self.assertEqual(u.corrected_terms, {})  # scan aborted before finishing
            self.assertTrue(u.spelling_exhausted)
            # never an error: the un-rewritten terms are what retrieval sees
            self.assertEqual(u.effective_terms,
                             QueryUnderstanding(
                                 original=u.original, normalized=u.normalized
                             ).effective_terms)
        finally:
            if saved is None:
                os.environ.pop("NEXUS_SPELL_CANDIDATE_BUDGET", None)
            else:
                os.environ["NEXUS_SPELL_CANDIDATE_BUDGET"] = saved

    def test_normal_query_not_flagged(self):
        u = understand_query("python tutorial", self.storage)
        self.assertFalse(u.spelling_exhausted)
        self.assertEqual(u.corrected_terms, {})


class TestSpellQualityUnchanged(_JunkStorage):
    """The DoS fix must not damage real corrections (audit acceptance)."""

    def test_real_typo_still_corrected(self):
        u = understand_query("pythom tutorial", self.storage)
        self.assertEqual(u.corrected_terms, {"pythom": "python"})

    def test_distance2_typo_still_corrected(self):
        # "pytohn" is 2 edits from "python" (transposition + insertion)
        Indexer(self.storage).add_document(
            "d2", "python snake", title="Python")
        u = understand_query("pytohn", self.storage)
        self.assertEqual(u.corrected_terms, {"pytohn": "python"})

    def test_no_candidate_returns_none(self):
        self.assertIsNone(correct_spelling("zzzzqqqq", {"python", "machine"}))


if __name__ == "__main__":
    unittest.main()
