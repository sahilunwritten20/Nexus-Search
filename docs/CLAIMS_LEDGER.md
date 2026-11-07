# Claims Ledger — review/hardening

Every claim from ROADMAP/README/SPEC/docs/code comments/.env.example.
Result: TRUE / FALSE / PARTIAL / UNVERIFIABLE. FALSE|PARTIAL => finding ID in REVIEW_CHECKLIST.md.

Legend: source = file:line. verify = command/test that produced the evidence.

## README.md

| # | Claim | Source | How verified | Result | Evidence |
|---|-------|--------|--------------|--------|----------|
| R-1 | "844 passed, 7 skipped, 614 subtests" offline | README.md:165 | 7 full pytest runs (3 normal, 3 random-seed, 1 xdist) | FALSE (stale) | real: 1168 passed/13 skipped/641 subtests at review start; 1171+ after fix batch 1; worklog S1.1 |
| R-2 | "849 passed, 2 skipped" with NEXUS_RUN_MODEL_TESTS=1 | README.md:168 | model-gated full run | FALSE (stale) | real: 1176 passed/5 skipped (MiniLM cached); worklog S1.3 |
| R-3 | NEXUS_ENV != dev refuses boot without NEXUS_API_KEY | README.md:43 | live boot probe + TestApiBootPolicy | TRUE | live RuntimeError output in worklog S1.7 (A6) |
| R-4 | Auth required POST/DELETE /documents; 401 without key | README.md:44 | live curl probes (401/201/200 matrix) | TRUE | probe table in worklog S1.7 |
| R-5 | GET /search bounds: 2000 chars, 128 words -> 400 | README.md:94-96 | live probes (2500 chars -> 400 "q too long"; 130 words -> 400) | TRUE | worklog S1.7 |
| R-6 | /metrics key-gated | README.md:93 | live: 401 without key, 200 with key | TRUE | worklog S1.7 |
| R-7 | Rate limit 60/minute default on /search + writes | README.md:49 | code read (api.py:365) + existing TestApiRateLimit; /metrics NOT limited (finding METRICS-NOLIMIT) | PARTIAL | worklog S1.7 |
| R-8 | Reindex --shadow zero-downtime | README.md:101-103 | code read (reindex.py) + test_reindex.py | PENDING | phase B1 re-verify |
| R-9 | Backups: consistent snapshot under WAL | README.md:122-124 | code read (backup.py, sqlite backup API) + test_backup_cli | PENDING | execute in C-data |
| R-10 | Embedder default hash:384 (code+compose+table agree) | README.md:178-183 | code default + /health live ("name":"hash:384") | TRUE | live /health output S1.7 |
| R-11 | Stub outside dev requires NEXUS_RAG_ALLOW_STUB=1 | README.md:79 | live /ask -> 503 "rag subsystem absent" (reason=stub_requires_opt_in on /health) | TRUE | worklog S1.7 |
| R-12 | NEXUS_RAG_ENABLED=0 -> /ask 503, /health rag disabled | README.md:78 | code read + test_ask_api | PENDING | kill-switch test exists; re-run in B7 |
| R-13 | LLM_TIMEOUT clamped <=30s | README.md:74 | code read (config.py:124 HARD_TIMEOUT_CEILING) | TRUE | static read; unit test pending |
| R-14 | Retries: connect/429/5xx only; read timeouts fail fast | README.md:75 | code read (provider.py _post_with_retries) + test_provider_http retry tests | TRUE | static read + 43 provider tests pass |
| R-15 | Concurrency semaphore -> 429 + Retry-After | README.md:76 | code read (_SlotMixin) + tests | PENDING | B7 verify |
| R-16 | Price table unset -> cost null | README.md:77 | code read (estimate_cost None path) | PENDING | B7 |
| R-17 | Context budgets never exceeded; drops reported | README.md:81 | context.py invariants + property tests pending | PENDING | B7 property tests |
| R-18 | llm_allowed:false never reaches provider | README.md:543 | egress test (SpyProvider) passes; capture-based test exists | TRUE | test_pipeline egress suite green |
| R-19 | Gate: 83.3% unanswerable refusal @ 5.9% false refusal held-out | README.md:554-558 | rerun calibration | PENDING | B7 |
| R-20 | CI floors: invented citations 0, injection 0, exfil 0, leak 0 | README.md:582-585 | rag_benchmark run | PENDING | B7 |
| R-21 | Sessions cross-key isolation mutation-killed | README.md:576-577 | cross-key test green; mutation pass pending | PARTIAL | test green (S1.5 run) |
| R-22 | Budgets hold across 2 workers (shared SQLite) | README.md:571-572 | two-instance test in test_sessions | PENDING | B7 |
| R-23 | Background PageRank worker clean shutdown | README.md:513-514 | code read; test pending | PENDING | B6 |
| R-24 | Graph benchmark 10k/100k recompute 3.2s vs 30s bound | README.md:517-518 | env-gated benchmark run pending | PENDING | B6 full run |
| R-25 | Authority weight default 0.0 inert (zero-weight identity pinned) | README.md:47,490-491 | test_ranker zero-weight identity | PENDING | B5/B6 |
| R-26 | DNS pinning: pinned IP is the one connected to | README.md:65 | test_rebinding_is_defeated + NEW pin-failure test | TRUE (post-fix) | worklog S1.4 — the pre-fix pin-fallback hole is FIXED; tests green |
| R-27 | .env.example documents all vars (lint-enforced, 61 knobs) | README.md:591 | scripts/dev/check_env_docs.py: "env docs consistent: 61 documented vars" | TRUE | run output S1.7 |
| R-28 | Docker image non-root, model baked to /opt/hf | README.md:148-150 | Dockerfile read/build pending | PENDING | C-sec |
| R-29 | Phase 6 "every bullet matches shipped code" | README.md:466-468 | per-bullet trace pending | PENDING | B6 |
| R-30 | Per-file test counts (Phase 3/4/5 sections) | README.md:326-453 | pytest --collect-only per file | FALSE (stale) | e.g. Frontier claimed 11 -> 16 now; Fetcher 4 -> 14; correction in A3 |
| R-31 | "tests/golden/keyword_baseline.json unchanged by allowlist" | README.md:105-108 | golden test in suite (green) | TRUE | suite run S1.1 |
| R-32 | Requirements split: runtime never imports dev deps | requirements.txt:34-36 | grep imports pending | PENDING | A5 verify |
| R-33 | networkx in venv but not in requirements* | venv pip list | grep usage: only dev scripts/tests | PARTIAL | not a runtime dep; document |
| R-34 | NEXUS_MAX_DECOMPRESSED_BYTES default 536870912 | README.md:67 (pre-fix) | code default 67108864; .env.example agrees with code | FALSE (README stale) | FIXED in A3; files.py:24 |
| R-35 | /docs, /openapi.json hidden in production | README via code comment api.py:81-88 | live probes: both 404 in production | TRUE | worklog S1.7 |
| R-36 | /ask unconditionally key-gated even with reads open | api.py:937-938 | live probe: 401 without key | TRUE | probe table S1.7 |

