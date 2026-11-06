# Canonical Coverage Report — 2026-11-06

Branch: review/hardening. Commit: abd45bc.
Command: `python -m pytest tests/ --cov=nexus_search --cov-branch --cov-report=term-missing -q -p no:randomly`
Note: Full suite runs >5 min; numbers below are compiled from targeted runs covering all nexus_search modules.

---

## Security-Critical Modules (Floor: 97% line / 90% branch)

| Module | Line % | Branch % | Status | Missing Lines |
|--------|--------|----------|--------|---------------|
| `nexus_search/rag/sanitize.py` | **99%** | **100%** | ✅ MET | 135 stmts, 0 miss |
| `nexus_search/crawler/security.py` | **99%** | **100%** | ✅ MET | 107 stmts, 1 miss |
| `nexus_search/core/storage.py` | **98%** | — | ✅ MET | 190 stmts, 3 miss (137-139 rollback) |
| `nexus_search/rag/egress.py` | **100%** | **100%** | ✅ MET | 21 stmts, 0 miss |
| `nexus_search/rag/gate.py` | **97%** | **100%** | ✅ MET | 57 stmts, 1 miss |
| `nexus_search/rag/retrieval.py` | **97%** | **100%** | ✅ MET | 144 stmts, 1 miss |
| `nexus_search/rag/session.py` | **96%** | **100%** | ⚠️ NEEDS +1% | 182 stmts, 6 miss |
| `nexus_search/ingestion/connectors/files.py` | **87%** | **93%** | ❌ NEEDS +10% | 508 stmts, 56 miss |
| `nexus_search/crawler/fetcher.py` | **68%** | — | ❌ NEEDS +29% | 209 stmts, 66 miss |
| `nexus_search/core/api.py` | **58%** | — | ❌ NEEDS +39% | 556 stmts, 236 miss |

---

## Overall Coverage (Gate: 92% line / 85% branch)

| Scope | Line % | Branch % | Notes |
|-------|--------|----------|-------|
| **All nexus_search** | **62%** | **89%** | Includes evaluation/benchmark modules (0-15%) |
| **Core modules only** (excl. evaluation) | **~85%** | **~92%** | Estimated from targeted runs |

**Explanation**: The "overall" 62% is dragged down by evaluation modules (benchmarks) that are env-gated and not run in CI. The mission gate applies to production code. Excluding `nexus_search/evaluation/` (12 modules, 0-15% each), core coverage is ~85% line / ~92% branch — still below the 92/85 gate.

---

## Earlier Numbers vs Reality

| Module | Claimed (S1.8) | Actual (Targeted Runs) | Discrepancy |
|--------|----------------|------------------------|-------------|
| api.py | 86.5% | 58% | -28.5% (partial run, missing error paths) |
| fetcher.py | 82.4% | 68% | -14.4% (JS render, charset, error paths untested) |
| files.py | 81.5% | 87% | +5.5% (new tests added) |
| session.py | 83.2% | 96% | +12.8% (new tests added) |
| security.py | 47% | 99% | +52% (was never measured; existing tests cover it) |
| storage.py | 90.7% | 98% | +7.3% (new tests added) |
| sanitize.py | 96.9% | 99% | +2.1% (new tests added) |
| rag/__main__.py | 0% | 94% | +94% (in-process CLI tests added) |

**Root cause**: S1.8 numbers were from a single full-suite run with different test selection. The targeted runs here use the committed test files and are the ground truth.

---

## Remaining Work Table (ALL Exit Gates)

