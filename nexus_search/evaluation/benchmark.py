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
from ..core.vector_store import VectorStoreManager
from ..core.embedding_sync import create_embedding_sync
from ..ingestion.dedup import Deduplicator
from ..ingestion.pipeline import ingest_documents
from ..ingestion.types import IngestDoc
from .dataset import create_benchmark_dataset
from .metrics import evaluate_all, compare_modes, print_comparison


def build_search_functions(db_path: str = None) -> tuple[dict[str, Callable[[str, int], list[str]]], HybridSearch, Storage, Deduplicator, VectorStoreManager]:
    """Create search functions for each mode."""
    if db_path is None:
        db_path = os.path.join(tempfile.mkdtemp(), "bench.db")

    storage = Storage(db_path)
    indexer = Indexer(storage)
    dedup = Deduplicator(db_path)
    vector_store = VectorStoreManager(db_path)
    hybrid = create_hybrid_search(storage, vector_store=vector_store, db_path=db_path)
    sync = create_embedding_sync(vector_store, batch_size=32)
    sync.attach(indexer)

    # Index benchmark documents
    docs, _ = create_benchmark_dataset()
    ingest_docs = [
        IngestDoc(d.doc_id, d.title, d.content, d.doc_type, d.metadata or {})
        for d in docs
    ]
    ingest_documents(ingest_docs, indexer, dedup, min_quality=0.0, chunk_size=None)
    
    # Flush embeddings
    sync.flush()

    def make_search_fn(mode: SearchMode):
        def search_fn(query: str, top_k: int) -> list[str]:
            results = hybrid.search(query, top_k=top_k, mode=mode)
            return [r.doc_id for r in results]
        return search_fn

    return {
        "keyword": make_search_fn(SearchMode.KEYWORD),
        "semantic": make_search_fn(SearchMode.SEMANTIC),
        "hybrid": make_search_fn(SearchMode.HYBRID),
    }, hybrid, storage, dedup, vector_store, sync


def run_benchmark(db_path: str = None) -> dict:
    """Run full benchmark and return results."""
    _, queries = create_benchmark_dataset()

    query_dicts = [
        {"query_id": q.query_id, "text": q.text, "relevant_docs": q.relevant_docs}
        for q in queries
    ]

    search_fns, hybrid, storage, dedup, vector_store, sync = build_search_functions(db_path)

    try:
        # Verify vector coverage is 100%
        vec_stats = vector_store.get_stats()
        doc_count = storage.document_count()
        if doc_count > 0:
            coverage = vec_stats.get("count", 0) / doc_count
            if coverage < 1.0:
                print(f"WARNING: Vector coverage is {coverage:.1%} ({vec_stats.get('count', 0)}/{doc_count} docs)")
                print("Refusing to print comparison numbers - vector coverage not 100%")
                return {"error": "vector_coverage_not_100%", "coverage": coverage}
        
        comparison = compare_modes(query_dicts, search_fns)
        print_comparison(comparison)
        return comparison
    finally:
        sync.close()
        hybrid.close()
        storage.close()
        dedup.close()
        vector_store.close()


def run_quick_benchmark() -> dict:
    """Run benchmark with in-memory-like temp DB."""
    import tempfile
    db_path = os.path.join(tempfile.mkdtemp(), "quick_bench.db")
    return run_benchmark(db_path)


if __name__ == "__main__":
    run_quick_benchmark()