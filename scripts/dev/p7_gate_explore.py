"""Exploratory dump for gate calibration (dev tool — NOT part of the suite).
Prints per-query top-1 signals (pre- and post-rerank) plus category."""
import os
import sys
import tempfile

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))  # repo root

from nexus_search.rag import calibration as cal


def main():
    tmp = tempfile.mkdtemp(prefix="rag-explore-")
    storage = hybrid = vs = sync = None
    try:
        storage, hybrid, vs, sync, retriever, queries = cal._build_corpus(
            os.path.join(tmp, "explore.db"))
        # also capture the PRE-rerank fused top-1: rerank off for a moment
        from nexus_search.ranking.query import understand_query
        from nexus_search.core.hybrid_search import SearchMode

        rows = cal.collect_signals(retriever, queries)
        for r in sorted(rows, key=lambda x: (not x.answerable, -x.top_bm25 if x.top_bm25 else 0)):
            print(f"{'A' if r.answerable else 'U'} "
                  f"{r.query_id:<22} cat={r.category:<12} "
                  f"bm25={r.top_bm25 if r.top_bm25 is None else round(r.top_bm25, 3)!s:<8} "
                  f"vec={r.top_vector if r.top_vector is None else round(r.top_vector, 3)!s:<7} "
                  f"n={r.n_results} top={r.top_chunk_id}")

        # pre-rerank top-1 comparison on unanswerables + weak answerables
        print("\n--- pre-rerank fused top-1 (rerank off) ---")
        retriever2_use = retriever
        retriever2_use.use_rerank = False
        rows2 = cal.collect_signals(retriever2_use, queries)
        for r in sorted(rows2, key=lambda x: (not x.answerable,)):
            if not r.answerable or (r.top_bm25 or 0) < 8:
                print(f"{'A' if r.answerable else 'U'} "
                      f"{r.query_id:<22} "
                      f"bm25={r.top_bm25 if r.top_bm25 is None else round(r.top_bm25, 3)!s:<8} "
                      f"vec={r.top_vector if r.top_vector is None else round(r.top_vector, 3)!s:<7}")
        retriever2_use.use_rerank = True
    finally:
        if sync:
            sync.close()
        if hybrid:
            hybrid.close()
        if vs:
            vs.close()
        if storage:
            storage.close()
        cal._rmtree_retry(tmp)


if __name__ == "__main__":
    main()
