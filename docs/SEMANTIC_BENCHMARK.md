# Semantic Benchmark (WP10)

Fixture: 168 docs / 77 queries (dev 45 / test 32, seed 20260101); reported on the held-out test split.

| system | P@10 | R@10 | MRR | NDCG@10 | p50 ms | p95 ms |
|---|---|---|---|---|---|---|
| hybrid-rrf | 0.116 | 0.719 | 0.663 | 0.604 | 33.6 | 41.1 |
| hybrid-rrf+rerank | 0.116 | 0.719 | 0.633 | 0.593 | 6.8 | 13.8 |
| hybrid-rrf+understanding | 0.119 | 0.734 | 0.672 | 0.661 | 36.0 | 85.6 |
| keyword | 0.100 | 0.641 | 0.562 | 0.596 | 1.0 | 3.3 |
| keyword+understanding | 0.106 | 0.672 | 0.638 | 0.678 | 6.0 | 39.3 |
| semantic-hash | 0.116 | 0.719 | 0.659 | 0.693 | 3.7 | 8.6 |
| semantic-st | 0.125 | 0.766 | 0.734 | 0.660 | 4.6 | 9.0 |

## Per-category NDCG@10 (key systems)

| category | hybrid-rrf | keyword | semantic-hash | semantic-st |
|---|---|---|---|---|
| exact | 0.940 | 0.904 | 0.980 | 0.966 |
| multi | 0.422 | 0.656 | 0.581 | 0.346 |
| no-answer | 1.000 | 1.000 | 1.000 | 1.000 |
| paraphrase | 0.497 | 0.328 | 0.351 | 0.625 |
| synonym | 0.612 | 0.625 | 0.715 | 0.869 |
| typo | 0.052 | 0.000 | 0.430 | 0.052 |

## Sanity + significance

- st_beats_hash_on_paraphrase: True
- hybrid_not_below_keyword_overall: True
- exact_not_hurt_by_hybrid: True
- st_vs_hash_ndcg10_ci95 (95% paired bootstrap): [-0.1593, 0.0974]
- hybrid_vs_keyword_ndcg10_ci95 (95% paired bootstrap): [-0.0592, 0.0681]

## no-answer top-1 scores (confident junk check)

- keyword: [5.365, 9.072]
- semantic-hash: [5.365, 9.072]
- hybrid-rrf: [0.026, 0.032]
