# WP0 Gap Audit — Exit Gates vs Measured Reality (UPDATED)

Date: 2026-11-06. Branch: review/hardening. Commit: abd45bc.

## Legend
- **PROVEN**: Command output + location saved
- **PARTIAL**: Some evidence, gap documented
- **NOT DONE**: No evidence, severity assigned

---

## 1. Mutation Testing (Target: >=85% on high-risk modules)

| Module | Tool | Score | Survivors | Status | Evidence |
|--------|------|-------|-----------|--------|----------|
| tokenizer | mutmut | NOT RUN | — | NOT DONE (P2) | mutmut blocked on Windows; manual per-fix checks only |
| BM25 | mutmut | NOT RUN | — | NOT DONE (P2) | manual checks in worklog |
| query_parser | mutmut | NOT RUN | — | NOT DONE (P2) | manual checks in worklog |
| fusion/normalization | mutmut | NOT RUN | — | NOT DONE (P2) | — |
| URL normalization | mutmut | NOT RUN | — | NOT DONE (P2) | — |
| SSRF/DNS pinning | mutmut | NOT RUN | — | NOT DONE (P1) | — |
| robots matcher | mutmut | NOT RUN | — | NOT DONE (P1) | — |
| PageRank | mutmut | NOT RUN | — | NOT DONE (P2) | — |
| citation verification | mutmut | NOT RUN | — | NOT DONE (P2) | — |
| sanitization | mutmut | NOT RUN | — | NOT DONE (P1) | — |
| refuse-gate | mutmut | NOT RUN | — | NOT DONE (P1) | — |
| auth | mutmut | NOT RUN | — | NOT DONE (P2) | — |
| budget accounting | mutmut | NOT RUN | — | NOT DONE (P2) | — |

**Action**: Run mutmut in Linux CI. For now: document as PARTIAL with manual checks logged.

---

## 2. Differential Tests

| Differential | Status | Evidence | Gap |
|--------------|--------|----------|-----|
| BM25 vs Lucene ref | PROVEN | tests/core/test_bm25_differential.py (1e-9, mutation-verified) | — |
| PageRank vs networkx | PROVEN | tests/links/test_pagerank_differential.py (8 shapes + 8 random, 5e-5) | — |
| Union-find vs networkx | NOT DONE (P2) | — | No test file |
| Retrieval metrics vs ranx/pytrec_eval | NOT DONE (P2) | — | No test file |
| Vector recall vs exact search | PROVEN | tests/core/test_vector_recall.py (top-k == brute force) | — |
| Fusion monotonicity | PROVEN | tests/core/test_fusion_properties.py (RRF, weighted, collapse) | — |

---

## 3. Migration Upgrade Tests (Populated DBs)

| Migration | Status | Evidence |
|-----------|--------|----------|
| storage v1/v3/v4 | PROVEN | tests/core/test_migration_upgrade.py (10 tests, zero row loss) |
| frontier v1->v2 | PROVEN | Same file; ancient no-etag repaired |
| link_graph v1->v3 | PROVEN | Same file; backfills verified |
| legacy vectors drop | PROVEN | Same file; dropped loudly |
| idempotence | NOT DONE (P2) | No explicit idempotence test |
| crash-safety during migration | NOT DONE (P2) | — |

---

## 4. Backup/Restore Executed & Verified

| Check | Status | Evidence |
|-------|--------|----------|
| Online snapshot under live API | PROVEN | Worklog S1.14: row counts equal, doc_id sets equal |
| PRAGMA integrity_check both sides | PROVEN | Worklog S1.14 |
| Restore verified with checksums | PARTIAL | Counts verified; no SHA256 recorded |

---

## 5. Chaos / Fault Injection (SIGTERM, disk-full, provider outage, DNS failure, DB lock storm)

| Scenario | Status | Evidence |
|----------|--------|----------|
| Kill-9 mid-write | PROVEN | tests/core/test_crash_safety.py (4 delays, integrity ok) |
| Kill-9 mid-ingestion | NOT DONE (P2) | — |
| Kill-9 mid-crawl | NOT DONE (P2) | — |
| Kill-9 mid-SSE stream | NOT DONE (P2) | — |
| Disk-full | NOT DONE (P2) | — |
| Read-only filesystem | NOT DONE (P2) | — |
| Provider outage | PARTIAL | Stream caps tested; no full outage test |
| DNS failure | PARTIAL | Pinning tests cover; no resolver failure test |
| DB lock storm | NOT DONE (P2) | — |
| Consistency checker after each | NOT DONE (P2) | — |

---

## 6. Route Inventory + Auth Guard Test

| Check | Status | Evidence |
|-------|--------|----------|
| Route table from app object | NOT DONE (P1) | No script generates route inventory |
| Guard test fails on new route without auth | NOT DONE (P1) | No guard test exists |
| All routes classified (open/key/admin) | NOT DONE (P1) | — |

---

## 7. Docs-Truth Scripts Wired into CI

| Check | Status | Evidence |
|-------|--------|----------|
| Every env var in code documented in .env.example | PROVEN | scripts/dev/check_env_docs.py: "61 documented vars" |
| Every .env.example var read in code | PROVEN | Same script |
| Routes vs docs | NOT DONE (P2) | — |
| Migrations list in docs | NOT DONE (P2) | — |
| Test counts in ROADMAP.md | PARTIAL | README corrected (A3); ROADMAP not updated |

---

## 8. Static Analysis on CURRENT HEAD

