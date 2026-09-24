"""Evaluation metrics for Nexus Search Phase 4."""
import math
from typing import Optional, Union


def to_doc_ids(retrieved: list[Union[str, object]]) -> list[str]:
    """Accept plain doc_id strings OR ranked/hybrid result objects (anything
    with a .doc_id, e.g. RankedResult), returning plain doc_ids. This keeps
    every metric usable for both retrieval results and Stage-5 reranked
    results without changing any metric's math."""
    out = []
    for item in retrieved:
        out.append(item if isinstance(item, str) else getattr(item, "doc_id"))
    return out


def precision_at_k(relevant: set[str], retrieved: list[str], k: int) -> float:
    if k <= 0:
        return 0.0
    retrieved_k = retrieved[:k]
    if not retrieved_k:
        return 0.0
    hits = sum(1 for doc_id in retrieved_k if doc_id in relevant)
    return hits / k


def recall_at_k(relevant: set[str], retrieved: list[str], k: int) -> float:
    if not relevant:
        return 1.0
    retrieved_k = retrieved[:k]
    hits = sum(1 for doc_id in retrieved_k if doc_id in relevant)
    return hits / len(relevant)


def mrr(relevant: set[str], retrieved: list[str]) -> float:
    for rank, doc_id in enumerate(retrieved, 1):
        if doc_id in relevant:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(relevant: dict[str, int], retrieved: list[str], k: int) -> float:
    """NDCG with graded relevance (0-3)."""
    if k <= 0:
        return 0.0

    def dcg(items: list[str]) -> float:
        score = 0.0
        for i, doc_id in enumerate(items):
            rel = relevant.get(doc_id, 0)
            if rel > 0:
                score += (2 ** rel - 1) / math.log2(i + 2)
        return score

    retrieved_k = retrieved[:k]
    actual_dcg = dcg(retrieved_k)

    ideal_items = sorted(relevant.keys(), key=lambda d: relevant[d], reverse=True)
    ideal_dcg = dcg(ideal_items[:k])

    if ideal_dcg == 0:
        return 1.0
    return actual_dcg / ideal_dcg


def evaluate_query(
    query_relevant: dict[str, int],
    retrieved: list,
    k_values: list[int] = None
) -> dict:
    if k_values is None:
        k_values = [1, 3, 5, 10, 20]

    retrieved = to_doc_ids(retrieved)
    relevant_set = {doc_id for doc_id, rel in query_relevant.items() if rel > 0}

    results = {}
    for k in k_values:
        results[f"precision@{k}"] = precision_at_k(relevant_set, retrieved, k)
        results[f"recall@{k}"] = recall_at_k(relevant_set, retrieved, k)
        results[f"ndcg@{k}"] = ndcg_at_k(query_relevant, retrieved, k)

    results["mrr"] = mrr(relevant_set, retrieved)
    return results


def evaluate_all(
    queries: list[dict],
    search_fn,
    k_values: list[int] = None
) -> dict:
    """Run evaluation across all queries.

    queries: list of {"query_id", "text", "relevant_docs": {doc_id: relevance}}
    search_fn: function(query_text, top_k) -> list of doc_ids
    """
    if k_values is None:
        k_values = [1, 3, 5, 10, 20]

    all_results = {}
    latencies = []

    for q in queries:
        import time
        start = time.time()
        retrieved = search_fn(q["text"], top_k=max(k_values))
        latencies.append(time.time() - start)

        metrics = evaluate_query(q["relevant_docs"], retrieved, k_values)
        all_results[q["query_id"]] = metrics

    # Aggregate
    agg = {}
    for k in k_values:
        agg[f"precision@{k}"] = sum(r[f"precision@{k}"] for r in all_results.values()) / len(all_results)
        agg[f"recall@{k}"] = sum(r[f"recall@{k}"] for r in all_results.values()) / len(all_results)
        agg[f"ndcg@{k}"] = sum(r[f"ndcg@{k}"] for r in all_results.values()) / len(all_results)

    agg["mrr"] = sum(r["mrr"] for r in all_results.values()) / len(all_results)
    agg["latency_mean"] = sum(latencies) / len(latencies)
    agg["latency_p50"] = sorted(latencies)[len(latencies) // 2]
    agg["latency_p95"] = sorted(latencies)[int(len(latencies) * 0.95)]

    return {
        "per_query": all_results,
        "aggregate": agg,
    }


def compare_modes(
    queries: list[dict],
    search_fns: dict[str, callable],
    k_values: list[int] = None
) -> dict:
    """Compare multiple search modes."""
    if k_values is None:
        k_values = [1, 3, 5, 10, 20]

    results = {}
    for mode_name, search_fn in search_fns.items():
        results[mode_name] = evaluate_all(queries, search_fn, k_values)

    return results


def print_comparison(comparison: dict):
    """Pretty print comparison results."""
    print("\n" + "=" * 80)
    print("SEARCH MODE COMPARISON")
    print("=" * 80)

    modes = list(comparison.keys())
    metrics = ["precision@1", "precision@5", "precision@10", "recall@10", "mrr", "ndcg@10", "latency_mean"]

    header = f"{'Metric':<20}" + "".join(f"{m:>15}" for m in modes)
    print(header)
    print("-" * len(header))

    for metric in metrics:
        row = f"{metric:<20}"
        for mode in modes:
            val = comparison[mode]["aggregate"].get(metric, 0)
            row += f"{val:>15.4f}"
        print(row)

    print("=" * 80)