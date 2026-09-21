"""Benchmark runner for Nexus Search Phase 4."""
import os
import tempfile
import time
from typing import Callable

# Use hash embedder for offline benchmarking
os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from ..core.storage import Storage
from ..core.indexer import Indexer
from ..core.hybrid_search import HybridSearch, SearchMode, create_hybrid_search
from ..ingestion.dedup import Deduplicator
from ..ingestion.pipeline import ingest_documents
from ..ingestion.types import IngestDoc
from .dataset import create_benchmark_dataset
from .metrics import evaluate_all, compare_modes, print_comparison


def build_search_functions(db_path: str = None) -> dict[str, Callable[[str, int], list[str]]]:
    """Create search functions for each mode."""
    if db_path is None:
        db_path = os.path.join(tempfile.mkdtemp(), "bench.db")

    storage = Storage(db_path)
    indexer = Indexer(storage)
    dedup = Deduplicator(db_path)
    hybrid = create_hybrid_search(storage, db_path=db_path)

    # Index benchmark documents
    docs, _ = create_benchmark_dataset()
    ingest_docs = [
        IngestDoc(d.doc_id, d.title, d.content, d.doc_type, d.metadata or {})
        for d in docs
    ]
    ingest_documents(ingest_docs, indexer, dedup, min_quality=0.0, chunk_size=None)

    # Wait a bit for embeddings to be generated
    time.sleep(0.5)

    def make_search_fn(mode: SearchMode):
        def search_fn(query: str, top_k: int) -> list[str]:
            results = hybrid.search(query, top_k=top_k, mode=mode)
            return [r.doc_id for r in results]
        return search_fn

    return {
        "keyword": make_search_fn(SearchMode.KEYWORD),
        "semantic": make_search_fn(SearchMode.SEMANTIC),
        "hybrid": make_search_fn(SearchMode.HYBRID),
    }, hybrid, storage, dedup


def run_benchmark(db_path: str = None) -> dict:
    """Run full benchmark and return results."""
    _, queries = create_benchmark_dataset()

    query_dicts = [
        {"query_id": q.query_id, "text": q.text, "relevant_docs": q.relevant_docs}
        for q in queries
    ]

    search_fns, hybrid, storage, dedup = build_search_functions(db_path)

    try:
        comparison = compare_modes(query_dicts, search_fns)
        print_comparison(comparison)
        return comparison
    finally:
        hybrid.close()
        storage.close()
        dedup.close()


def run_quick_benchmark() -> dict:
    """Run benchmark with in-memory-like temp DB."""
    import tempfile
    db_path = os.path.join(tempfile.mkdtemp(), "quick_bench.db")
    return run_benchmark(db_path)


if __name__ == "__main__":
    run_quick_benchmark()