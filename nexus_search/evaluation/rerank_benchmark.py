"""Rerank benchmark for Phase 5 — "hybrid" vs "hybrid + rerank".

Mirrors evaluation/benchmark.py's structure exactly: same dataset, same
metrics (they are reused, not reimplemented), same refusal to print when
coverage isn't 100%.

Honesty note: with the DEFAULT weights (retrieval signals dominate), rerank
usually agrees with plain hybrid on this small dataset — that's the expected,
correct outcome (the ranker must not degrade retrieval by default). The
value being demonstrated is the FRAMEWORK: measurable A/B numbers once
non-default weights are configured.
"""
import os

# Hash embedder for offline benchmarking, same as evaluation/benchmark.py.
os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from ..core.hybrid_search import SearchMode
from ..ranking.query import understand_query
from ..ranking.ranker import RankingWeights, rerank
from .benchmark import build_search_functions
from .dataset import create_benchmark_dataset
from .metrics import compare_modes, print_comparison


def run_rerank_benchmark(db_path: str = None, weights: RankingWeights = None) -> dict:
    """Run hybrid vs hybrid+rerank on the benchmark dataset and return the
    comparison dict (also pretty-prints it, like benchmark.py)."""
    _, queries = create_benchmark_dataset()
    query_dicts = [
        {"query_id": q.query_id, "text": q.text, "relevant_docs": q.relevant_docs}
        for q in queries
    ]

    fns, hybrid, storage, dedup, vector_store, sync = build_search_functions(db_path)
    weights = weights or RankingWeights()

    try:
        vec_stats = vector_store.get_stats()
        doc_count = storage.document_count()
        if doc_count > 0 and vec_stats.get("count", 0) / doc_count < 1.0:
            print("Refusing to compare - vector coverage below 100%")
            return {"error": "vector_coverage_not_100%"}

        def rerank_fn(query: str, top_k: int) -> list[str]:
            results = hybrid.search(query, top_k=top_k, mode=SearchMode.HYBRID)
            ranked = rerank(results, query, weights, storage=storage,
                            understanding=understand_query(query, storage))
            return [r.doc_id for r in ranked]

        comparison = compare_modes(query_dicts, {
            "hybrid": fns["hybrid"],
            "hybrid+rerank": rerank_fn,
        })
        print_comparison(comparison)
        return comparison
    finally:
        sync.close()
        hybrid.close()
        storage.close()
        dedup.close()
        vector_store.close()


if __name__ == "__main__":
    run_rerank_benchmark()
