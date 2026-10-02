"""WP5: large-graph benchmark — the measurement docs/PHASE6_PLAN.md §3
committed to ("full recompute completes in under 30 seconds for 10,000
pages / 100,000 edges on a single core").

Shape: 10,000 pages across 1,000 domains (10 pages/domain), ~100,000
edges with a deliberate hub skew (20% of links target the top-50 pages),
~5% nofollow, ~10% reciprocal — enough structure that the anti-farm
corrections actually run.

Usage:
    python -m nexus_search.evaluation.graph_benchmark            # full run
    NEXUS_GRAPH_BENCH_SMOKE=1 python -m ...                      # reduced CI mode
"""
import argparse
import json
import os
import random
import tempfile
import time

from ..links.authority import compute_authority
from ..links.graph import LinkGraph

FULL_PAGES = 10_000
FULL_EDGES = 100_000
SMOKE_PAGES = 500
SMOKE_EDGES = 5_000
NOFOLLOW_RATE = 0.05
HUB_RATE = 0.20
HUB_COUNT = 50


def _smoke_mode() -> bool:
    return (os.environ.get("NEXUS_GRAPH_BENCH_SMOKE", "") or "").strip() \
        in ("1", "true", "yes")


def generate_edges(n_pages: int, n_edges: int, domains: int = 1000,
                   seed: int = 42):
    """Deterministic edge list: pages spread over `domains` hosts, hub skew,
    nofollow sprinkling. Yields (from_url, to_url, anchor, rel) tuples."""
    rng = random.Random(seed)

    def page_url(i: int) -> str:
        return f"http://d{i % domains}.com/p{i}"

    pages = [page_url(i) for i in range(n_pages)]
    hubs = pages[:HUB_COUNT]
    out = []
    for e in range(n_edges):
        src = pages[rng.randrange(n_pages)]
        if rng.random() < HUB_RATE:
            dst = hubs[rng.randrange(len(hubs))]
        else:
            dst = pages[rng.randrange(n_pages)]
        if src == dst:
            continue
        rel = "nofollow" if rng.random() < NOFOLLOW_RATE else ""
        out.append((src, dst, f"link number {e}", rel))
    return out


def run_benchmark(pages: int = None, edges: int = None, quiet: bool = False) -> dict:
    smoke = _smoke_mode()
    pages = pages or (SMOKE_PAGES if smoke else FULL_PAGES)
    edges = edges or (SMOKE_EDGES if smoke else FULL_EDGES)

    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bench.db")
        graph = LinkGraph(db)
        try:
            edge_list = generate_edges(pages, edges)
            t0 = time.perf_counter()
            inserted = graph.record_edges(edge_list)
            ingest_s = time.perf_counter() - t0

            t0 = time.perf_counter()
            stats = compute_authority(graph, force=True)
            compute_s = time.perf_counter() - t0

            scored = graph.conn.execute(
                "SELECT COUNT(*) FROM authority_scores").fetchone()[0]
            anchors = graph.conn.execute(
                "SELECT COUNT(*) FROM url_anchors").fetchone()[0]
        finally:
            graph.close()

    result = {
        "mode": "smoke" if smoke else "full",
        "pages": pages,
        "edges_requested": edges,
        "edges_inserted": inserted,
        "ingest_seconds": round(ingest_s, 2),
        "recompute_seconds": round(compute_s, 2),
        "recompute_converged": stats.converged,
        "recompute_iterations": stats.iterations,
        "urls_scored": scored,
        "urls_with_anchors": anchors,
        "budget_30s_ok": compute_s < 30.0,
    }
    if not quiet:
        print(json.dumps(result, indent=2, sort_keys=True))
    return result


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pages", type=int, default=None)
    p.add_argument("--edges", type=int, default=None)
    p.add_argument("--json", default=None, help="also write results to this path")
    args = p.parse_args(argv)
    result = run_benchmark(args.pages, args.edges)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, sort_keys=True)
        print(f"wrote {args.json}")
    if result["mode"] == "full" and not result["budget_30s_ok"]:
        print("FAILED: full-mode recompute exceeded the PHASE6_PLAN 30s bound")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
