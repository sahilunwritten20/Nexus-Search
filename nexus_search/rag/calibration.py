"""P7-4 — refuse-gate floor calibration on the WP10 fixture.

PROTOCOL (WP10 style, committed):
- Corpus: the frozen `evaluation/semantic_benchmark/fixture.py` docs
  (construction ground truth). The hash embedder keeps everything
  deterministic and CI-runnable offline.
- Answerable queries: the fixture's labeled queries (exact / ident /
  synonym / paraphrase / multi / typo) — relevant docs exist by
  construction. "Well-evidenced" answerables are those whose TOP-1 hit is
  one of the fixture's graded-relevant docs: refusing one of those throws
  away good evidence, which is the false refusal that harms; refusing an
  answerable whose top hit is a WRONG doc is the gate honestly reporting
  weak evidence (both rates are reported).
- Unanswerable queries: the fixture's no-answer category PLUS this
  harness's committed extras (EXTRA_UNANSWERABLE) — English questions
  about topics absent from the corpus, labeled unanswerable by
  construction (the WP10 set is only 5; 15 give the junk distribution
  enough mass to calibrate against).
- 60/40 dev/test split, stratified by answerability, by the committed
  SPLIT_SEED. Floors are CHOSEN ON DEV ONLY; RESULTS ARE REPORTED ON THE
  HELD-OUT TEST SPLIT.
- Signals are collected through the DEPLOYED retrieval path
  (rag.retrieval.RagRetriever: hybrid, understanding on, rerank on with
  default weights). Fused scores are never inputs (WP12-B2).

STATED TARGET (restated from the mission default — measured first, see
the Pareto frontier in docs/PHASE7_WORKLOG.md): on the dev split,
- unanswerable refusal >= 2/3,
- well-evidenced answerable false-refusal <= 6%,
- raw answerable false-refusal <= 12%,
then among floor pairs meeting all three: max junk refusal, min
well-evidenced false-refusal, max vector floor (drift-safer against
junk), min bm25 floor — deterministic tie-break, so the committed
gate_floors.json is exactly re-derivable in CI.

WHY NOT THE MISSION DEFAULT (>= 95% junk refusal at <= 10% raw answerable
false-refusal)? MEASURED, not assumed: on hash:384 the junk and
answerable distributions overlap on BOTH raw axes simultaneously — the
lexical embedder cannot distinguish weak-relevant from junk evidence.
Reaching 100% junk refusal costs 15.6% (dev) / 41.2% (test)
well-evidenced false refusals; 88.9% junk costs 12.5% / 35.3%. The
committed frontier is in the worklog. A refusal is first-class (reason
code + best sources attached + zero LLM spend), while a junk pass-through
is a confident wrong answer — the floors below bias toward refusing.

HONEST LIMITS:
- English-only calibration; non-English quality is unmeasured;
- the gate cannot detect a confidently-wrong corpus;
- hash:384 floors do not transfer to st:* embedders (floors are stored
  PER EMBEDDER IDENTITY; an embedder without an entry gets the
  conservative default + a loud warning);
- bm25 idf is corpus-dependent — recalibrate after major corpus changes.
"""
import json
import random
import shutil
import tempfile
import time
from dataclasses import dataclass
from typing import Optional

from .retrieval import RagRetriever

# Committed, deterministic unanswerable extras (construction ground truth:
# none of these topics exist anywhere in the fixture corpus).
EXTRA_UNANSWERABLE = [
    "how to change a flat tire on a bicycle",
    "best pasta carbonara recipe with guanciale",
    "what is the tallest mountain in africa",
    "how do I get coffee stains out of a wool sweater",
    "winning lottery numbers for saturday night",
    "recommended puppy vaccination schedule",
    "how tall is the eiffel tower in meters",
    "symptoms of vitamin d deficiency in adults",
    "bus timetable from the airport to downtown",
    "who wrote pride and prejudice",
]

# The restated, measured targets (see module docstring).
MIN_JUNK_REFUSAL_DEV = 2.0 / 3.0
MAX_WELL_EVIDENCED_FALSE_REFUSAL = 0.06
MAX_RAW_ANSWERABLE_FALSE_REFUSAL = 0.12

_BM25_GRID = [x * 0.5 for x in range(0, 41)]          # 0.0 .. 20.0
_VECTOR_GRID = [x * 0.05 for x in range(0, 21)]       # 0.0 .. 1.00


@dataclass
class SignalRow:
    query_id: str
    text: str
    answerable: bool
    top_bm25: Optional[float]
    top_vector: Optional[float]
    top_chunk_id: Optional[str]
    n_results: int
    well_evidenced: bool = False   # top-1 hit is a graded-relevant doc
    category: str = ""


