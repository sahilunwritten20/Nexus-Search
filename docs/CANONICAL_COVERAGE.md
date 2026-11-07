# Canonical Coverage Report — 2026-11-06 (FULL SUITE)

Branch: review/hardening. Commit: 7c344a5 (after WP0/1 commit).
Command: `python -m pytest --cov=nexus_search --cov-branch --cov-report=term-missing -q -p no:randomly`
Output: `docs/evidence/coverage_full_20261106_2.log` (raw), `docs/evidence/coverage_full.xml`

---

## Full-Suite Results

| Metric | Value |
|--------|-------|
| **Overall Line Coverage** | **90%** (9084 stmts, 732 miss) |
| **Overall Branch Coverage** | **89%** (2716 branches, 295 partial) |
| **Tests** | 1536 passed, 13 skipped, 846 subtests |
| **Runtime** | 858s (14:18) |

---

## Per-Module Coverage (All nexus_search modules)

| Module | Line % | Branch % | Miss | Status vs 97/90 |
|--------|--------|----------|------|-----------------|
| `nexus_search/__init__.py` | 100% | 100% | 0 | — |
| `nexus_search/core/__init__.py` | 100% | 100% | 0 | — |
| `nexus_search/core/api.py` | **86%** | **81%** | 65 | ❌ NOT MET (both) |
| `nexus_search/core/backup.py` | 95% | 88% | 1 | — |
| `nexus_search/core/bm25.py` | 95% | 91% | 7 | — |
| `nexus_search/core/diversity.py` | 89% | 85% | 4 | — |
| `nexus_search/core/embedders.py` | 92% | 86% | 11 | — |
| `nexus_search/core/embedding_sync.py` | 88% | 86% | 13 | — |
| `nexus_search/core/filters.py` | 92% | 88% | 1 | — |
| `nexus_search/core/hybrid_search.py` | 95% | 91% | 11 | — |
| `nexus_search/core/indexer.py` | 98% | 90% | 0 | — |
| `nexus_search/core/migrations.py` | 90% | 90% | 3 | — |
| `nexus_search/core/models.py` | 97% | 97% | 2 | — |
| `nexus_search/core/normalize.py` | 95% | 92% | 1 | — |
| `nexus_search/core/query_cache.py` | 100% | 100% | 0 | — |
| `nexus_search/core/query_parser.py` | 99% | 97% | 1 | — |
| `nexus_search/core/reindex.py` | 89% | 88% | 7 | — |
| `nexus_search/core/storage.py` | **99%** | **99%** | 3 | ✅ MET |
| `nexus_search/core/tokenizer.py` | 98% | 96% | 1 | — |
| `nexus_search/core/vector_maintenance.py` | 86% | 81% | 9 | — |
| `nexus_search/core/vector_store.py` | 87% | 89% | 33 | — |
| `nexus_search/crawler/__init__.py` | 100% | 100% | 0 | — |
| `nexus_search/crawler/blocklist.py` | 96% | 88% | 1 | — |
| `nexus_search/crawler/cli.py` | 86% | 79% | 19 | — |
| `nexus_search/crawler/fetcher.py` | **99%** | **98%** | 2 | ✅ MET |
| `nexus_search/crawler/frontier.py` | 98% | 89% | 1 | — |
| `nexus_search/crawler/metrics.py` | 100% | 100% | 0 | — |
| `nexus_search/crawler/pipeline.py` | 90% | 84% | 14 | — |
| `nexus_search/crawler/politeness.py` | 95% | 100% | 3 | — |
| `nexus_search/crawler/robots.py` | 91% | 91% | 10 | — |
| `nexus_search/crawler/scheduler.py` | 100% | 100% | 0 | — |
| `nexus_search/crawler/security.py` | **98%** | **95%** | 1 | ✅ MET |
| `nexus_search/crawler/sitemap.py` | 92% | 82% | 3 | — |
| `nexus_search/crawler/url_utils.py` | **100%** | **100%** | 0 | ✅ MET |
| `nexus_search/evaluation/__init__.py` | 100% | 100% | 0 | — |
| `nexus_search/evaluation/authority_benchmark.py` | 81% | 67% | 11 | — |
| `nexus_search/evaluation/benchmark.py` | 83% | 50% | 8 | — |
| `nexus_search/evaluation/dataset.py` | 46% | 83% | 17 | — |
| `nexus_search/evaluation/graph_benchmark.py` | 75% | 86% | 16 | — |
| `nexus_search/evaluation/metrics.py` | 93% | 87% | 4 | — |
| `nexus_search/evaluation/rag_benchmark/__init__.py` | 100% | 100% | 0 | — |
| `nexus_search/evaluation/rag_benchmark/fixture.py` | 83% | 94% | 11 | — |
| `nexus_search/evaluation/rag_benchmark/runner.py` | 60% | 91% | 76 | — |
| `nexus_search/evaluation/rerank_benchmark.py` | 86% | 50% | 3 | — |
| `nexus_search/evaluation/scale_benchmark.py` | 44% | 96% | 65 | — |
| `nexus_search/evaluation/semantic_benchmark/__init__.py` | 100% | 100% | 0 | — |
| `nexus_search/evaluation/semantic_benchmark/__main__.py` | 0% | 0% | 3 | — |
| `nexus_search/evaluation/semantic_benchmark/fixture.py` | 93% | 89% | 3 | — |
| `nexus_search/evaluation/semantic_benchmark/runner.py` | 77% | 87% | 42 | — |
| `nexus_search/ingestion/__init__.py` | 100% | 100% | 0 | — |
| `nexus_search/ingestion/chunker.py` | 90% | 83% | 1 | — |
| `nexus_search/ingestion/cli.py` | 98% | 92% | 1 | — |
| `nexus_search/ingestion/connectors/__init__.py` | 100% | 100% | 0 | — |
| `nexus_search/ingestion/connectors/code.py` | 87% | 90% | 4 | — |
| `nexus_search/ingestion/connectors/files.py` | **88%** | **93%** | 54 | ❌ NOT MET |
| `nexus_search/ingestion/connectors/product.py` | 100% | 100% | 0 | — |
| `nexus_search/ingestion/connectors/web.py` | 89% | 79% | 6 | — |
| `nexus_search/ingestion/dedup.py` | 96% | 100% | 3 | — |
| `nexus_search/ingestion/failures.py` | 100% | 100% | 0 | — |
| `nexus_search/ingestion/history.py` | 100% | 100% | 0 | — |
| `nexus_search/ingestion/mime.py` | 91% | 91% | 4 | — |
| `nexus_search/ingestion/pipeline.py` | 94% | 91% | 5 | — |
| `nexus_search/ingestion/quality.py` | 97% | 93% | 0 | — |
| `nexus_search/ingestion/types.py` | 100% | 100% | 0 | — |
| `nexus_search/links/__init__.py` | 100% | 100% | 0 | — |
| `nexus_search/links/authority.py` | 98% | 97% | 3 | — |
| `nexus_search/links/graph.py` | 90% | 86% | 29 | — |
| `nexus_search/links/reports.py` | 91% | 93% | 5 | — |
| `nexus_search/rag/__init__.py` | 100% | 100% | 0 | — |
| `nexus_search/rag/__main__.py` | **94%** | **90%** | 4 | ❌ NOT MET |
| `nexus_search/rag/budget.py` | 88% | 83% | 4 | — |
| `nexus_search/rag/calibration.py` | **90%** | **90%** | 13 | ❌ NOT MET |
| `nexus_search/rag/citations.py` | 97% | 91% | 2 | — |
| `nexus_search/rag/config.py` | 92% | 86% | 5 | — |
| `nexus_search/rag/context.py` | 95% | 90% | 3 | — |
| `nexus_search/rag/egress.py` | **100%** | **100%** | 0 | ✅ MET |
| `nexus_search/rag/gate.py` | **97%** | **93%** | 1 | ✅ MET |
| `nexus_search/rag/metrics.py` | 89% | 93% | 5 | — |
| `nexus_search/rag/pipeline.py` | 88% | 82% | 30 | — |
| `nexus_search/rag/prompt.py` | 96% | 90% | 2 | — |
| `nexus_search/rag/provider.py` | 90% | 85% | 32 | — |
| `nexus_search/rag/retrieval.py` | **97%** | **91%** | 1 | ✅ MET |
| `nexus_search/rag/sanitize.py` | **99%** | **94%** | 0 | ✅ MET |
| `nexus_search/rag/session.py` | **96%** | **95%** | 6 | ❌ NOT MET (line) |
| `nexus_search/ranking/__init__.py` | 100% | 100% | 0 | — |
| `nexus_search/ranking/ab.py` | 97% | 88% | 1 | — |
| `nexus_search/ranking/features.py` | 100% | 100% | 0 | — |
| `nexus_search/ranking/query.py` | 96% | 93% | 5 | — |
| `nexus_search/ranking/ranker.py` | 91% | 92% | 10 | — |
| `nexus_search/ranking/signals.py` | 95% | 96% | 5 | — |
| `nexus_search/ranking/suggestions.py` | 98% | 100% | 2 | — |

