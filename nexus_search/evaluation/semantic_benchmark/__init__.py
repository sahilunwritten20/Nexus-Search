"""WP10: labeled semantic-quality benchmark.

Fixture corpus (deterministic construction, labels are ground truth BY
CONSTRUCTION — the labeling protocol is documented in the README of this
package): >= 300 docs across 12 topics; >= 60 queries across six categories
deliberately chosen to stress what an embedder is for:

  paraphrase  — reworded, little or no keyword overlap with the target docs
  synonym     — synonyms of the target vocabulary
  exact       — exact keyword/identifier queries
  multi-intent— two topics in one query
  typo        — 1-2 edit typos of target terms
  no-answer   — no relevant document exists (score-distribution honesty)

Split: dev/test 60/40 by a fixed seed, committed here. Systems compared at
K=10 on identical data: keyword BM25; semantic st:all-MiniLM-L6-v2;
semantic hash:384 (control); hybrid RRF (st); hybrid weighted (st); each
with/without query understanding; hybrid-st with/without rerank.

Metrics: P@10, R@10, MRR, NDCG@10 (graded, the existing formula), latency
p50/p95, vector coverage (refuses to report below 100%). Per-category
breakdown for key systems. Paired bootstrap CIs for st-vs-hash and
hybrid-vs-keyword on NDCG@10.

Run: python -m nexus_search.evaluation.semantic_benchmark
Outputs JSON + a markdown table to docs/SEMANTIC_BENCHMARK.md.
"""
from .fixture import build_fixture, SPLIT_SEED  # noqa: F401
from .runner import run_benchmark, main  # noqa: F401

__all__ = ["build_fixture", "run_benchmark", "main", "SPLIT_SEED"]