def _split(rows: list[SignalRow], seed: int) -> tuple[list, list]:
    """60/40 stratified by answerability, deterministic via `seed`."""
    rng = random.Random(seed)
    by_cat: dict[bool, list[SignalRow]] = {}
    for r in rows:
        by_cat.setdefault(r.answerable, []).append(r)
    dev: list[SignalRow] = []
    test: list[SignalRow] = []
    for cat in sorted(by_cat, key=bool):
        items = sorted(by_cat[cat], key=lambda r: r.query_id)
        rng.shuffle(items)
        cut = max(1, int(len(items) * 0.6))
        dev.extend(items[:cut])
        test.extend(items[cut:])
    return dev, test


def _build_corpus(db_path: str):
    """Indexer + hash vector store + HybridSearch + RagRetriever over the
    frozen fixture corpus — mirrors semantic_benchmark's build."""
    from ..evaluation.semantic_benchmark.fixture import build_fixture
    from ..core.storage import Storage
    from ..core.indexer import Indexer
    from ..core.vector_store import VectorStoreManager
    from ..core.embedding_sync import EmbeddingSync
    from ..core.hybrid_search import HybridSearch
    from ..ranking.ranker import RankingWeights

    docs, queries = build_fixture()
    storage = Storage(db_path)
    indexer = Indexer(storage)
    vs = VectorStoreManager(db_path)  # hash embedder; tests pin hash:384
    sync = EmbeddingSync(vs, batch_size=64)
    sync.attach(indexer)
    for doc in docs:
        indexer.add_document(doc.doc_id, doc.content, title=doc.title,
                             doc_type="text")
    sync.flush()
    hybrid = HybridSearch(storage, vector_store=vs, db_path=db_path)
    retriever = RagRetriever(
        hybrid, storage, use_rerank=True,
        weights_fn=lambda: RankingWeights(),  # calibration runs the
        link_intel=None)                      # DEFAULT signal weights
    return storage, hybrid, vs, sync, retriever, queries


def collect_signals(retriever: RagRetriever, queries) -> list[SignalRow]:
    rows = []
    for q in queries:
        relevant = set(q.relevant or {})
        out = retriever.retrieve(q.text, k=10)
        top = out.results[0] if out.results else None
        rows.append(SignalRow(
            query_id=q.query_id, text=q.text, answerable=bool(q.relevant),
            top_bm25=top.bm25_score if top else None,
            top_vector=top.vector_score if top else None,
            top_chunk_id=top.chunk_id if top else None,
            n_results=len(out.results),
            well_evidenced=bool(top and top.chunk_id in relevant),
            category=q.category))
    for i, text in enumerate(EXTRA_UNANSWERABLE):
        out = retriever.retrieve(text, k=10)
        top = out.results[0] if out.results else None
        rows.append(SignalRow(
            query_id=f"no-answer-extra-{i}", text=text, answerable=False,
            top_bm25=top.bm25_score if top else None,
            top_vector=top.vector_score if top else None,
            top_chunk_id=top.chunk_id if top else None,
            n_results=len(out.results),
            well_evidenced=False, category="no-answer-extra"))
    return rows


def _gate_refuses(row: SignalRow, bm25_floor: float,
                  vector_floor: float) -> bool:
    """The gate's exact decision math on one signal row (mirror of
    RefuseGate.evaluate's rule; the calibration tests keep them in sync)."""
    if row.n_results == 0:
        return True  # no_evidence
    present: dict[str, float] = {}
    if row.top_bm25 is not None:
        present["bm25"] = row.top_bm25
    if row.top_vector is not None:
        present["vector"] = row.top_vector
    if not present:
        return True
    below = {name for name, value in present.items()
             if (value < bm25_floor if name == "bm25"
                 else value < vector_floor)}
    return len(below) == len(present)


def evaluate_floors(rows: list[SignalRow], bm25_floor: float,
                    vector_floor: float) -> dict:
    junk = [r for r in rows if not r.answerable]
    answerable = [r for r in rows if r.answerable]
    well = [r for r in answerable if r.well_evidenced]
    return {
        "junk_refusal_rate":
            (sum(_gate_refuses(r, bm25_floor, vector_floor) for r in junk)
             / len(junk)) if junk else 0.0,
        "well_evidenced_false_refusal_rate":
            (sum(_gate_refuses(r, bm25_floor, vector_floor) for r in well)
             / len(well)) if well else 0.0,
        "raw_answerable_false_refusal_rate":
            (sum(_gate_refuses(r, bm25_floor, vector_floor)
                 for r in answerable) / len(answerable)) if answerable else 0.0,
        "junk_total": len(junk),
        "well_evidenced_total": len(well),
        "answerable_total": len(answerable),
    }