---

## Security-Critical Modules vs 97% Line / 90% Branch Floor

| Module | Line % | Branch % | Floor (97/90) | Status |
|--------|--------|----------|---------------|--------|
| `core/storage.py` | 99% | 99% | 97/90 | ✅ **MET** |
| `crawler/security.py` | 98% | 95% | 97/90 | ✅ **MET** |
| `rag/sanitize.py` | 99% | 94% | 97/90 | ✅ **MET** |
| `rag/egress.py` | 100% | 100% | 97/90 | ✅ **MET** |
| `rag/gate.py` | 97% | 93% | 97/90 | ✅ **MET** |
| `rag/retrieval.py` | 97% | 91% | 97/90 | ✅ **MET** |
| `crawler/fetcher.py` | 99% | 98% | 97/90 | ✅ **MET** |
| `crawler/url_utils.py` | 100% | 100% | 97/90 | ✅ **MET** |
| `rag/session.py` | 96% | 95% | 97/90 | ❌ **NOT MET** (line 96% < 97%) |
| `ingestion/connectors/files.py` | 88% | 93% | 97/90 | ❌ **NOT MET** (line 88% < 97%) |
| `rag/calibration.py` | 90% | 90% | 97/90 | ❌ **NOT MET** (both) |
| `rag/__main__.py` | 94% | 90% | 97/90 | ❌ **NOT MET** (line 94% < 97%) |
| `core/api.py` | 86% | 81% | 97/90 | ❌ **NOT MET** (both) |

