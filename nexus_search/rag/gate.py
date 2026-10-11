"""P7-4 â€” the refuse-gate: decide answerability BEFORE any LLM spend.

CONTRACT (PHASE7_PLAN Â§6, WP12-B2):
- inputs are RAW retrieval signals ONLY: the un-normalized top-hit
  `bm25_score` and the raw top-hit `vector_score` (cosine). Fused RRF /
  weighted scores are rank/normalization artifacts and are NEVER read
  here (their values are identical for a 0.92-cosine and a 0.05-cosine
  top hit â€” Phase 4 measured confident junk at BM25 top-1 5.4-9.1 on
  unanswerable queries, which is exactly what these floors cut).
- None handling: a signal that is None means that retriever did not find
  the row; the decision then rests on the signals that ARE present
  (the "hybrid agreement" rule: a top hit that came from only ONE
  retriever is refused when that retriever's raw score is under its
  floor â€” a special case of "refuse iff every present signal is below
  its floor").
- thresholds are CALIBRATED on the WP10 fixture (rag/calibration.py),
  frozen per EMBEDDER IDENTITY in rag/gate_floors.json (hash vs ST
  distributions differ), and enforced as CI regression floors.
- an UNKNOWN embedder gets a conservative default (floors HIGH â†’ refuse
  more) plus a LOUD warning â€” never a silent pass-through.

HONEST LIMIT: the gate cannot detect a corpus that confidently contains
WRONG information â€” a high-scoring lie is still a high-scoring hit.
"""
import json
import logging
import os
import threading
from dataclasses import dataclass
from typing import Optional

from .retrieval import RagChunk

logger = logging.getLogger("nexus_search.rag.gate")

FLOORS_PATH = os.path.join(os.path.dirname(__file__), "gate_floors.json")

# Unknown-embedder fallback: deliberately HIGH so the gate fails CLOSED
# (refuses) rather than answering from uncalibrated signals.
CONSERVATIVE_DEFAULT = {"bm25_floor": 10.0, "vector_floor": 0.55}

REFUSAL_NO_EVIDENCE = "no_evidence"
REFUSAL_LOW_CONFIDENCE = "low_confidence"
REFUSAL_RETRIEVAL_UNAVAILABLE = "retrieval_unavailable"


@dataclass(frozen=True)
class GateFloors:
    embedder: str
    bm25_floor: float
    vector_floor: float


def load_floors() -> dict:
    """The committed floors file (the ONLY source of production floors)."""
    with open(FLOORS_PATH, encoding="utf-8") as fh:
        return json.load(fh)


_warned_unknown: set[str] = set()
_warn_lock = threading.Lock()


def floors_for(embedder: str, floors_doc: Optional[dict] = None) -> GateFloors:
    doc = floors_doc if floors_doc is not None else load_floors()
    entry = (doc.get("floors") or {}).get(embedder)
    if entry is None:
        with _warn_lock:  # warn once per embedder per process, LOUDLY
            if embedder not in _warned_unknown:
                _warned_unknown.add(embedder)
                logger.warning(
                    "RAG refuse-gate: NO CALIBRATED FLOORS for embedder "
                    "%r â€” using the conservative default (refuses more). "
                    "Run rag/calibration.py to calibrate this embedder.",
                    embedder)
        return GateFloors(embedder=embedder or "unknown",
                          **CONSERVATIVE_DEFAULT)
    return GateFloors(embedder=embedder,
                      bm25_floor=float(entry["bm25_floor"]),
                      vector_floor=float(entry["vector_floor"]))


@dataclass
class GateDecision:
    answerable: bool
    refusal_reason: Optional[str] = None
    # the raw evidence (for response metadata): top-hit raw scores, the
    # floors they were measured against, and the top hit's id
    signals: dict = None

    def __post_init__(self):
        if self.signals is None:
            self.signals = {}


class RefuseGate:
    """Stateless evaluator; floors are per-embedder and committed."""

    def __init__(self, floors_doc: Optional[dict] = None):
        self._floors_doc = floors_doc

    def evaluate(self, results: list[RagChunk],
                 embedder: Optional[str]) -> GateDecision:
        floors = floors_for(embedder or "unknown", self._floors_doc)
        if not results:
            return GateDecision(
                False, REFUSAL_NO_EVIDENCE,
                {"embedder": embedder, "bm25_floor": floors.bm25_floor,
                 "vector_floor": floors.vector_floor, "top_bm25": None,
                 "top_vector": None, "top_chunk_id": None})

        top = results[0]  # the post-rerank top hit (rank order)
        present: dict[str, float] = {}
        if top.bm25_score is not None:
            present["bm25"] = float(top.bm25_score)
        if top.vector_score is not None:
            present["vector"] = float(top.vector_score)
        if not present:
            # a row neither retriever scored cannot be evidence
            return GateDecision(
                False, REFUSAL_NO_EVIDENCE,
                {"embedder": embedder, "bm25_floor": floors.bm25_floor,
                 "vector_floor": floors.vector_floor, "top_bm25": None,
                 "top_vector": None, "top_chunk_id": top.chunk_id})

        below = {name for name, value in present.items()
                 if (value < floors.bm25_floor if name == "bm25"
                     else value < floors.vector_floor)}
        answerable = len(below) < len(present)  # at least one signal at/above floor
        return GateDecision(
            answerable,
            None if answerable else REFUSAL_LOW_CONFIDENCE,
            {"embedder": embedder, "top_chunk_id": top.chunk_id,
             "top_bm25": present.get("bm25"),
             "top_vector": present.get("vector"),
             "bm25_floor": floors.bm25_floor,
             "vector_floor": floors.vector_floor,
             "signals_present": sorted(present),
             "signals_below_floor": sorted(below)})


def evaluate_gate(results: list[RagChunk], embedder: Optional[str],
                  floors_doc: Optional[dict] = None) -> GateDecision:
    """Functional form (the service and the calibration harness use this)."""
    return RefuseGate(floors_doc).evaluate(results, embedder)

