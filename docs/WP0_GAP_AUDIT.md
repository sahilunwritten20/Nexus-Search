# WP0 Gap Audit — Exit Gates vs Measured Reality (FINAL — Full Suite)

Date: 2026-11-06. Branch: review/hardening. Commit: 7c344a5.

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

**Action**: Run mutmut in Linux CI (WSL/Docker). For now: document as PARTIAL with manual checks logged.

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

## 9. Security-Critical Module Coverage Floors (FULL-SUITE MEASURED)

| Module | Line % | Branch % | Floor Line | Floor Branch | Status |
|--------|--------|----------|------------|--------------|--------|
| storage.py | 99% | 99% | 97% | 90% | ✅ **MET** |
| security.py | 98% | 95% | 97% | 90% | ✅ **MET** |
| sanitize.py | 99% | 94% | 97% | 90% | ✅ **MET** |
| egress.py | 100% | 100% | 97% | 90% | ✅ **MET** |
| gate.py | 97% | 93% | 97% | 90% | ✅ **MET** |
| retrieval.py | 97% | 91% | 97% | 90% | ✅ **MET** |
| fetcher.py | 99% | 98% | 97% | 90% | ✅ **MET** |
| url_utils.py | 100% | 100% | 97% | 90% | ✅ **MET** |
| session.py | 96% | 95% | 97% | 90% | ❌ **NOT MET** (line 96% < 97%) |
| files.py | 88% | 93% | 97% | 90% | ❌ **NOT MET** (line 88% < 97%) |
| calibration.py | 90% | 90% | 97% | 90% | ❌ **NOT MET** (both) |
| __main__.py | 94% | 90% | 97% | 90% | ❌ **NOT MET** (line 94% < 97%) |
| api.py | 86% | 81% | 97% | 90% | ❌ **NOT MET** (both) |

**Overall Core (excl. evaluation)**: 93% line / 91% branch → **MET 92/85 gate**
**Overall All**: 90% line / 89% branch → **NOT MET 92/85 gate**
**Exclusion**: evaluation/ modules excluded via ADR-003 (docs/COVERAGE_EXCLUSION_ADR.md)

---

## 10. Real-Embedder Status

| Item | Status | Evidence |
|------|--------|----------|
| sentence-transformers path env-gated | PROVEN | NEXUS_RUN_MODEL_TESTS=1 required |
| What IS verified with real embedder | PARTIAL | Model-gated arm: 1176 passed / 5 skipped (worklog S1.3) |
| What is NOT verified | DOCUMENTED | Refuse-gate calibration, benchmark floors, semantic search quality |
| Benchmark under real embedder | NOT DONE (P2) | Command: `NEXUS_RUN_MODEL_TESTS=1 pytest tests/evaluation/` |
| Refuse-gate under real embedder | NOT DONE (P1) | Same |
| CPU test (all-MiniLM-L6-v2) | NOT RUN | Blocker: network/download in CI; exact command: `pip install sentence-transformers && NEXUS_RUN_MODEL_TESTS=1 pytest tests/rag/test_calibration.py -q` |

---

## 11. Finding Count Reconciliation

| Source | Claimed | Verified |
|--------|---------|----------|
| Worklog S1.4 (Round 1) | 17 fixed | 17 in REVIEW_REPORT.md |
| Worklog S1.16 (Round 2 skeptic) | 8 crawler + 13 RAG = 21 | 21 in REVIEW_REPORT.md |
| Worklog S1.17 (Round 2 seal) | 28 total (3 P1, 11 P2, 14 P3) | 28 in CLAIMS_LEDGER |
| REVIEW_CHECKLIST.md findings table | 18 entries (37-54) | 18 entries |
| Git log (fix commits) | Count from commits | 7c344a5 message: "WP0/1: Canonical coverage audit + security-critical coverage lift" |

**Discrepancy**: Checklist shows 18 findings (rows 37-54) but 28 claimed. Need to add missing 10 findings to checklist.

---

## Summary: NOT DONE / PARTIAL by Severity

| Severity | Count | Items |
|----------|-------|-------|
| P1 | 10 | Mutation (SSRF, sanitize, refuse-gate, auth, budget), Route guard, Security-critical coverage (session, files, calibration, __main__, api), Real-embedder refuse-gate, Route inventory, Auth guard test |
| P2 | 18 | Mutation (other modules), Union-find diff, Retrieval vs ranx, Migration idempotence, Chaos (ingest/crawl/SSE/disk/ro/lock), Docs-truth (routes, migrations, ROADMAP), Static (mypy, noqa, suppression), Security-critical coverage (api), Real-embedder benchmark, Backup checksums, Mutation testing, Refuse-gate calibration, Round-3 skeptic |
| P3 | 5 | pip-licenses (DONE), suppression wiring, backup checksums, ROADMAP update, finding-count reconciliation |

---

## Next: WP1 — Coverage Lift (Priority: Security-Critical Modules First)

Target order (security-critical first):
1. **api.py** (86% → 97% line, 81% → 90% branch) — P1 (auth, limiter, filters, bulk, reindex, admin)
2. **files.py** (88% → 97% line, 93% → 90% branch) — P1 (encoding fallbacks, OCR, optional deps)
3. **session.py** (96% → 97% line, 95% → 90% branch) — P1 (rollback, expired, count)
4. **gate.py** (97% → 97% line, 93% → 90% branch) — P1 (lines 93, 145)
5. **calibration.py** (90% → 97% line, 90% → 90% branch) — P1 (refuse-gate logic)
6. **rag/__main__.py** (94% → 97% line, 90% → 90% branch) — P1 (CLI edge cases)