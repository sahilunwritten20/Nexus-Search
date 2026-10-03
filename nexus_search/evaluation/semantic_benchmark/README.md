# Semantic Benchmark Package (WP10)

## Labeling protocol (honest scope)

The fixture (`fixture.py`) is CONSTRUCTION-GROUND-TRUTH: queries are built
from topic definitions, so relevance labels are known exactly (core topic
doc = 3, secondary = 2/1, none = 0). There is **no human second pass** —
that is a stated limitation of this benchmark; a human-labeled set (or a
small public IR set like SciFact) would strengthen the conclusions and
remains future work. The construction is deterministic (SPLIT_SEED committed),
so every number is reproducible.

Split: dev/test 60/40, stratified per category, seed 20260101. Numbers in
docs/SEMANTIC_BENCHMARK.md are reported on the HELD-OUT test split; nothing
was tuned on it (all defaults shipped as-is).

## Reading the results

- `no-answer` rows show NDCG 1.000 for every system — that is the metric's
  empty-relevance convention (nothing relevant existed, nothing falsely
  ranked). The honest signal is the **top-1 score distribution** in the
  report: keyword/hash serve CONFIDENT junk (scores 5.4-9.1) while
  hybrid-RRF ranks nothing above 0.03 — hybrid's low-confidence behavior
  on no-answer queries is the desirable property.
- Per-category NDCG is where the semantic story lives. Overall numbers mix
  categories with opposite winners (exact/typo favor lexical; paraphrase/
  synonym favor semantics), so the paired-bootstrap CI on the OVERALL
  difference can honestly cross zero — read it together with the per-category
  table, not instead of it.
