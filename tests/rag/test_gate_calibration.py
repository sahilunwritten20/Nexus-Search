"""P7-4 — CI calibration regression: the committed gate floors still meet
the STATED targets on the WP10 fixture (dev AND test splits), and the
harness's deterministic rule still re-derives the frozen numbers. A drift
here means retrieval or gate behavior changed the raw-signal separation —
that is a quality regression, never a silent pass."""
import os
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")
if os.environ.get("NEXUS_EMBEDDER") != "hash:384":
    raise unittest.SkipTest("calibration floors are pinned to hash:384")

from nexus_search.rag import calibration
from nexus_search.rag.gate import load_floors


@unittest.skipIf(os.environ.get("NEXUS_RUN_LLM_TESTS") == "1",
                 "deterministic fixture calibration only (no LLM involved)")
class TestGateCalibrationRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = calibration.run_calibration(quiet=True)

    def test_harness_ran_on_the_hash_embedder(self):
        self.assertEqual(self.report["embedder"], "hash:384")
        self.assertGreaterEqual(self.report["queries_total"], 80)

    def test_frozen_floors_match_the_harness_rule_output(self):
        # deterministic drift detection: the committed floors ARE what the
        # committed rule chooses on the dev split today
        frozen = load_floors()["floors"]["hash:384"]
        self.assertEqual(self.report["chosen_floors"], frozen)

    def test_dev_split_meets_the_stated_targets(self):
        dev = self.report["dev"]
        self.assertGreaterEqual(
            dev["junk_refusal_rate"], calibration.MIN_JUNK_REFUSAL_DEV)
        self.assertLessEqual(
            dev["well_evidenced_false_refusal_rate"],
            calibration.MAX_WELL_EVIDENCED_FALSE_REFUSAL)
        self.assertLessEqual(
            dev["raw_answerable_false_refusal_rate"],
            calibration.MAX_RAW_ANSWERABLE_FALSE_REFUSAL)

    def test_test_split_meets_the_stated_targets(self):
        # the REPORTED numbers: held-out split, both quality bounds hold
        test = self.report["test"]
        self.assertGreaterEqual(test["junk_refusal_rate"],
                                load_floors()["calibration"]["stated_targets"]
                                ["min_junk_refusal_dev"])
        self.assertLessEqual(test["well_evidenced_false_refusal_rate"],
                             load_floors()["calibration"]["stated_targets"]
                             ["max_well_evidenced_false_refusal"])

    def test_test_split_numbers_match_the_committed_report(self):
        # the JSON is the record of the measured held-out results
        committed = load_floors()["calibration"]["test_split_results"]
        test = self.report["test"]
        for key in ("junk_refusal_rate",
                    "well_evidenced_false_refusal_rate",
                    "raw_answerable_false_refusal_rate"):
            self.assertAlmostEqual(test[key], committed[key], places=3,
                                   msg=f"held-out {key} drifted")


if __name__ == "__main__":
    unittest.main()
