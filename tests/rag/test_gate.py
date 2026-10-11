"""P7-4 — RefuseGate math: raw signals only (fused scores never read),
None handling (hybrid agreement rule), no_evidence, committed floors,
conservative default for unknown embedders with a loud warning, and the
calibration-mirror invariant (calibration._gate_refuses == gate.evaluate)."""
import logging
import os
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.rag import calibration
from nexus_search.rag.gate import (
    CONSERVATIVE_DEFAULT, REFUSAL_LOW_CONFIDENCE, REFUSAL_NO_EVIDENCE,
    RefuseGate, floors_for, load_floors,
)
from nexus_search.rag.retrieval import RagChunk


def chunk(cid, bm25=None, vector=None):
    return RagChunk(chunk_id=cid, parent_id=cid, title=cid,
                    bm25_score=bm25, vector_score=vector)


COMMITTED = load_floors()
HASH_FLOORS = COMMITTED["floors"]["hash:384"]
BF = HASH_FLOORS["bm25_floor"]
VF = HASH_FLOORS["vector_floor"]


class TestGateCommittedFloors(unittest.TestCase):
    def test_frozen_file_shape_and_values(self):
        self.assertEqual(COMMITTED["version"], 1)
        self.assertIn("hash:384", COMMITTED["floors"])
        self.assertGreater(BF, 0)
        self.assertGreater(VF, 0)
        self.assertLessEqual(VF, 1.0)
        # the frozen floors must be reproducible by the harness rule
        report = None  # (expensive run is in test_gate_calibration)
        self.assertEqual(BF, 6.5)
        self.assertEqual(VF, 0.45)

    def test_floors_for_known_embedder(self):
        f = floors_for("hash:384")
        self.assertEqual((f.bm25_floor, f.vector_floor), (BF, VF))

    def test_unknown_embedder_gets_conservative_default_and_loud_warning(self):
        logger = logging.getLogger("nexus_search.rag.gate")
        with self.assertLogs(logger, level="WARNING") as captured:
            f = floors_for("st:some-uncalibrated-model")
        self.assertEqual(f.bm25_floor, CONSERVATIVE_DEFAULT["bm25_floor"])
        self.assertEqual(f.vector_floor, CONSERVATIVE_DEFAULT["vector_floor"])
        self.assertTrue(any("NO CALIBRATED FLOORS" in m for m in captured.output))
        # second lookup for the SAME embedder warns once, not per call
        with self.assertRaises(AssertionError):
            with self.assertLogs(logger, level="WARNING"):
                floors_for("st:some-uncalibrated-model")