**Summary**: 9/13 security-critical modules MET floor. 4 NOT MET: session, files, calibration, __main__, api.

---

## Discrepancy Log (Earlier Claims vs Full-Suite Truth)

| Module | Claimed S1.8 | Claimed Targeted | **Full-Suite Truth** | Explanation |
|--------|--------------|------------------|---------------------|-------------|
| `api.py` | 86.5% | 58% / 55% | **86%** | Targeted runs missed auth/filter paths; full suite exercises all routes |
| `files.py` | 81.5% | 89% / 87% | **88%** | Targeted runs with new tests covered more; full suite less than targeted |
| `security.py` | 96.6% | 47% / 99% | **98%** | S1.8 measured different scope; targeted run missed some paths; full suite is ground truth |
| `session.py` | 83.2% | 96% | **96%** | Targeted tests lifted coverage; full suite confirms |
| `fetcher.py` | 82.4% | 68% / 98% | **99%** | Targeted run covered more; full suite confirms high coverage |
| `storage.py` | 90.7% | 98% | **99%** | Consistent |
| `sanitize.py` | 96.9% | 100% | **99%** | Targeted tests covered StreamLeakScrubber; full suite 1% gap |
| `rag/__main__.py` | 0% | 94% | **94%** | In-process CLI tests work |

**Branch > Line**: Branch coverage (89%) exceeds line coverage (90%) in some modules because:
- Many branches are on error paths that are never taken (e.g., `except` blocks for impossible conditions)
- Branch coverage counts decision points; line coverage counts executable lines
- A line with `if x: ... else: ...` counts as 1 line but 2 branches

---

## Earlier CANONICAL_COVERAGE.md Status

**SUPERSEDED** — This document replaces the previous CANONICAL_COVERAGE.md which was based on targeted runs. Full-suite numbers above are the ground truth.