| # | Gate | Status | Evidence / Work Needed |
|---|------|--------|------------------------|
| **1** | **Coverage: Overall 92% line / 85% branch** | ❌ BLOCKING | Core ~85%/92%; evaluation drags down. Must lift core to 92/85 or exclude evaluation from gate. |
| **2** | **Security-critical floors (97/90)** | ⚠️ PARTIAL | 6/11 MET: sanitize, security, storage, egress, gate, retrieval. 5 need work: session (+1%), files (+10%), fetcher (+29%), api (+39%). |
| **3** | **Mutation testing >=85% on high-risk** | ❌ NOT DONE | mutmut blocked on Windows. Manual checks done per-fix. Need Linux CI run. |
| **4** | **Differential: BM25 vs Lucene** | ✅ PROVEN | tests/core/test_bm25_differential.py |
| **5** | **Differential: PageRank vs networkx** | ✅ PROVEN | tests/links/test_pagerank_differential.py |
| **6** | **Differential: Union-find vs networkx** | ❌ NOT DONE | No test file |
| **7** | **Differential: Retrieval metrics vs ranx/pytrec_eval** | ❌ NOT DONE | No test file |
| **8** | **Differential: Vector recall vs exact** | ✅ PROVEN | tests/core/test_vector_recall.py |
| **9** | **Differential: Fusion monotonicity** | ✅ PROVEN | tests/core/test_fusion_properties.py |
| **10** | **Migration upgrades on populated DBs** | ✅ PROVEN | tests/core/test_migration_upgrade.py (10 tests) |
| **11** | **Migration idempotence** | ❌ NOT DONE | No explicit test |
| **12** | **Backup/restore executed & verified** | ⚠️ PARTIAL | Worklog S1.14: counts ok; no SHA256 recorded |
| **13** | **Kill-9 mid-write** | ✅ PROVEN | tests/core/test_crash_safety.py |
| **14** | **Kill-9 mid-ingestion** | ❌ NOT DONE | — |
| **15** | **Kill-9 mid-crawl** | ❌ NOT DONE | — |
| **16** | **Kill-9 mid-SSE stream** | ❌ NOT DONE | — |
| **17** | **Disk-full / read-only FS** | ❌ NOT DONE | — |
| **18** | **Provider outage** | ⚠️ PARTIAL | Stream caps tested; no full outage test |
| **19** | **DNS failure** | ⚠️ PARTIAL | Pinning tests cover; no resolver failure test |
| **20** | **DB lock storm** | ❌ NOT DONE | — |
| **21** | **Route inventory from app object** | ❌ NOT DONE | No script generates route table |
| **22** | **Auth guard test for new routes** | ❌ NOT DONE | No guard test exists |
| **23** | **Docs-truth: env vars both directions** | ✅ PROVEN | scripts/dev/check_env_docs.py |
| **24** | **Docs-truth: routes vs docs** | ❌ NOT DONE | — |
| **25** | **Docs-truth: migrations list** | ❌ NOT DONE | — |
| **26** | **Docs-truth: test counts in ROADMAP.md** | ⚠️ PARTIAL | README corrected; ROADMAP not updated |
| **27** | **Static: ruff curated config** | ❌ NOT DONE | ~800 baseline; no config |
| **28** | **Static: mypy strict config** | ❌ NOT DONE | 155 errors w/o config |
| **29** | **Static: suppression comments justified** | ❌ NOT DONE | 14 noqa sites read; not all wired |
| **30** | **Refuse-gate calibration (train-only)** | ❌ NOT DONE | calibration.py::_gate_refuses needs honest held-out eval |
| **31** | **Round-3 Skeptic Pass** | ❌ NOT DONE | Fresh-context review of all post-R2 changes |
| **32** | **Final Seal (3x random + 1x xdist)** | ❌ NOT DONE | Requires all above green |
| **33** | **Real-embedder benchmark (CPU)** | ❌ NOT DONE | all-MiniLM-L6-v2 on CPU; test if download works |
| **34** | **Real-embedder refuse-gate** | ❌ NOT DONE | Same |

---

## Priority Order for WP1 Continuation

1. **files.py** (87% → 97%): encoding fallbacks, OCR, optional deps
2. **fetcher.py** (68% → 92%): JS render, charset, error paths, retries
3. **api.py** (58% → 92%): error paths, filter validation, auth failures
4. **session.py** (96% → 97%): 6 lines (rollback, expired, count)
5. **gate.py** (97% → 97%): 1 line (93, 145)
6. **Evaluate if evaluation modules should be excluded from overall gate** — they are env-gated benchmarks, not production code.

---

## Next Commit Plan

1. Commit files.py coverage tests (current)
2. Add fetcher.py coverage tests
3. Add api.py coverage tests
4. Add session.py coverage tests
5. Document gate decision on evaluation modules