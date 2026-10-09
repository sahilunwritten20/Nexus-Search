"""WP8: scale benchmark for the audit's 'potential performance' items at
1K / 5K / 10K synthetic docs, hash embedder. Reports measured cost per item;
fixes only what the numbers prove is worse than linear. Everything here is
prototype-scale-acknowledged (SPEC.md) — this benchmark keeps the
acknowledgment honest.

WP13-2: TWO scenarios per size (the reviewer's point: the original corpus
put "python search" in EVERY document, so every query matched 100% of the
corpus and latency grew linearly BY CONSTRUCTION — a worst case, not a
representative one):
  - match_all      — the original worst case: every document contains
                     the query terms (labelled as such in every row);
  - selective      — a Zipfian-topic corpus (50 topics, s=1.2, plus a
                     500-word secondary vocabulary) where the query
                     terms occur in ~1-5% of documents.

Usage: python -m nexus_search.evaluation.scale_benchmark
"""
import json
import os
import random
import time

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from ..core.hybrid_search import HybridSearch, SearchMode  # noqa: E402
from ..core.indexer import Indexer  # noqa: E402
from ..core.storage import Storage  # noqa: E402

_ZIPF_TOPICS = 50
_ZIPF_EXP = 1.2
_SECONDARY_WORDS = [f"w{i:03d}" for i in range(500)]
# the second query term: a mid/low-frequency secondary word (each doc
# carries 3 of the 500 secondaries -> ~0.6% expected share). The query's
# MATCH set (either term) lands at ~3% of the corpus — the representative
# selective shape, not the 100% worst case.
_QUERY_SECONDARY = "w123"


def _zipf_ranks(n_topics: int, s: float) -> list:
    """Deterministic Zipf probabilities over topic ranks 1..n_topics."""
    weights = [1.0 / (r ** s) for r in range(1, n_topics + 1)]
    total = sum(weights)
    return [w / total for w in weights]


def _build_corpus(db_path: str, n: int) -> HybridSearch:
    """The original WP8 corpus: EVERY document contains the query terms —
    the latency worst case (every doc matches)."""
    storage = Storage(db_path)
    indexer = Indexer(storage)
    from ..core.vector_store import VectorStoreManager
    from ..core.embedding_sync import EmbeddingSync
    vs = VectorStoreManager(db_path)
    sync = EmbeddingSync(vs, batch_size=64)
    sync.attach(indexer)
    for i in range(n):
        indexer.add_document(
            f"d{i:06d}",
            f"document {i} about python search engines and ranking signals "
            f"with filler text number {i % 97} and shared vocabulary",
            title=f"Document {i:06d}")
    sync.flush()
    hybrid = HybridSearch(storage, vector_store=vs, db_path=db_path)
    hybrid._bench_sync = sync       # keep refs for cleanup
    hybrid._bench_vs = vs
    hybrid._bench_storage = storage
    return hybrid


def _build_selective_corpus(db_path: str, n: int):
    """Zipfian mixed-vocabulary corpus where the query terms occur in
    ~1-5% of documents. Deterministic (seeded) so runs are comparable.
    Returns (hybrid, query, match_fraction)."""
    storage = Storage(db_path)
    indexer = Indexer(storage)
    from ..core.vector_store import VectorStoreManager
    from ..core.embedding_sync import EmbeddingSync
    vs = VectorStoreManager(db_path)
    sync = EmbeddingSync(vs, batch_size=64)
    sync.attach(indexer)

    rng = random.Random(20241009)
    probs = _zipf_ranks(_ZIPF_TOPICS, _ZIPF_EXP)
    topics = [f"topic{k:02d}" for k in range(_ZIPF_TOPICS)]
    topic_counts = {t: 0 for t in topics}
    secondary_counts = {w: 0 for w in _SECONDARY_WORDS}
    topic_x_secondary = {t: 0 for t in topics}
    for i in range(n):
        topic = rng.choices(topics, weights=probs, k=1)[0]
        topic_counts[topic] += 1
        secondaries = rng.sample(_SECONDARY_WORDS, k=3)
        for w in secondaries:
            secondary_counts[w] += 1
        if _QUERY_SECONDARY in secondaries:
            topic_x_secondary[topic] += 1
        indexer.add_document(
            f"d{i:06d}",
            f"document {i} about {topic} architecture and "
            f"{secondaries[0]} integration with {secondaries[1]} tuning "
            f"and {secondaries[2]} observability across the pipeline",
            title=f"Document {i:06d}")
    sync.flush()

    # pick the topic whose ACTUAL share of the corpus is closest to 2.5%
    target = 0.025
    topic, count = min(topic_counts.items(),
                       key=lambda kv: abs(kv[1] / n - target))
    # the query's match set: docs containing the topic OR the secondary
    union = count + secondary_counts[_QUERY_SECONDARY] - topic_x_secondary[topic]
    query = f"{topic} {_QUERY_SECONDARY}"
    hybrid = HybridSearch(storage, vector_store=vs, db_path=db_path)
    hybrid._bench_sync = sync
    hybrid._bench_vs = vs
    hybrid._bench_storage = storage
    return hybrid, query, union / n