| Tool | Status | Evidence |
|------|--------|----------|
| ruff | PARTIAL | ~800 baseline; no curated config; 94 unused-noqa (RUF100) |
| formatter (ruff format) | PARTIAL | 1 BOM file (__init__.py) |
| mypy/pyright | PARTIAL | 155 errors w/o config; no strict config |
| vulture | DONE (baseline) | Unused response params, imports |
| bandit | DONE (baseline) | 21 findings triaged in worklog S1.4 |
| pip-audit | PROVEN | "No known vulnerabilities found" |
| Secret scan (git history) | PROVEN | Pattern sweep clean (gitleaks unavailable) |
| pip-licenses | PROVEN | No copyleft licenses |
| Suppression comments justified | NOT DONE (P2) | 14 noqa sites read; not all wired as live |

---

## 9. Security-Critical Module Coverage Floors

| Module | Current Line | Current Branch | Floor Line | Floor Branch | Status |
|--------|-------------|----------------|------------|--------------|--------|
| storage.py | **98%** | — | 97% | 90% | **MET** ✓ |
| security.py | **99%** | — | 97% | 90% | **MET** ✓ |
| sanitize.py | **100%** | — | 97% | 90% | **MET** ✓ |
| prompt.py | NOT MEASURED | — | 97% | 90% | UNKNOWN |
| citations.py | NOT MEASURED | — | 97% | 90% | UNKNOWN |
| politeness.py | NOT MEASURED | — | 97% | 90% | UNKNOWN |
| files.py | **89%** | — | 97% | 90% | **NOT MET** (P1) |
| url_utils.py | **86%** | — | 97% | 90% | NOT MET |
| egress.py | **76%** | — | 97% | 90% | **NOT MET** (P1) |
| gate.py | **96%** | — | 97% | 90% | NOT MET (line) |
| retrieval.py | **98%** | — | 97% | 90% | **MET** (line) |
| session.py | **96%** | — | 92% | 85% | **MET** ✓ (overall) |

**Overall**: 89.2% line / 83.1% branch claimed → **NEEDS VERIFICATION**

**Progress since WP0 start:**
- storage.py: 57% → 98% (+41%)
- security.py: 47% → 99% (+52%) - already covered by existing tests
- sanitize.py: 70% → 100% (+30%)
- files.py: 70% → 89% (+19%)
- session.py: 77% → 96% (+19%)

---

## 10. Real-Embedder Status

| Item | Status | Evidence |
|------|--------|----------|
| sentence-transformers path env-gated | PROVEN | NEXUS_RUN_MODEL_TESTS=1 required |
| What IS verified with real embedder | PARTIAL | Model-gated arm: 1176 passed / 5 skipped (worklog S1.3) |
| What is NOT verified | DOCUMENTED | Refuse-gate calibration, benchmark floors, semantic search quality |
| Benchmark under real embedder | NOT DONE (P2) | Requires GPU/CI; command: `NEXUS_RUN_MODEL_TESTS=1 pytest tests/evaluation/` |
| Refuse-gate under real embedder | NOT DONE (P1) | Same |

---

## 11. Finding Count Reconciliation

| Source | Claimed | Verified |
|--------|---------|----------|
| Worklog S1.4 (Round 1) | 17 fixed | 17 in REVIEW_REPORT.md |
| Worklog S1.16 (Round 2 skeptic) | 8 crawler + 13 RAG = 21 | 21 in REVIEW_REPORT.md |
| Worklog S1.17 (Round 2 seal) | 28 total (3 P1, 11 P2, 14 P3) | 28 in CLAIMS_LEDGER |
| REVIEW_CHECKLIST.md findings table | 18 entries (37-54) | 18 entries |
| Git log (fix commits) | Count from commits | abd45bc message: "28 findings fixed" |

**Discrepancy**: Checklist shows 18 findings (rows 37-54) but 28 claimed. Need to add missing 10 findings to checklist.

---

## Summary: NOT DONE / PARTIAL by Severity

| Severity | Count | Items |
|----------|-------|-------|
| P1 | 7 | Mutation (SSRF, sanitize, refuse-gate), Route guard, Security-critical coverage (files 89%, egress 76%, url_utils 86%, gate 96%), Real-embedder refuse-gate |
| P2 | 15 | Mutation (other modules), Union-find diff, Retrieval vs ranx, Migration idempotence, Chaos (ingest/crawl/SSE/disk/ro/lock), Docs-truth (routes, migrations, ROADMAP), Static (mypy, noqa, suppression), Security-critical coverage (prompt, citations, politeness), Real-embedder benchmark |
| P3 | 3 | pip-licenses (DONE), suppression wiring, backup checksums |

---

## WP1 Progress (Coverage Lift) - IN PROGRESS

| Module | Before | After | Status |
|--------|--------|-------|--------|
| storage.py | 57% | 98% | ✓ MET security floor |
| security.py | 47% | 99% | ✓ MET security floor |
| sanitize.py | 70% | 100% | ✓ MET security floor |
| files.py | 70% | 89% | PARTIAL (needs 97%) |
| session.py | 77% | 96% | ✓ MET overall gate |
| egress.py | 76% | 76% | NOT STARTED |
| fetcher.py | 68% | 68% | NOT STARTED |
| api.py | 58% | 58% | NOT STARTED |
| gate.py | 96% | 96% | NEEDS +1% |
| rag/__main__.py | 0% | 95%* | *CLI tests added |

---

## Next Actions (Priority Order)

1. **Files.py**: Lift from 89% → 97% (remaining: encoding detection fallbacks, OCR, docx/xlsx/pptx import errors)
2. **Egress.py**: Lift from 76% → 97% (moderation paths)
3. **Fetcher.py**: Lift from 68% → 92% (JS render, charset, error paths)
4. **Api.py**: Lift from 58% → 92% (error paths, filter validation)
5. **Gate.py**: Lift from 96% → 97% (line 93, 145)
6. **Run mutmut** in Linux CI for mutation scores
7. **Route inventory + auth guard** script
8. **Real-embedder benchmark** (if GPU available)