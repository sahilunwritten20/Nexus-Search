# Review Checklist — review/hardening

One row per audit item. Status: TODO / INPROG / FIXED / VERIFIED / ACCEPTED(trigger).
Severity: P0/P1/P2/P3. Evidence = test name, command, or file:line.

## Phase A — Baseline & Truth

| ID | Area | Item | Severity | Status | Evidence/Test | Commit |
|----|------|------|----------|--------|---------------|--------|
| A1-1 | map | Full tree + line counts + module graph | P2 | INPROG | worklog S1.0 | - |
| A1-2 | map | docs/ARCHITECTURE.md written from code | P2 | TODO | - | - |
| A1-3 | map | Architecture diffed vs doc claims; discrepancies logged | P2 | TODO | - | - |
| A2-1 | baseline | Full suite x3 normal, record counts/runtime | P1 | INPROG | worklog | - |
| A2-2 | baseline | Suite x3 random order; flakiness found+fixed | P1 | TODO | - | - |
| A2-3 | baseline | Suite x1 xdist parallel | P1 | TODO | - | - |
| A2-4 | baseline | Clean-venv install from lockfile | P1 | TODO | - | - |
| A3-1 | counts | True test counts per phase 1-7 + file; fix ROADMAP | P2 | TODO | - | - |
| A4-1 | coverage | Per-module line+branch coverage; list <85% modules | P2 | TODO | - | - |
| A4-2 | coverage | Untested public functions, dead except blocks, pragmas, env-gated tests | P2 | TODO | - | - |
| A5-1 | static | ruff/flake8 strict baseline | P2 | TODO | - | - |
| A5-2 | static | format check (black/ruff-format) | P2 | TODO | - | - |
| A5-3 | static | mypy/pyright strict-ish baseline | P2 | TODO | - | - |
| A5-4 | static | vulture dead code, radon complexity | P2 | TODO | - | - |
| A5-5 | static | pip-audit/safety CVEs, bandit, secret scan (incl. history) | P1 | TODO | - | - |
| A5-6 | static | pip-licenses conflicts | P3 | TODO | - | - |
| A5-7 | static | Justify or remove every suppression comment | P2 | TODO | - | - |
| A6-1 | live | Boot service; probe /health /ready /metrics /openapi.json | P1 | TODO | - | - |
| A6-2 | live | Curl every route: valid/invalid/oversized/empty/unicode/unauth | P1 | TODO | - | - |
| A6-3 | live | OpenAPI matches behavior | P2 | TODO | - | - |
| A7-1 | testquality | Weak/duplicate/mock-only/stateful/sleepy tests identified | P2 | TODO | - | - |
| A7-2 | mutation | Mutation testing on high-risk modules, score recorded | P2 | TODO | - | - |

## Phase B — Per-Phase Adversarial Audit

