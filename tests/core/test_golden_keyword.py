"""WP0 baseline: keyword-mode output must stay byte-identical to the golden
snapshot unless a commit intentionally regenerates it.

The golden file was captured on the pre-remediation code (branch point of
fix/audit-phase1-6) with the default hash embedder environment. Compare is on
rounded scores (6dp), titles, snippets, ordering, and totals.
"""
import json
import os
import tempfile
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from tests.golden.golden_corpus import golden_output  # noqa: E402

_GOLDEN_PATH = os.path.join(os.path.dirname(__file__), "..", "golden",
                            "keyword_baseline.json")


class TestKeywordGoldenBaseline(unittest.TestCase):
    def test_keyword_mode_matches_golden_snapshot(self):
        with open(_GOLDEN_PATH, encoding="utf-8") as f:
            golden = json.load(f)
        with tempfile.TemporaryDirectory() as tmp:
            live = golden_output(os.path.join(tmp, "live.db"))

        self.assertEqual(
            [q["q"] for q in live["queries"]],
            [q["q"] for q in golden["queries"]],
            "golden query list drifted — regenerate the snapshot",
        )
        failures = []
        for live_q, golden_q in zip(live["queries"], golden["queries"]):
            if live_q != golden_q:
                failures.append(live_q["q"])
        self.assertEqual(
            failures, [],
            "keyword-mode output changed vs tests/golden/keyword_baseline.json "
            f"for queries: {failures}. If this change is intentional, regenerate "
            "the snapshot with scripts/dev/regen_golden.py in the same commit.")


if __name__ == "__main__":
    unittest.main()
