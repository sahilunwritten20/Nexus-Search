"""Evaluation package for Nexus Search Phase 4."""
from .dataset import create_benchmark_dataset, Document, Query, load_dataset, save_dataset
from .metrics import (
    precision_at_k, recall_at_k, mrr, ndcg_at_k,
    evaluate_query, evaluate_all, compare_modes, print_comparison
)
from .benchmark import build_search_functions, run_benchmark, run_quick_benchmark

__all__ = [
    "create_benchmark_dataset", "Document", "Query", "load_dataset", "save_dataset",
    "precision_at_k", "recall_at_k", "mrr", "ndcg_at_k",
    "evaluate_query", "evaluate_all", "compare_modes", "print_comparison",
    "build_search_functions", "run_benchmark", "run_quick_benchmark",
]