| ID | Phase | Item | Severity | Status | Evidence/Test | Commit |
|----|-------|------|----------|--------|---------------|--------|
| B1-* | 1 | tokenizer Unicode/limits/positions | P1 | TODO | - | - |
| B1-* | 1 | BM25 differential vs reference impl | P1 | TODO | - | - |
| B1-* | 1 | SQLite integrity/concurrency/EXPLAIN | P1 | TODO | - | - |
| B1-* | 1 | Query parser fuzz/ReDoS/SQLi | P1 | TODO | - | - |
| B1-* | 1 | Snippets/pagination/boosting invariants | P1 | TODO | - | - |
| B2-* | 2 | Per-format adversarial corpora (bombs/XXE/limits) | P1 | TODO | - | - |
| B2-* | 2 | MIME sniff, encoding, chunking, dedup, idempotence | P1 | TODO | - | - |
| B3-* | 3 | SSRF red team (all encodings, rebinding, redirects) | P0 | TODO | - | - |
| B3-* | 3 | robots.txt RFC 9309 conformance | P1 | TODO | - | | 
| B3-* | 3 | Resilience (slowloris, bombs, loops) | P1 | TODO | - | - |
| B3-* | 3 | Frontier/persistence/kill-9/leaks | P1 | TODO | - | - |
| B4-* | 4 | Embedding lifecycle consistency checker | P1 | TODO | - | - |
| B4-* | 4 | Fusion math properties (monotonicity, NaN, ties) | P1 | TODO | - | - |
| B4-* | 4 | Vector recall vs exact ground truth | P1 | TODO | - | - |
| B4-* | 4 | Eval validity (leakage, CIs, metric impls vs ranx) | P1 | TODO | - | - |
| B5-* | 5 | Adversarial query set; spell-correction safety | P1 | TODO | - | - |
| B5-* | 5 | Signal ranges/determinism/placeholder zero-influence | P1 | TODO | - | - |
| B5-* | 5 | A/B bucketing uniformity/stability; privacy | P1 | TODO | - | - |
| B5-* | 5 | UX: XSS in highlighting, facet correctness | P1 | TODO | - | - |
| B6-* | 6 | URL normalization idempotence/collisions | P1 | TODO | - | - |
| B6-* | 6 | Graph migrations on populated DBs | P1 | TODO | - | - |
| B6-* | 6 | PageRank differential vs networkx | P1 | TODO | - | - |
| B6-* | 6 | Algorithms vs networkx; spam farms benchmark | P1 | TODO | - | - |
| B6-* | 6 | Cache invalidation properties; worker lifecycle | P1 | TODO | - | - |
| B7-* | 7 | 200+ case adversarial corpus committed | P0 | TODO | - | - |
| B7-* | 7 | llm_allowed:false never reaches provider (capture proof) | P0 | TODO | - | - |
| B7-* | 7 | Citation forgery false-accept/reject rates | P1 | TODO | - | - |
| B7-* | 7 | Refuse-gate honest calibration, held-out only | P1 | TODO | - | - |
| B7-* | 7 | Budgets/sessions isolation/mutation-kill | P1 | TODO | - | - |
| B7-* | 7 | SSE lifecycle, provider failure modes, no key/body leaks | P1 | TODO | - | - |
| B7-* | 7 | Ops: kill switch, env docs both directions | P1 | TODO | - | - |

## Phase C — Cross-Cutting Hardening

| ID | Area | Item | Severity | Status | Evidence/Test | Commit |
|----|------|------|----------|--------|---------------|--------|
| C-sec-* | security | STRIDE threat model per phase (SECURITY.md) | P1 | TODO | - | - |
| C-sec-* | security | Route table from app object; guard test for new routes | P1 | TODO | - | - |
| C-sec-* | security | Authn/authz audit (constant-time, scopes, brute force) | P1 | TODO | - | - |
| C-sec-* | security | Input limits, CORS, headers, log injection, error leaks | P1 | TODO | - | - |
| C-sec-* | security | ZAP/schemathesis API fuzz; fix 5xx/contract violations | P1 | TODO | - | - |
| C-sec-* | security | Supply chain: pins, hashes, image scan, non-root | P1 | TODO | - | - |
| C-data-* | data | kill -9 at critical sections + integrity checks | P1 | TODO | - | - |
| C-data-* | data | Migrations on populated DBs; backup/restore executed | P1 | TODO | - | - |
| C-data-* | data | Clock skew/TZ daily budget rollover | P2 | TODO | - | - |
| C-perf-* | perf | Profile hot paths; before/after numbers | P2 | TODO | - | - |
| C-perf-* | perf | Load tests p50/p95/p99; soak for leaks | P2 | TODO | - | - |
| C-obs-* | ops | /health vs /ready; config fail-fast; RUNBOOK executed | P2 | TODO | - | - |
| C-dx-* | dx | Fresh-clone test from README alone; CI pipeline; env-consistency script | P1 | TODO | - | - |
| C-doc-* | docs | All docs verified vs code by scripts in CI | P2 | TODO | - | - |

## Phase D — Roadmap-Admitted Gaps

| ID | Item | Severity | Status | Evidence/Test | Commit |
|----|------|----------|--------|---------------|--------|
| D-1 | Real embeddings benchmark + tested path | P2 | TODO | - | - |
| D-2 | LTR/click data path or documented gate | P2 | TODO | - | - |
| D-3 | Language support safe degradation tests | P2 | TODO | - | - |
| D-4 | Registrable-domain spam option (benchmarked) | P2 | TODO | - | - |
| D-5 | Sessions: production default decision + upgrade tests | P2 | TODO | - | - |
| D-6 | Evaluated production features (reindex, erasure, cursors, ...) | P2 | TODO | - | - |

## Phase E — Convergence

| ID | Round | Result | Status |
|----|-------|--------|--------|
| E-R1 | 1 | - | INPROG |
| E-R2 | 2 | - | TODO |
| E-R3 | 3 | - | TODO |