## Live-probe findings from A6 (worklog S1.7)
- [P3 INF-WEIGHT] `bm25_weight=inf` accepted (200); pydantic nulls the
  non-finite scores — ranking silently degrades to tie-break order and
  scores misreport as null. Should be a 400 (reject non-finite weights).
- [P3 METRICS-NOLIMIT] /metrics carries no rate limiter (every other
  key-gated route does).

## SPEC.md / docs/*.md / code comments

(to be populated as files are read — each doc read appends rows)

## .env.example

(to be populated by the env-consistency script diff)

## Finding-Count Reconciliation

| Source | Round | Claimed | Verified | Discrepancy |
|--------|-------|---------|----------|-------------|
| Worklog S1.4 | 1 | 17 fixed (1 P1, 7 P2, 9 P3) | 17 in REVIEW_REPORT.md | — |
| Worklog S1.16 | 2 (skeptic) | 8 crawler + 13 RAG = 21 | 21 in REVIEW_REPORT.md | — |
| Worklog S1.17 | 2 (seal) | 28 total (3 P1, 11 P2, 14 P3) | 28 in CLAIMS_LEDGER | — |
| REVIEW_CHECKLIST.md findings table | — | 18 entries (rows 37-54) | 18 entries | **10 findings missing from checklist** |
| Git log fix commits | — | Count from commits | da78e0f message: "28 findings" | — |

