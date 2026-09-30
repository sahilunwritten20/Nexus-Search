"""Audit repro measurements (WP0 baseline, re-run at the final gate).

Measures — never asserts — the five reproduced audit defects so the same
script produces comparable before/after numbers:

  1. junk-query understand_query CPU time at 229/629/1689/2000 chars
  2. hybrid pagination: offset=50 with default candidates
  3. hybrid top_k=100 with default candidates
  4. link-graph URL-variant authority lookup (fragment/utm/slash/case)
  5. edge ingest scaling: record_edge at 250/500/1000 edges + record_edges 10k

Usage:  python scripts/dev/audit_repro.py [--json out.json]
"""
import argparse
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.core.hybrid_search import HybridSearch, SearchMode  # noqa: E402
from nexus_search.core.indexer import Indexer  # noqa: E402
from nexus_search.core.storage import Storage  # noqa: E402
from nexus_search.links.authority import compute_authority  # noqa: E402
from nexus_search.links.graph import LinkGraph  # noqa: E402
from nexus_search.ranking.query import understand_query  # noqa: E402


def _junk_query(n_chars: int) -> str:
    q, i = "", 0
    while len(q) < n_chars:
        q += f" zqvxj{i}"
        i += 1
    return q[:n_chars].strip()


def measure_junk_queries() -> dict:
    out = {}
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(os.path.join(tmp, "s.db"))
        Indexer(storage).add_document(
            "d1", "normal doc about python search engines", title="Python Search")
        for n in (229, 629, 1689, 2000):
            q = _junk_query(n)
            t0 = time.perf_counter()
            understand_query(q, storage)
            out[f"{n}chars_s"] = round(time.perf_counter() - t0, 3)
        storage.close()
    return out


def measure_pagination() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "p.db")
        storage = Storage(db)
        ix = Indexer(storage)
        for i in range(120):
            ix.add_document(f"d{i}", f"alpha beta gamma document number {i}",
                            title=f"Doc {i}")
        h = HybridSearch(storage, db_path=db)
        p1 = h.search_page("alpha beta", top_k=50, offset=0,
                           mode=SearchMode.HYBRID, candidates=50)
        p2 = h.search_page("alpha beta", top_k=50, offset=50,
                           mode=SearchMode.HYBRID, candidates=50)
        p3 = h.search_page("alpha beta", top_k=100, offset=0,
                           mode=SearchMode.HYBRID, candidates=50)
        h.close()
        storage.close()
        return {
            "offset50_results": len(p2.results),
            "offset50_total": p2.total,
            "page1_results": len(p1.results),
            "topk100_results": len(p3.results),
        }


def measure_url_variants() -> dict:
    """Hub linked via 4 URL variants; doc stored under the normalized URL."""
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "g.db")
        storage = Storage(db)
        Indexer(storage).add_document(
            "hub", "hub page content", title="Hub", doc_type="web",
            metadata={"url": "http://hub.com/page"})
        graph = LinkGraph(db)
        variants = [
            "http://d1.com", "http://d2.com", "http://d3.com", "http://d4.com",
        ]
        for src, tgt in [
            ("http://d1.com", "http://hub.com/page#section"),
            ("http://d2.com", "http://hub.com/page?utm_source=x"),
            ("http://d3.com", "http://hub.com/page/"),
            ("http://d4.com", "http://HUB.com/page"),
        ]:
            graph.record_edge(src, tgt)
        compute_authority(graph)
        found = graph.authority_for("http://hub.com/page")
        edge_count = graph.edge_count()
        graph.close()
        storage.close()
        return {
            "edges_stored": edge_count,       # 4 = fragmented (bug), 1 = collapsed
            "authority_found": found is not None,  # False = lookup miss (bug)
            "authority": round(found[0], 4) if found else None,
        }


def measure_ingest() -> dict:
    out = {}
    with tempfile.TemporaryDirectory() as tmp:
        graph = LinkGraph(os.path.join(tmp, "ingest.db"))
        for n in (250, 500, 1000):
            t0 = time.perf_counter()
            for i in range(n):
                graph.record_edge(f"http://s{n}-{i}.com", f"http://t{n}-{i}.com")
            out[f"record_edge_{n}_edges_s"] = round(time.perf_counter() - t0, 2)
        graph.close()
    with tempfile.TemporaryDirectory() as tmp:
        graph = LinkGraph(os.path.join(tmp, "bulk.db"))
        edges = [(f"http://b{i//10}.com", f"http://bt{i%10}.com", "a", "")
                 for i in range(10_000)]
        t0 = time.perf_counter()
        graph.record_edges(edges)
        out["record_edges_10k_s"] = round(time.perf_counter() - t0, 2)
        graph.close()
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--json", default=None)
    args = p.parse_args()

    report = {
        "junk_query": measure_junk_queries(),
        "pagination": measure_pagination(),
        "url_variants": measure_url_variants(),
        "ingest": measure_ingest(),
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, sort_keys=True)
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
