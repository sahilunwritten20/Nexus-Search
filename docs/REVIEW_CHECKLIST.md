# Review Checklist — review/hardening

One row per audit item. Status: TODO / INPROG / FIXED / VERIFIED / ACCEPTED(trigger).
Severity: P0/P1/P2/P3. Evidence = test name, command, or file:line.

## Phase A — Baseline & Truth

| ID | Area | Item | Severity | Status | Evidence/Test | Commit |
|----|------|------|----------|--------|---------------|--------|
| A1-1 | map | Full tree + line counts + module graph | P2 | DONE | worklog S1.0/S1.3 | d7755cd |
| A1-2 | map | docs/ARCHITECTURE.md written from code | P2 | DONE | file committed | 21e09a7 |
| A1-3 | map | Architecture diffed vs doc claims; discrepancies logged | P2 | INPROG | claims ledger; README env row fixed | b01f623 |
| A2-1 | baseline | Full suite x3 normal, record counts/runtime | P1 | DONE | worklog S1.1 (3 normal runs identical) | ff1f7b1 |
| A2-2 | baseline | Suite x3 random order; flakiness found+fixed | P1 | DONE | 3 random runs green; ORDER-1 root-caused+fixed (conftest baseline) | 7c08c77 |
| A2-3 | baseline | Suite x1 xdist parallel | P1 | DONE | run7 1168 passed | ff1f7b1 |
| A2-4 | baseline | Clean-venv install from lockfile | P1 | DONE | fresh venv from requirements*.txt -> pip check clean, imports OK | - |
| A3-1 | counts | True test counts per phase 1-7 + file; fix ROADMAP | P2 | DONE | README truth pass; per-dir counts | b01f623 |
| A4-1 | coverage | Per-module line+branch coverage; list <85% modules | P2 | DONE | Full-suite canonical coverage in docs/CANONICAL_COVERAGE.md; 90% line / 89% branch overall | 7c344a5 |
| A4-2 | coverage | Untested public functions, dead except blocks, pragmas, env-gated tests | P2 | DONE | Core 93%/91% (excl. evaluation); security-critical table in CANONICAL_COVERAGE.md | 7c344a5 |
| A5-1 | static | ruff strict baseline | P2 | DONE (baseline) | ~800 findings, no config yet; curated config = Phase C | - |
| A5-2 | static | format check | P2 | DONE (baseline) | 1 file (BOM in __init__.py) | - |
| A5-3 | static | mypy strict-ish baseline | P2 | PARTIAL | 155 errors w/o config; config+triage pending | - |
| A5-4 | static | vulture dead code, radon complexity | P2 | DONE (baseline) | vulture: unused response params/imports | - |
| A5-5 | static | pip-audit/safety CVEs, bandit, secret scan | P1 | PARTIAL | pip-audit CLEAN; bandit 21 triaged-in-worklog; git-history secret scan PENDING | - |
| A5-6 | static | pip-licenses conflicts | P3 | TODO | - | - |
| A5-7 | static | Justify or remove every suppression comment | P2 | TODO | 94 RUF100 unused-noqa found; audit pending | - |
| A6-1 | live | Boot service; probe /health /ready /metrics /openapi.json | P1 | DONE | worklog S1.7 (full probe table) | - |
| A6-2 | live | Curl every route: valid/invalid/oversized/empty/unicode/unauth | P1 | DONE | 60+ probes, all per contract; INF-WEIGHT found+fixed | 6bc2e08 |
| A6-3 | live | OpenAPI matches behavior | P2 | PARTIAL | /openapi.json disabled in production (live 404); schema-conformance fuzzing (schemathesis) 900 req/15 ops/0 5xx | 06b60f7 |
| A7-1 | testquality | Weak/duplicate/mock-only/stateful/sleepy tests identified | P2 | DONE | ORDER-1 class fixed; no flakes in 2x randomized runs | 7c08c77 |
| A7-2 | mutation | Mutation testing on high-risk modules, score recorded | P2 | NOT DONE | mutmut blocked on Windows; manual checks only; Linux CI needed | - |
| A8-1 | coverage-excl | Evaluation modules excluded from overall gate (ADR-003) | P2 | DONE | docs/COVERAGE_EXCLUSION_ADR.md; core 93/91 meets gate | 7c344a5 |

## Findings register (this branch)