def choose_floors(dev: list[SignalRow]) -> Optional[dict]:
    """Deterministic floor choice on the dev split against the STATED
    targets (see module docstring). None when no grid pair meets them."""
    best_key = None
    best_floors = None
    for bm25_floor in _BM25_GRID:
        for vector_floor in _VECTOR_GRID:
            rates = evaluate_floors(dev, bm25_floor, vector_floor)
            if not (rates["junk_refusal_rate"] >= MIN_JUNK_REFUSAL_DEV
                    and rates["well_evidenced_false_refusal_rate"]
                    <= MAX_WELL_EVIDENCED_FALSE_REFUSAL
                    and rates["raw_answerable_false_refusal_rate"]
                    <= MAX_RAW_ANSWERABLE_FALSE_REFUSAL):
                continue
            key = (round(rates["junk_refusal_rate"], 6),
                   round(-rates["well_evidenced_false_refusal_rate"], 6),
                   round(vector_floor, 6),
                   round(-bm25_floor, 6))
            if best_key is None or key > best_key:
                best_key, best_floors = key, (bm25_floor, vector_floor)
    if best_floors is None:
        return None
    return {"bm25_floor": best_floors[0], "vector_floor": best_floors[1]}


def run_calibration(quiet: bool = False) -> dict:
    """Full harness run: build fixture corpus, collect signals, split,
    choose floors on dev, evaluate on dev + test. Returns the report."""
    from ..evaluation.semantic_benchmark.fixture import SPLIT_SEED
    tmp = tempfile.mkdtemp(prefix="rag-calib-")
    try:
        db_path = _join(tmp, "calib.db")
        storage, hybrid, vs, sync, retriever, queries = _build_corpus(db_path)
        try:
            rows = collect_signals(retriever, queries)
            dev, test = _split(rows, SPLIT_SEED)
            chosen = choose_floors(dev)
            fallback = {"bm25_floor": 0.0, "vector_floor": 0.0}
            report = {
                "embedder": retriever.embedder_name,
                "split_seed": SPLIT_SEED,
                "queries_total": len(rows), "dev": len(dev), "test": len(test),
                "stated_targets": {
                    "min_junk_refusal_dev": round(MIN_JUNK_REFUSAL_DEV, 4),
                    "max_well_evidenced_false_refusal":
                        MAX_WELL_EVIDENCED_FALSE_REFUSAL,
                    "max_raw_answerable_false_refusal":
                        MAX_RAW_ANSWERABLE_FALSE_REFUSAL,
                },
                "chosen_floors": chosen,
                "dev": evaluate_floors(dev, **(chosen or fallback)),
                "test": evaluate_floors(test, **(chosen or fallback)),
                "distributions": _distributions(rows),
            }
            if not quiet:
                _print_report(report)
            return report
        finally:
            sync.close(); hybrid.close(); vs.close(); storage.close()
    finally:
        _rmtree_retry(tmp)


def _distributions(rows: list[SignalRow]) -> dict:
    def _stats(values):
        values = sorted(v for v in values if v is not None)
        if not values:
            return {"n": 0}
        return {
            "n": len(values),
            "min": round(values[0], 3),
            "p25": round(values[len(values) // 4], 3),
            "median": round(values[len(values) // 2], 3),
            "p75": round(values[(len(values) * 3) // 4], 3),
            "max": round(values[-1], 3),
        }
    out = {}
    for group, label in ((True, "answerable"), (False, "unanswerable")):
        subset = [r for r in rows if r.answerable == group]
        out[label] = {
            "top_bm25": _stats([r.top_bm25 for r in subset]),
            "top_vector": _stats([r.top_vector for r in subset]),
            "zero_results": sum(1 for r in subset if r.n_results == 0),
            "well_evidenced": sum(1 for r in subset if r.well_evidenced),
        }
    return out


def _join(*parts):
    import os
    return os.path.join(*parts)


def _rmtree_retry(path: str) -> None:
    for _attempt in range(10):  # Windows: connections close asynchronously
        try:
            shutil.rmtree(path)
            return
        except PermissionError:
            time.sleep(0.2)


def _print_report(report: dict) -> None:
    print(json.dumps(report, indent=2, sort_keys=True))


def main() -> int:
    report = run_calibration()
    if report["chosen_floors"] is None:
        print("\nNO FLOOR PAIR MEETS THE STATED TARGETS on the dev split — "
              "inspect the distributions; never guess floors.")
        return 1
    print("\nFreeze these into nexus_search/rag/gate_floors.json under "
          f"'{report['embedder']}':")
    print(f'  "{report["embedder"]}": '
          f'{json.dumps(report["chosen_floors"])}')
    print("\nHeld-out TEST split results (the reported numbers):")
    print(json.dumps(report["test"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
