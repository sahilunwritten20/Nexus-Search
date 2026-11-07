"""Evaluation package for Nexus Search Phase 4."""

from .benchmark import build_search_functions, run_benchmark, run_quick_benchmark
from .dataset import (
    Document,
    Query,
    create_benchmark_dataset,
    load_dataset,
    save_dataset,
)
from .metrics import (
    compare_modes,
    evaluate_all,
    evaluate_query,
    mrr,
    ndcg_at_k,
    precision_at_k,
    print_comparison,
    recall_at_k,
)

__all__ = [
    'Document',
    'Query',
    'build_search_functions',
    'compare_modes',
    'create_benchmark_dataset',
    'evaluate_all',
    'evaluate_query',
    'load_dataset',
    'mrr',
    'ndcg_at_k',
    'precision_at_k',
    'print_comparison',
    'recall_at_k',
    'run_benchmark',
    'run_quick_benchmark',
    'save_dataset',
]