| ID | Severity | Description | Status | Fix commit | Regression test |
|----|----------|-------------|--------|------------|-----------------|
| SSRF-PIN-1 | P1 | pin_dns_for_url nullcontext fallback let mixed-record rebinding DNS connect to private IPs (robots fetch path too) | FIXED | 7c08c77 | test_dns_pinning::test_pin_validation_failure_never_falls_back_to_live_dns + test_security_edges::test_unvalidatable_host_blocks_entry |
| STORAGE-1 | P2 | rag_session_prune deleted by bare session_id across scopes + oldest-turn staleness | FIXED | 7c08c77 | test_sessions::test_prune_never_deletes_another_scopes_fresh_session + test_prune_uses_session_last_activity_not_oldest_turn |
| ORDER-1 | P2 | Suite order-dependence via leaked env; teardown reloads opened repo-root nexus_search.db | FIXED | 7c08c77 | isolated single-file runs (conftest.py baseline + tests/_api_reset.py) |
| UTC-1 | P2 | Budget/metrics day keys localtime vs documented UTC-day contract | FIXED | f893ef9 | test_storage::test_day_key_is_utc_not_local |
| STREAM-1 | P2 | Unbounded SSE accumulation (pipeline gap-watchdog + adapter buffering) | FIXED | f893ef9 | test_pipeline::TestStreamIsBounded + test_provider_http::test_streaming_far_beyond_budget_is_cut_not_buffered |
| ROBOTS-GAP | P2 | Redirect hops never robots/blocklist/allowed-domain checked | FIXED | 40741cf | tests/crawler/test_redirect_robots.py (4 tests) |
| INF-WEIGHT | P3 | bm25_weight=inf accepted; scores degenerate to null | FIXED | 6bc2e08 | test_search_dos::TestNonFiniteWeights (3 tests) |
| R-1/R-2/R-30/R-34 | P3 | Stale README counts + wrong env default row | FIXED | b01f623 | README truth pass |
| METRICS-NOLIMIT | P3 | /metrics has no rate limiter | FIXED | 60c221f | test_api::TestApiRateLimit::test_metrics_is_limited_too |
| ZIP-TXT | P3 | zip renamed .txt indexed as garbage (bypasses binary refusal) | FIXED | 60c221f | test_files_connector::TestZipRenamedTxtIsRefused (2 tests) |
| SUGGEST-LOCK | P3 | Suggester._from_log bypasses ExperimentLog.lock | FIXED | 60c221f | test_suggestions::TestRelatedSearchesLockDiscipline (GuardedLog double) |
| POLITENESS-MONO | P3 | Politeness pacing uses wall clock not monotonic | FIXED | 60c221f | test_politeness::test_crawl_delay_survives_forward_wall_clock_skew + test_reserve_slot_survives... |
| ASK-VS-STREAM | P3 | ask() prompts raw query, ask_streaming() prompted condensed | FIXED | 55da15c | test_sessions::TestFollowups::test_ask_and_stream_prompt_the_same_question |
| NDCG-EMPTY | P3 (effects P2) | ndcg_at_k returned 1.0 for zero-relevant queries — inflated ALL published aggregate NDCG by no-answer free 1.0s | FIXED | 9620f14 | test_benchmark::test_ndcg_with_no_relevant_docs_is_zero_not_perfect; floors re-derived |
| SESSION-DEADCHECK | P3 | SqliteSessionStore.get dead first TTL check (last_used just set) | FIXED | 60c221f | existing TTL tests (test_sqlite_store_roundtrip_and_ttl_prune) |
| ROBOTS-WILDCARD | P2 | stdlib robotparser lacks RFC 9309 * / $ wildcards + longest-match (documented) | OPEN — B3 decision: implement RFC 9309 matcher (no new deps) | - | - |
| COVERAGE-GATE | P2 | Full-suite: 90% line / 89% branch overall. Core (excl. evaluation): 93% line / 91% branch ✅ 92/85 gate. Security-critical (97/90 floor): 9/13 MET (storage 99%, security 98%, sanitize 99%, egress 100%, gate 97%, retrieval 97%, fetcher 99%, url_utils 100%, models 97%); 5 NOT MET: api 86/81, session 96/95, files 88/93, calibration 90/90, __main__ 94/90 | INPROG | docs/CANONICAL_COVERAGE.md |
| NOQA-DEBT | P2 | 94 unused noqa (RUF100) + 57 unused imports + BOM file | OPEN (A5-7) | - | ruff baseline S1.4 |
| EGRESS-COV | P1 | egress.py lifted from 76% → 100% line, 100% branch | FIXED | abd45bc | tests/rag/test_egress.py (TestLLMAdded) |
| SANITIZE-COV | P1 | sanitize.py lifted from 70% → 100% line, 100% branch | FIXED | abd45bc | tests/rag/test_sanitize_scrubber.py |
| STORAGE-COV | P1 | storage.py lifted from 57% → 98% line | FIXED | abd45bc | tests/core/test_storage_coverage.py |
| SESSION-COV | P1 | session.py lifted from 77% → 96% line, 100% branch | FIXED | abd45bc | tests/rag/test_session_coverage.py |
| FILES-COV | P1 | files.py lifted from 70% → 87% line, 93% branch | INPROG | - | tests/ingestion/test_files_coverage.py |
| FETCHER-COV | P1 | fetcher.py lifted from 68% → 98% line, 97% branch | FIXED | abd45bc | tests/crawler/test_fetcher_coverage.py |
| API-COV | P1 | api.py at 55% line, needs 92% | NOT STARTED | - | - |
| EXFIL-PARTIAL | P2 | Unterminated markdown link/image forms survived sanitization (fuzz-found) | FIXED | c299665 | test_sanitize_fuzz (600 payload soups, zero channels) |
| PR-CONVERGE | P2 | PageRank 30-iter cap shipped non-converged scores on hub-heavy graphs | FIXED | 8b9f76b | test_pagerank_differential (8 shapes vs networkx) |
| RETRY-AFTER | P3 | Budget 429 Retry-After flat 3600 under-stated resets by up to 23h | FIXED | f7de1c8 | test_budget_rollover (seconds to UTC midnight) |
| REDTEAM-FILTERS | P2 | Unknown filter keys on /ask escaped as 500 with internals | FIXED | e277d54 | test_red_team (clean 400 on /ask + /ask/stream) |
| SKEP-1 | P1 | Robots matcher regex backtracking hung process (attacker robots.txt '/a*a*...*b') | FIXED | 5afb8e7 | test_skeptic_round2 (linear two-pointer glob) |
| SKEP-2 | P2 | Crawl-delay unvalidated (3-year sleeps, NaN/-5 disabled pacing) | FIXED | 5afb8e7 | test_skeptic_round2 (clamp [0,60]) |
| SKEP-3 | P2 | Redirect hops bypassed target-domain pacing | FIXED | 5afb8e7 | test_skeptic_round2 (hop pacing) |
| SKEP-4 | P3 | Sitemap index children + loc URLs skipped blocklist | FIXED | 5afb8e7 | test_skeptic_round2 (blocklist gate) |
| SKEP-5 | P3 | Pinned hosts fell back to REAL DNS for service-name ports | FIXED | 5afb8e7 | test_skeptic_round2 (fail closed on gaierror) |
| SKEP-R1 | P1 | /ask/stream token events bypassed scrub_leaks AND moderator (system prompt + nonce leaked) | FIXED | 81b78d7 | test_skeptic_rag_round2 (StreamLeakScrubber) |
| SKEP-R2 | P1 | Real adapters buffered entire SSE body before any cap (requests.post without stream=True) | FIXED | 81b78d7 | test_skeptic_rag_round2 (lazy reader threads) |
| SKEP-R3 | P2 | worker.join(timeout) bounded whole buffered parse; zombie workers held slots | FIXED | 81b78d7 | test_skeptic_rag_round2 (reader threads) |
| SKEP-R4 | P2 | CommonMark reference-definition links weaponized citation markers | FIXED | 81b78d7 | test_skeptic_rag_round2 (ref-def + full-ref regexes) |
| SKEP-R5 | P2 | Disconnects settled nothing; condense spend unbilled on streams | FIXED | 81b78d7 | test_skeptic_rag_round2 (disconnect settles + condense) |
| SKEP-R6..R13 | P3 | Session-turn parity, filter validation, removal counts, TTL parity, separator collision, corpus determinism, stream_limit honesty | FIXED | 81b78d7 | test_skeptic_rag_round2 (14 tests) |

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
| E-R1 | 1 | 17 findings fixed (1xP1 SSRF pin bypass, 7xP2 incl. 3 found by new differentials/fuzz, 9xP3 + docs truth pass); differentials BM25/PageRank/fusion/vector green; fuzz parser/tokenizer/sanitize green (1 fuzz-found P2 fixed); baselines closed (clean-venv, pip-audit, secrets, licenses); randomized suite 1259/13 twice. Interim verdicts in docs/REVIEW_REPORT.md | DONE |
| E-R2 | 2 | 11 findings fixed (3xP1 crawler skeptic, 8xP2/P3 RAG skeptic); schemathesis 900 req/15 ops/0 5xx; spam-farm decision; load smoke; fresh-clone; SECURITY/RUNBOOK; kill-9 chaos; migration upgrades; UTC rollover; 201-case red team; 1323 passed x2 randomized. | DONE |
| E-R3 | 3 | Coverage lift in progress: egress 100%, sanitize 100%, storage 98%, session 96%, fetcher 98%, files 87%, api 55%. Refuse-gate calibration, Round-3 skeptic, final seal pending. | INPROG |

Final round-1 suite evidence: two randomized full runs, both
"1259 passed, 13 skipped, 641 subtests" (logs regress_round1{,b}.txt in
the opencode temp dir; recorded 2026-10-14).
