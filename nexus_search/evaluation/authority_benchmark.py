"""WP5: authority on/off benchmark — "does Phase 6 genuinely change ranking
when configured, and by how much?" on a graph with known ground truth.

Mission shape (from docs/PHASE6_PLAN.md): three identical-content docs,
distinguished ONLY by the web graph:
  hub    — 10 distinct-domain inlinks            (ground-truth relevance 3)
  clique — 3-page single-domain reciprocal clique (ground-truth relevance 1)
  orphan — no inlinks                            (ground-truth relevance 0)

Arms compared (rerank always on):
  baseline  — all Phase 6 signal weights 0 (retrieval order only)
  authority — NEXUS_AUTHORITY_WEIGHT-style weights (source_authority=0.2,
              popularity=0.1, anchor_relevance=0.1)

Reports per-arm NDCG@10 / MRR plus the rank movement of hub/clique/orphan.
"""
import argparse
import json
import os

from ..core.hybrid_search import HybridSearch, SearchMode
from ..core.indexer import Indexer
from ..core.storage import Storage
from ..links.authority import compute_authority
from ..links.graph import LinkGraph
from ..ranking.ranker import RankingWeights, rerank
from .metrics import evaluate_query

CORPUS = [
    # (doc_id, url, inlink plan)
    ("hub", "http://hub.com/page", "hub"),
    ("clique", "http://c1.com/page", "clique"),
    ("orphan", "http://orphan.com/page", "orphan"),
]
QUERY = "search topic"
RELEVANCE = {"hub": 3, "clique": 1, "orphan": 0}  # identical content:
# the graph IS the only relevance signal, by construction


def _build(db_path: str):
    storage = Storage(db_path)
    indexer = Indexer(storage)
    graph = LinkGraph(db_path)
    for doc_id, url, _ in CORPUS:
        indexer.add_document(doc_id, "search topic content about ranking",
                             title="Same", doc_type="web",
                             metadata={"url": url})
    for i in range(10):
        graph.record_edge(f"http://d{i}.com", "http://hub.com/page",
                          f"search topic reference {i}", "")
    graph.record_edge("http://c1.com/page", "http://c2.com/page", "mutual", "")
    graph.record_edge("http://c2.com/page", "http://c3.com/page", "mutual", "")
    graph.record_edge("http://c3.com/page", "http://c1.com/page", "mutual", "")
    graph.record_edge("http://orphan.com/page", "http://nowhere.com/", "out", "")
    compute_authority(graph, force=True)
    return storage, graph


def run_authority_benchmark(db_path: str = None, quiet: bool = False) -> dict:
    import tempfile
    tmpdir = None
    if db_path is None:
        tmpdir = tempfile.TemporaryDirectory()
        db_path = os.path.join(tmpdir.name, "bench.db")
    storage, graph = _build(db_path)
    try:
        hybrid = HybridSearch(storage, db_path=db_path)
        try:
            page = hybrid.search_page(QUERY, top_k=10, mode=SearchMode.HYBRID)
        finally:
            hybrid.close()
        retrieval_order = [r.doc_id for r in page.results]

        def arm(weights: RankingWeights):
            ranked = rerank(page.results, QUERY, weights=weights,
                            storage=storage, link_intel=graph)
            order = [r.doc_id for r in ranked]
            metrics = evaluate_query(RELEVANCE, order, k_values=[10])
            return order, metrics

        base_order, base_metrics = arm(RankingWeights())
        auth_order, auth_metrics = arm(RankingWeights(
            source_authority=0.2, popularity=0.1, anchor_relevance=0.1))

        def movement(order):
            return {doc_id: (retrieval_order.index(doc_id) + 1
                             if doc_id in retrieval_order else None,
                             order.index(doc_id) + 1)
                    for doc_id, _, _ in CORPUS}

        result = {
            "query": QUERY,
            "retrieval_order": retrieval_order,
            "baseline_order": base_order,
            "authority_on_order": auth_order,
            "rank_movement": {"baseline": movement(base_order),
                              "authority_on": movement(auth_order)},
            "baseline": {k: base_metrics[k] for k in ("ndcg@10", "mrr")},
            "authority_on": {k: auth_metrics[k] for k in ("ndcg@10", "mrr")},
            "ground_truth": RELEVANCE,
        }
    finally:
        graph.close()
        storage.close()
        if tmpdir is not None:
            tmpdir.cleanup()

    if not quiet:
        print(json.dumps(result, indent=2, sort_keys=True))
    return result


def main(argv=None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    result = run_authority_benchmark()
    print("\n=== Authority on/off (mission shape) ===")
    print(f"retrieval order : {result['retrieval_order']}")
    print(f"baseline (0.0)  : {result['baseline_order']}  "
          f"NDCG@10={result['baseline']['ndcg@10']:.3f}")
    print(f"authority ON    : {result['authority_on_order']}  "
          f"NDCG@10={result['authority_on']['ndcg@10']:.3f}")
    ok = result["authority_on"]["ndcg@10"] >= result["baseline"]["ndcg@10"]
    print(f"ndcg_delta      : "
          f"{result['authority_on']['ndcg@10'] - result['baseline']['ndcg@10']:+.3f} "
          f"({'uplift' if ok else 'REGRESSION'})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
