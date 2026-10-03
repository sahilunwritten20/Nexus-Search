"""WP8: scale benchmark for the audit's 'potential performance' items at
1K / 5K / 10K synthetic docs, hash embedder. Reports measured cost per item;
fixes only what the numbers prove is worse than linear. Everything here is
prototype-scale-acknowledged (SPEC.md) — this benchmark keeps the
acknowledgment honest.

Usage: python -m nexus_search.evaluation.scale_benchmark
"""
import json
import os
import time

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from ..core.hybrid_search import HybridSearch, SearchMode  # noqa: E402
from ..core.indexer import Indexer  # noqa: E402
from ..core.storage import Storage  # noqa: E402


def _build_corpus(db_path: str, n: int) -> HybridSearch:
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


def run_scale_benchmark(sizes=(1_000, 5_000, 10_000)) -> dict:
    import tempfile
    results = {}
    for n in sizes:
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "scale.db")
        t0 = time.perf_counter()
        hybrid = _build_corpus(db, n)
        build_s = time.perf_counter() - t0

        def timeit(fn, repeat=3):
            fn()  # warm
            best = float("inf")
            for _ in range(repeat):
                t = time.perf_counter()
                fn()
                best = min(best, time.perf_counter() - t)
            return best * 1000

        page = hybrid.search_page("python search", top_k=10,
                                   mode=SearchMode.HYBRID)
        assert page.results
        hybrid_ms = timeit(lambda: hybrid.search_page(
            "python search", top_k=10, mode=SearchMode.HYBRID))
        keyword_ms = timeit(lambda: hybrid.search_page(
            "python search", top_k=10, mode=SearchMode.KEYWORD))
        semantic_ms = timeit(lambda: hybrid.search_page(
            "python search", top_k=10, mode=SearchMode.SEMANTIC))

        # vocab load (understanding's cold pass)
        from ..ranking.query import _vocabulary
        hybrid._bench_storage_lock = None
        t0 = time.perf_counter()
        _vocabulary(hybrid._bench_storage)
        vocab_ms = (time.perf_counter() - t0) * 1000

        results[n] = {
            "docs": n,
            "build_seconds": round(build_s, 1),
            "hybrid_ms": round(hybrid_ms, 1),
            "keyword_ms": round(keyword_ms, 1),
            "semantic_ms": round(semantic_ms, 1),
            "vocab_cold_ms": round(vocab_ms, 1),
            "vector_count": vs_count(hybrid),
        }
        print(json.dumps(results[n]))
        hybrid._bench_sync.close()
        hybrid.close()
        hybrid._bench_vs.close()
        hybrid._bench_storage.close()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    return results


def vs_count(hybrid) -> int:
    return hybrid.vector_store.get_stats().get("count", 0)


def main() -> int:
    print("=== scale benchmark (hash embedder, Windows/Python 3.14) ===")
    results = run_scale_benchmark()
    # honesty check: search latency should be roughly linear (or better) in
    # corpus size; super-linear growth would be a confirmed fix-now finding
    if 1_000 in results and 10_000 in results:
        ratio = results[10_000]["hybrid_ms"] / max(results[1_000]["hybrid_ms"], 0.1)
        print(f"hybrid latency growth 1K->10K: x{ratio:.1f} (linear would be ~10x)")
    print("NOTE: SQLite-scale limits are acknowledged by SPEC.md; these "
          "numbers document the cost curve, they do not gate CI.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