### Missing 10 Findings from Checklist (rows 37-54 only show 18)

| Finding ID | Round | Severity | Description | Fix Commit | Regression Test |
|------------|-------|----------|-------------|------------|-----------------|
| EXFIL-PARTIAL | 1 (fuzz) | P2 | Unterminated markdown link/image forms survived sanitization | c299665 | test_sanitize_fuzz (600 payload soups) |
| PR-CONVERGE | 1 (diff) | P2 | PageRank 30-iter cap shipped non-converged scores | 8b9f76b | test_pagerank_differential (8 shapes vs networkx) |
| RETRY-AFTER | 2 (utc) | P3 | Budget 429 Retry-After understated resets by up to 23h | f7de1c8 | test_budget_rollover (UTC midnight) |
| REDTEAM-FILTERS | 2 (red team) | P2 | Unknown filter keys on /ask escaped as 500 | e277d54 | test_red_team (clean 400) |
| SKEP-1 | 2 (crawler skeptic) | P1 | Robots matcher regex backtracking hung process | 5afb8e7 | test_skeptic_round2 (linear glob) |
| SKEP-2 | 2 (crawler skeptic) | P2 | Crawl-delay unvalidated (3-year sleeps, NaN disables) | 5afb8e7 | test_skeptic_round2 (clamp [0,60]) |
| SKEP-3 | 2 (crawler skeptic) | P2 | Redirect hops bypassed target-domain pacing | 5afb8e7 | test_skeptic_round2 (hop pacing) |
| SKEP-4 | 2 (crawler skeptic) | P3 | Sitemap index children skipped blocklist | 5afb8e7 | test_skeptic_round2 (blocklist gate) |
| SKEP-5 | 2 (crawler skeptic) | P3 | Pinned hosts fell back to real DNS for service ports | 5afb8e7 | test_skeptic_round2 (fail closed) |
| SKEP-R1 | 2 (RAG skeptic) | P1 | /ask/stream shipped system prompt + nonce | 81b78d7 | test_skeptic_rag_round2 (StreamLeakScrubber) |
| SKEP-R2 | 2 (RAG skeptic) | P1 | Real adapters buffered entire SSE body | 81b78d7 | test_skeptic_rag_round2 (lazy reader threads) |
| SKEP-R3 | 2 (RAG skeptic) | P2 | worker.join(timeout) bounded whole buffered parse | 81b78d7 | test_skeptic_rag_round2 (reader threads) |
| SKEP-R4 | 2 (RAG skeptic) | P2 | CommonMark reference-def links weaponized | 81b78d7 | test_skeptic_rag_round2 (ref-def regexes) |
| SKEP-R5 | 2 (RAG skeptic) | P2 | Disconnects settled nothing; condense spend unbilled | 81b78d7 | test_skeptic_rag_round2 (settle + condense) |
| SKEP-R6..R13 | 2 (RAG skeptic) | P3 | Various parity/validation fixes | 81b78d7 | test_skeptic_rag_round2 (14 tests) |

**Total**: 17 (Round 1) + 21 (Round 2 skeptic) = 38 individual findings, but some overlap. Claimed 28 unique findings fixed across rounds. Checklist must be updated to reflect all 28.
