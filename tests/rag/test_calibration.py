"""Tests for the refuse-gate calibration module (P7-4)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from nexus_search.rag.calibration import (
    _split,
    _gate_refuses,
    evaluate_floors,
    choose_floors,
    run_calibration,
    SignalRow,
)


class TestCalibrationSplit(unittest.TestCase):
    """Test the 60/40 stratified split logic."""

    def test_split_preserves_answerability_ratio(self):
        """The split should preserve the answerable/unanswerable ratio."""
        rows = [
            SignalRow("q1", "a", True, 1.0, 0.5, "c1", 5, True, "exact"),
            SignalRow("q2", "b", True, 0.5, 0.3, "c2", 3, False, "ident"),
            SignalRow("q3", "c", False, 0.2, 0.1, "c3", 1, False, "no-ans"),
            SignalRow("q4", "d", False, 0.1, 0.9, "c4", 2, False, "no-ans"),
        ]
        dev, test = _split(rows, seed=42)
        # 60/40 split: 2 answerable -> 1 dev, 1 test; 2 unanswerable -> 1 dev, 1 test
        dev_ans = sum(1 for r in dev if r.answerable)
        test_ans = sum(1 for r in test if r.answerable)
        self.assertEqual(dev_ans, 1)
        self.assertEqual(test_ans, 1)

    def test_split_deterministic(self):
        """Same seed should produce identical splits."""
        rows = [
            SignalRow(f"q{i}", "x", i % 2 == 0, 0.5, 0.5, "c", 1, False, "cat")
            for i in range(20)
        ]
        dev1, test1 = _split(rows, seed=42)
        dev2, test2 = _split(rows, seed=42)
        self.assertEqual([r.query_id for r in dev1], [r.query_id for r in dev2])
        self.assertEqual([r.query_id for r in test1], [r.query_id for r in test2])

    def test_split_respects_60_40_ratio(self):
        """Split should be approximately 60/40."""
        rows = [SignalRow(f"q{i}", "x", i % 2 == 0, 0.5, 0.5, "c", 1, False, "cat") for i in range(100)]
        dev, test = _split(rows, seed=42)
        self.assertEqual(len(dev), 60)
        self.assertEqual(len(test), 40)


class TestGateRefuses(unittest.TestCase):
    """Test the gate refusal logic."""

    def test_no_results_refuses(self):
        """No results should trigger refusal."""
        row = SignalRow("q1", "no results", True, None, None, "c1", 0, False, "")
        self.assertTrue(_gate_refuses(row, 6.5, 0.45))

    def test_bm25_below_floor_allows(self):
        """BM25 below floor, vector above -> allows (not all weak)."""
        row = SignalRow("q1", "low score", True, 5.0, 0.9, "c1", 1, True, "exact")
        self.assertFalse(_gate_refuses(row, 6.5, 0.45))

    def test_vector_below_floor_allows(self):
        """Vector below floor, BM25 above -> allows (not all weak)."""
        row = SignalRow("q1", "low vec", True, 10.0, 0.3, "c1", 1, True, "exact")
        self.assertFalse(_gate_refuses(row, 6.5, 0.45))

    def test_above_floors_allows(self):
        """Scores above both floors should allow."""
        row = SignalRow("q1", "high scores", True, 10.0, 0.9, "c1", 5, True, "exact")
        self.assertFalse(_gate_refuses(row, 6.5, 0.45))

    def test_bm25_above_vector_below_allows(self):
        # BM25 above floor, vector below -> allows (not all weak)
        row = SignalRow("q1", "mixed", True, 10.0, 0.3, "c1", 5, True, "exact")
        self.assertFalse(_gate_refuses(row, 6.5, 0.45))

    def test_bm25_below_vector_above_allows(self):
        # BM25 below floor, vector above -> allowed (at least one above).
        row = SignalRow("q1", "mixed", True, 5.0, 0.9, "c1", 1, True, "exact")
        self.assertFalse(_gate_refuses(row, 6.5, 0.45))

    def test_no_evidence_refuses(self):
        """No results should refuse."""
        row = SignalRow("q1", "none", True, None, None, "c1", 0, False, "")
        self.assertTrue(_gate_refuses(row, 6.5, 0.45))

    def test_missing_bm25_vector_above_allows(self):
        """Missing BM25, vector above floor -> allows."""
        row = SignalRow("q1", "vec only", True, None, 0.9, "c1", 5, True, "exact")
        self.assertFalse(_gate_refuses(row, 6.5, 0.45))


class TestEvaluateFloors(unittest.TestCase):
    """Test floor evaluation metrics."""

    def test_junk_refusal_rate(self):
        """Junk (unanswerable) queries should be refused at expected rate."""
        rows = [
            SignalRow("q1", "junk1", False, 5.0, 0.2, "c1", 1, False, "no-ans"),
            SignalRow("q2", "junk2", False, 3.0, 0.1, "c2", 1, False, "no-ans"),
            SignalRow("q3", "junk3", False, 8.0, 0.9, "c3", 1, True, "no-ans"),  # above both
        ]
        rates = evaluate_floors(rows, bm25_floor=6.5, vector_floor=0.45)
        # 2/3 junk refused (first two), 1/3 allowed (third above both)
        self.assertAlmostEqual(rates["junk_refusal_rate"], 2/3, places=2)

    def test_well_evidenced_false_refusal(self):
        """Well-evidenced answerable queries refused."""
        rows = [
            SignalRow("q1", "ans1", True, 5.0, 0.2, "c1", 1, True, "exact"),
            SignalRow("q2", "ans2", True, 8.0, 0.9, "c2", 1, True, "exact"),
        ]
        rates = evaluate_floors(rows, bm25_floor=6.5, vector_floor=0.45)
        # First is well-evidenced (top_bm25=5.0 < 6.5? No, 5.0 < 6.5 -> refused)
        # Wait: 5.0 < 6.5 -> below floor -> refused
        # Second: 8.0 > 6.5 -> above floor -> allowed
        # So 1/2 well-evidenced false refusal
        self.assertAlmostEqual(rates["well_evidenced_false_refusal_rate"], 0.5, places=2)

    def test_raw_answerable_false_refusal(self):
        """Raw answerable false refusal rate (includes non-well-evidenced)."""
        rows = [
            SignalRow("q1", "ans1", True, 5.0, 0.2, "c1", 1, False, "exact"),  # not well-evidenced
            SignalRow("q2", "ans2", True, 10.0, 0.9, "c2", 1, True, "exact"),  # well
        ]
        rates = evaluate_floors(rows, bm25_floor=6.5, vector_floor=0.45)
        # First is below bm25 floor (5.0 < 6.5) -> refused
        # Second above both -> allowed
        # 1/2 answerable refused
        self.assertAlmostEqual(rates["raw_answerable_false_refusal_rate"], 0.5, places=2)


class TestChooseFloors(unittest.TestCase):
    """Test floor selection logic."""

    def test_choose_floors_returns_best(self):
        """Should select floors meeting all three criteria."""
        # Create synthetic dev rows where we know the outcomes
        rows = [
            # Junk queries - we want high refusal
            SignalRow("jq1", "junk1", False, 1.0, 0.1, "c1", 1, False, "junk"),
            SignalRow("jq2", "junk2", False, 2.0, 0.1, "c2", 1, False, "junk"),
            SignalRow("jq3", "junk3", False, 8.0, 0.9, "c3", 1, True, "junk"),  # above both
            # Answerable well-evidenced
            SignalRow("aq1", "ans1", True, 8.0, 0.9, "c1", 1, True, "exact"),
            SignalRow("aq2", "ans2", True, 10.0, 0.9, "c2", 1, True, "exact"),
            # Answerable but not well-evidenced
            SignalRow("aq3", "ans3", True, 3.0, 0.2, "c3", 1, False, "weak"),
        ]
        result = choose_floors(rows)
        self.assertIsNotNone(result)
        self.assertIn("bm25_floor", result)
        self.assertIn("vector_floor", result)
        # Should meet all three criteria
        rates = evaluate_floors(
            [r for r in rows if not r.answerable],  # junk
            result["bm25_floor"], result["vector_floor"])
        self.assertGreaterEqual(rates["junk_refusal_rate"], 2.0/3.0 - 0.01)
        rates2 = evaluate_floors(
            [r for r in rows if r.answerable and r.well_evidenced],
            result["bm25_floor"], result["vector_floor"])
        self.assertLessEqual(rates["well_evidenced_false_refusal_rate"], 0.06 + 0.01)
        rates3 = evaluate_floors(
            [r for r in rows if r.answerable], result["bm25_floor"], result["vector_floor"])
        self.assertLessEqual(rates["raw_answerable_false_refusal_rate"], 0.12 + 0.01)


class TestRunCalibration(unittest.TestCase):
    """Integration test for the full calibration pipeline."""

    def test_run_calibration_returns_report(self):
        """Full calibration should produce a valid report."""
        report = run_calibration(quiet=True)
        self.assertIn("chosen_floors", report)
        self.assertIn("dev", report)
        self.assertIn("test", report)
        self.assertIn("chosen_floors", report)
        self.assertIsNotNone(report["chosen_floors"])
        self.assertIn("bm25_floor", report["chosen_floors"])
        self.assertIn("vector_floor", report["chosen_floors"])
        self.assertIn("dev", report)
        self.assertIn("test", report)
        self.assertIn("distributions", report)


class TestDistributions(unittest.TestCase):
    """Test the statistics computation."""

    def test_distributions_computed(self):
        from nexus_search.rag.calibration import _distributions
        rows = [
            SignalRow("q1", "a", True, 10.0, 0.9, "c1", 1, True, "exact"),
            SignalRow("q2", "b", True, 5.0, 0.5, "c2", 1, False, "exact"),
            SignalRow("q3", "c", False, 2.0, 0.1, "c3", 1, False, "junk"),
        ]
        dist = _distributions(rows)
        self.assertIn("answerable", _distributions([SignalRow("q", "x", True, 1.0, 0.5, "c", 1, True, "ex")]))
        self.assertIn("unanswerable", _distributions([SignalRow("q", "x", False, 1.0, 0.5, "c", 1, False, "junk")]))


if __name__ == "__main__":
    unittest.main()