def _timeit(fn, repeat=3):
    fn()  # warm
    best = float("inf")
    for _ in range(repeat):
        t = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t)
    return best * 1000


def _measure(hybrid, query: str) -> dict:
    page = hybrid.search_page(query, top_k=10, mode=SearchMode.HYBRID)
    assert page.results, f"scenario query returned nothing: {query!r}"
    return {
        "hybrid_ms": round(_timeit(lambda: hybrid.search_page(
            query, top_k=10, mode=SearchMode.HYBRID)), 1),
        "keyword_ms": round(_timeit(lambda: hybrid.search_page(
            query, top_k=10, mode=SearchMode.KEYWORD)), 1),
        "semantic_ms": round(_timeit(lambda: hybrid.search_page(
            query, top_k=10, mode=SearchMode.SEMANTIC)), 1),
    }


def _close(hybrid):
    hybrid._bench_sync.close()
    hybrid.close()
    hybrid._bench_vs.close()
    hybrid._bench_storage.close()


def run_scale_benchmark(sizes=(1_000, 5_000, 10_000)) -> dict:
    import tempfile
    import shutil
    results = {}
    for n in sizes:
        for scenario in ("match_all", "selective"):
            tmp = tempfile.mkdtemp()
            db = os.path.join(tmp, "scale.db")
            t0 = time.perf_counter()
            if scenario == "match_all":
                hybrid = _build_corpus(db, n)
                query, frac = "python search", 1.0
            else:
                hybrid, query, frac = _build_selective_corpus(db, n)
            build_s = time.perf_counter() - t0
            if not (0.01 <= frac <= 0.05) and scenario == "selective":
                _close(hybrid)
                shutil.rmtree(tmp, ignore_errors=True)
                raise AssertionError(
                    f"selective query matched {frac:.1%} of docs; "
                    f"scenario must stay in the 1-5% band")
            row = {
                "scenario": scenario,
                "docs": n,
                "query": query,
                "match_fraction": round(frac, 4),
                "build_seconds": round(build_s, 1),
            }
            row.update(_measure(hybrid, query))
            results[f"{scenario}:{n}"] = row
            print(json.dumps(row))
            _close(hybrid)
            shutil.rmtree(tmp, ignore_errors=True)
    return results


def vs_count(hybrid) -> int:
    return hybrid.vector_store.get_stats().get("count", 0)


def main() -> int:
    print("=== scale benchmark (hash embedder, Windows/Python 3.14) ===")
    print("NOTE: 'match_all' is the WORST CASE — every document contains "
          "the query terms; 'selective' is the representative case "
          "(~1-5% of docs match).")
    results = run_scale_benchmark()
    # honesty check: search latency should be roughly linear (or better) in
    # corpus size; super-linear growth would be a confirmed fix-now finding
    for scenario in ("match_all", "selective"):
        if f"{scenario}:1000" in results and f"{scenario}:10000" in results:
            a = results[f"{scenario}:1000"]["hybrid_ms"]
            b = results[f"{scenario}:10000"]["hybrid_ms"]
            print(f"{scenario} hybrid latency growth 1K->10K: "
                  f"x{b / max(a, 0.1):.1f} (linear would be ~10x)")
    print("NOTE: SQLite-scale limits are acknowledged by SPEC.md; these "
          "numbers document the cost curve, they do not gate CI.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