class TestGateDecisionMath(unittest.TestCase):
    def setUp(self):
        self.gate = RefuseGate()

    def test_no_results_is_no_evidence(self):
        d = self.gate.evaluate([], "hash:384")
        self.assertFalse(d.answerable)
        self.assertEqual(d.refusal_reason, REFUSAL_NO_EVIDENCE)
        self.assertIsNone(d.signals["top_bm25"])

    def test_both_signals_above_floors_answers(self):
        d = self.gate.evaluate([chunk("a", bm25=BF + 1, vector=VF + 0.01)],
                               "hash:384")
        self.assertTrue(d.answerable)
        self.assertIsNone(d.refusal_reason)

    def test_both_below_floors_refuses_low_confidence(self):
        d = self.gate.evaluate([chunk("a", bm25=BF - 1, vector=VF - 0.1)],
                               "hash:384")
        self.assertFalse(d.answerable)
        self.assertEqual(d.refusal_reason, REFUSAL_LOW_CONFIDENCE)
        self.assertEqual(sorted(d.signals["signals_below_floor"]),
                         ["bm25", "vector"])

    def test_one_strong_signal_suffices_when_both_present(self):
        # strong bm25, weak vector: evidence exists -> answer
        self.assertTrue(self.gate.evaluate(
            [chunk("a", bm25=BF + 5, vector=VF - 0.2)], "hash:384").answerable)
        # weak bm25, strong vector -> answer
        self.assertTrue(self.gate.evaluate(
            [chunk("a", bm25=BF - 5, vector=VF + 0.2)], "hash:384").answerable)

    def test_single_retriever_hit_below_floor_refuses_agreement_rule(self):
        # bm25-only hit (vector side never saw it) under the floor -> refuse
        d = self.gate.evaluate([chunk("a", bm25=BF - 1, vector=None)],
                               "hash:384")
        self.assertFalse(d.answerable)
        self.assertEqual(d.refusal_reason, REFUSAL_LOW_CONFIDENCE)

    def test_single_retriever_hit_above_floor_answers(self):
        d = self.gate.evaluate([chunk("a", bm25=BF + 1, vector=None)],
                               "hash:384")
        self.assertTrue(d.answerable)
        self.assertEqual(d.signals["signals_present"], ["bm25"])

    def test_vector_only_hit_below_floor_refuses(self):
        d = self.gate.evaluate([chunk("a", bm25=None, vector=VF - 0.05)],
                               "hash:384")
        self.assertFalse(d.answerable)

    def test_vector_only_hit_above_floor_answers(self):
        self.assertTrue(self.gate.evaluate(
            [chunk("a", bm25=None, vector=VF + 0.05)], "hash:384").answerable)

    def test_both_signals_none_is_no_evidence(self):
        # defensive: a hit neither retriever scored is not evidence
        d = self.gate.evaluate([chunk("a", bm25=None, vector=None)],
                               "hash:384")
        self.assertFalse(d.answerable)
        self.assertEqual(d.refusal_reason, REFUSAL_NO_EVIDENCE)

    def test_decision_uses_top_hit_only(self):
        rows = [chunk("weak", bm25=0.1, vector=0.01),
                chunk("strong", bm25=BF + 10, vector=VF + 0.3)]
        d = self.gate.evaluate(rows, "hash:384")
        self.assertFalse(d.answerable)  # the TOP hit's signals decide
        self.assertEqual(d.signals["top_chunk_id"], "weak")

    def test_fused_score_is_never_an_input(self):
        # identical raw signals must produce identical decisions whatever
        # the fused/normalized artifacts look like
        import dataclasses
        a = chunk("a", bm25=BF - 1, vector=VF - 0.1)
        b = dataclasses.replace(a, fused_score=999.0, source="bm25+vector")
        self.assertEqual(self.gate.evaluate([a], "hash:384").answerable,
                         self.gate.evaluate([b], "hash:384").answerable)
        self.assertFalse(self.gate.evaluate([b], "hash:384").answerable)

    def test_signals_recorded_for_metadata(self):
        d = self.gate.evaluate([chunk("a", bm25=12.5, vector=0.31)],
                               "hash:384")
        self.assertEqual(d.signals["top_bm25"], 12.5)
        self.assertEqual(d.signals["top_vector"], 0.31)
        self.assertEqual(d.signals["bm25_floor"], BF)
        self.assertEqual(d.signals["vector_floor"], VF)
        self.assertEqual(d.signals["embedder"], "hash:384")


class TestCalibrationMirror(unittest.TestCase):
    """calibration._gate_refuses must mirror RefuseGate.evaluate exactly —
    the floors were calibrated with that math, so drift here invalidates
    the frozen numbers."""

    def test_mirror_on_all_signal_shapes(self):
        rows = [
            calibration.SignalRow("r1", "t", False, None, None, None, 0),
            calibration.SignalRow("r2", "t", True, BF - 1, VF - 0.1, "x", 5,
                                  well_evidenced=True),
            calibration.SignalRow("r3", "t", True, BF + 1, None, "x", 5),
            calibration.SignalRow("r4", "t", True, None, VF + 0.1, "x", 5),
            calibration.SignalRow("r5", "t", False, BF - 1, VF + 0.1, "x", 3),
            calibration.SignalRow("r6", "t", False, None, None, "x", 2),
        ]
        for row in rows:
            chunks = [] if row.n_results == 0 else [
                chunk(row.top_chunk_id or "x", bm25=row.top_bm25,
                      vector=row.top_vector)]
            gate_refused = not RefuseGate().evaluate(
                chunks, "hash:384").answerable
            mirror_refused = calibration._gate_refuses(row, BF, VF)
            self.assertEqual(gate_refused, mirror_refused,
                             f"mirror drift on {row.query_id}")


if __name__ == "__main__":
    unittest.main()
