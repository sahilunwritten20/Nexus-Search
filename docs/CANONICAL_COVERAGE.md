# Canonical Coverage Report — 2026-11-07 (FULL SUITE)

Branch: review/hardening. Commit: 4754367 (after WP-A/1).
Command: `python -m pytest tests/ --cov=nexus_search --cov-branch --cov-report=term-missing -q -p no:randomly`
Output: `docs/evidence/coverage_full.json` (raw), `docs/evidence/coverage_full.xml` (XML)

---

## Full-Suite Results (Measured from coverage.json)

| Metric | Value |
|--------|-------|
| **Overall Line Coverage** | **94.21%** (7505/7966 statements) |
| **Overall Branch Coverage** | **87.85%** (2147/2444 branches) |
| **Tests** | 1536 passed, 13 skipped, 846 subtests |
| **Runtime** | ~680s |

---

## Per-Module Coverage (Security-Critical Modules)

| Module | Line % | Branch % | Floor (97/90) | Status |
|--------|--------|----------|---------------|--------|
| `nexus_search/core/storage.py` | 98.41% | 100.00% | 97/90 | ✅ **MET** |
| `nexus_search/crawler/security.py` | 99.07% | 94.74% | 97/90 | ✅ **MET** |
| `nexus_search/rag/sanitize.py` | 100.00% | 94.44% | 97/90 | ✅ **MET** |
| `nexus_search/rag/egress.py` | 100.00% | 100.00% | 97/90 | ✅ **MET** |
| `nexus_search/rag/gate.py` | 98.21% | 92.86% | 97/90 | ✅ **MET** |
| `nexus_search/rag/retrieval.py` | 99.30% | 90.48% | 97/90 | ✅ **MET** |
| `nexus_search/crawler/fetcher.py` | 99.05% | 98.28% | 97/90 | ✅ **MET** |
| `nexus_search/crawler/url_utils.py` | 100.00% | 100.00% | 97/90 | ✅ **MET** |
| `nexus_search/core/models.py` | 98.41% | 75.00% | 97/90 | ❌ **NOT MET** (branch) |
| `nexus_search/core/api.py` | 88.31% | 79.87% | 97/90 | ❌ **NOT MET** |
| `nexus_search/rag/session.py` | 96.69% | 95.16% | 97/90 | ❌ **NOT MET** (line) |
| `nexus_search/ingestion/connectors/files.py` | 89.35% | 83.91% | 97/90 | ❌ **NOT MET** |
| `nexus_search/rag/calibration.py` | 91.28% | 87.50% | 97/90 | ❌ **NOT MET** |
| `nexus_search/rag/__main__.py` | 94.52% | 90.00% | 97/90 | ❌ **NOT MET** (line) |
| `nexus_search/rag/budget.py` | 89.19% | 90.00% | 97/90 | ❌ **NOT MET** (line) |
| `nexus_search/rag/provider.py` | 92.59% | 85.00% | 97/90 | ❌ **NOT MET** (both) |
| `nexus_search/rag/pipeline.py` | 89.66% | 82.05% | 97/90 | ❌ **NOT MET** |
| `nexus_search/rag/citations.py` | 98.47% | 90.91% | 97/90 | ✅ **MET** |
| `nexus_search/rag/context.py` | 97.35% | 90.48% | 97/90 | ✅ **MET** |
| `nexus_search/core/migrations.py` | 90.32% | 90.00% | 97/90 | ❌ **NOT MET** |
| `nexus_search/core/backup.py` | 97.22% | 87.50% | 97/90 | ❌ **NOT MET** (branch) |
| `nexus_search/crawler/blocklist.py` | 97.83% | 87.50% | 97/90 | ❌ **NOT MET** (branch) |
| `nexus_search/crawler/robots.py` | 92.75% | 87.50% | 97/90 | ❌ **NOT MET** |

---

## Overall Gates

| Scope | Line % | Branch % | Meets Gate? |
|-------|--------|----------|-------------|
| **All nexus_search (103 modules)** | 94.21% | 87.85% | ✅ Line / ❌ Branch (85% required) |
| **Core excl. eval benchmarks (89 modules)** | 94.21% | 87.85% | ✅ Line / ❌ Branch (85% required) |

**Note**: Both overall and core miss the 85% branch gate. The 85% branch gate is the primary blocker.

---

## Discrepancy Log (Earlier Claims vs Full-Suite Truth)

| Module | Claimed S1.8 | Claimed Targeted | **Full-Suite Truth** | Explanation |
|--------|--------------|------------------|---------------------|-------------|
| `api.py` | 86.5% | 58% / 55% | **88.31%** | Targeted runs missed auth/filter paths; full suite exercises all routes |
| `files.py` | 81.5% | 89% / 87% | **89.35%** | Targeted runs with new tests covered more; full suite confirms |
| `security.py` | 96.6% | 47% / 99% | **99.07%** | S1.8 measured different scope; targeted run missed some paths |
| `session.py` | 83.2% | 96% | **96.69%** | Targeted tests lifted coverage; full suite confirms |
| `fetcher.py` | 82.4% | 68% / 98% | **99.05%** | Targeted run covered more; full suite confirms high coverage |
| `storage.py` | 90.7% | 98% | **98.41%** | Consistent |
| `sanitize.py` | 96.9% | 100% | **100.00%** | Targeted tests covered StreamLeakScrubber |
| `rag/__main__.py` | 0% | 94% | **94.52%** | In-process CLI tests work |
| `models.py` | unmeasured | unmeasured | **98.41% line / 75% branch** | New measurement |

**Branch > Line**: Some modules have branch < line because branches include partial coverage on error paths (e.g., `models.py` has 100% line but only 75% branch due to untested exception paths).

---

## Earlier CANONICAL_COVERAGE.md Status

**SUPERSEDED** — This document replaces the previous CANONICAL_COVERAGE.md which was based on targeted runs. Full-suite numbers above are the ground truth.