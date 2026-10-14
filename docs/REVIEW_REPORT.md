# Review Report — review/hardening, Round 1 (INTERIM)

Date: 2026-10-14. This is the ROUND-1 interim report of the hostile
review. The convergence loop (Phase E) is NOT complete — verdicts below
are per-phase round-1 verdicts, and the "Remaining" section is the
honest backlog a fresh session resumes from (see REVIEW_WORKLOG.md
S1.1-S1.14 and REVIEW_CHECKLIST.md for full evidence).

## Verdicts (round 1)

| Phase | Verdict | Justification (evidence in worklog) |
|---|---|---|
| 1 Search core | **Ready with caveats** | BM25 differential vs hand-derived reference at 1e-9 (incl. 40 random corpora); parser fuzz 600 inputs zero exceptions; tokenizer property suite; NDCG metric bug found+fixed (inflated published benchmarks); inf-weight 400. Caveats: coverage below gate (files.py-adjacent core at 88-95%), mutmut run not yet done. |
| 2 Ingestion | **Ready with caveats** | Zip-renamed-.txt garbage-indexing hole fixed; limits verified by existing suites; encoding matrix already strong (WP13). Caveats: files.py 81.3% coverage (OCR/edge paths unexercised in default CI), adversarial per-format corpus (billion-laughs variants, formula bombs) not yet run as a dedicated sweep. |
| 3 Crawler | **Ready with caveats** | SSRF pin-fallback P1 fixed (live private-IP round trip pre-fix); redirect hops now robots/blocklist/domain gated; RFC 9309 matcher owned (interpreter-version-independent, 18-case conformance); monotonic politeness clock. Caveats: DNS pinning TOCTOU now fails closed (design verified), render_js residual DNS-rebinding stays documented; full 50-worker politeness measurement + kill-9 crawl-resume suite not yet executed. |
| 4 Hybrid | **Ready with caveats** | Fusion monotonicity properties; vector top-k == brute force (exact, incl. filters); refuse-gate floors verified against corrected NDCG. Caveats: recall@10k/100k scale benchmarks not re-run this round; ST-arm benchmark re-run pending (floors re-derived under corrected metric). |
| 5 Ranking | **Ready with caveats** | Suggester lock discipline fixed; weight validation (NaN/inf) hardened; signals clamped/deterministic per existing suites. Caveats: adversarial query-set campaign + LTR placeholder zero-influence test pass not yet run. |
| 6 Links | **Ready with caveats** | PageRank differential vs networkx (8 shapes) caught a real truncation (30-iter cap raised to 100); reciprocal-discount deviation pinned as intentional. Caveats: populated-DB migration upgrade tests v1->v2->v3 not yet executed; spam-farm benchmark not yet run. |
| 7 RAG | **Ready with caveats** | llm_allowed egress, citation verification, prompt fencing verified by suites; stream bounds fixed (pipeline + adapter); unterminated-markdown exfil hole found by fuzz and fixed; sessions prune cross-scope deletion fixed; UTC day keys fixed. Caveats: the 200+ case red-team corpus is NOT yet built; refuse-gate improvement campaign not run; mutmut on citations/gate pending. |

## Findings fixed this round (17, all RED->GREEN->mutation-checked where applicable)

| ID | Sev | Description | Commit | Regression test |
|---|---|---|---|---|
| SSRF-PIN-1 | P1 | DNS pin fallback connected to private IPs under rebinding DNS | 7c08c77 | test_dns_pinning::test_pin_validation_failure_never_falls_back_to_live_dns |
| PR-CONVERGE | P2 | PageRank 30-iter cap shipped non-converged scores on hub-heavy graphs | 8b9f76b | test_pagerank_differential (8 shapes vs networkx) |
| ROBOTS-GAP | P2 | Redirect hops bypassed robots/blocklist/allowed-domains | 40741cf | test_redirect_robots (4 tests, Host-routing server) |
| ROBOTS-WILDCARD | P2 | robots.txt semantics depended on Python version (3.12 literal vs 3.14 wildcards) | 79e26f3 | test_robots_rfc9309 (18-case conformance) |
| STORAGE-1 | P2 | session prune deleted other scopes' fresh sessions (client-supplied id collision) | 7c08c77 | test_sessions::test_prune_never_deletes_another_scopes_fresh_session |
| ORDER-1 | P2 | suite order-dependence via leaked env; teardown reloads opened repo-root DB | 7c08c77 | conftest.py baseline + isolated single-file runs |
| UTC-1 | P2 | budget/metrics day keys localtime vs documented UTC contract | f893ef9 | test_storage::test_day_key_is_utc_not_local |
| STREAM-1 | P2 | unbounded SSE accumulation (pipeline + adapter buffering) | f893ef9 | TestStreamIsBounded + provider cap test |
| NDCG-EMPTY | P2-effects | no-relevant queries scored PERFECT 1.0 — inflated ALL published aggregates | 9620f14 | test_benchmark::test_ndcg_with_no_relevant_docs... + floors re-derived |
| EXFIL-PARTIAL | P2 | unterminated markdown link/image forms survived sanitization (fuzz-found) | (S1.14) | test_sanitize_fuzz (600 payload soups, zero channels) |
| INF-WEIGHT | P3 | bm25_weight=inf accepted (pydantic ge=0 admits +inf) | 6bc2e08 | TestNonFiniteWeights (3 tests) |
| ASK-VS-STREAM | P3 | /ask and /ask/stream prompted different question text | 55da15c | test_ask_and_stream_prompt_the_same_question |
| METRICS-NOLIMIT | P3 | /metrics had no rate limiter | 60c221f | test_metrics_is_limited_too |
| ZIP-TXT | P3 | generic zip under .txt indexed as mojibake garbage | 60c221f | TestZipRenamedTxtIsRefused |
| SUGGEST-LOCK | P3 | related-searches read query log connection without the lock | 60c221f | GuardedLog test double |
| POLITENESS-MONO | P3 | crawl pacing on wall clock (NTP skew collapsed delays) | 60c221f | 2 skew tests |
| SESSION-DEADCHECK | P3 | dead TTL check in SqliteSessionStore.get | 60c221f | covered by TTL suite |

Docs truth pass: README suite counts (stale 844/7 -> measured), env-table
default fixed (NEXUS_MAX_DECOMPRESSED_BYTES), per-file/per-phase counts,
Phase-3 known-limits row updated (commits b01f623, 35b2ed2).

## Before / after

| Measure | Before | After (round 1) |
|---|---|---|
| Full suite (offline) | 1168 passed / 13 skipped | 1233+ passed / 13 skipped (65 new review tests) |
| Order independence | by leaked env (fragile) | by design (conftest baseline); single-file runs pass |
| BM25 vs reference | never differential-tested | 1e-9 agreement, mutation-verified |
| PageRank vs networkx | never differential-tested; 30-iter cap | agreement at 5e-5, cap fixed |
| NDCG metric | +1.0 free points for unanswerable queries | 0.0 (floors re-derived, docs regenerated) |
| Secrets/CVEs/licenses | unverified | pip-audit clean; history pattern-sweep clean; no copyleft |
| Coverage | unmeasured | 88.2% line / 82.0% branch (gate: 92/85 — NOT met) |
| Clean venv install | unverified | pip check clean, imports OK |

## NOT verified this round (honest list — the resume backlog)

Round 2 (this session) closed the top items:
- ~~Populated-DB migration tests~~ — DONE (10 tests: storage v1/v3/v4-era
  upgrades with zero row loss, pre-runner baselining, frontier ancient
  shapes, link-graph backfills, legacy vector-table drop).
- ~~UTC rollover + Retry-After~~ — DONE (budget resets at UTC midnight,
  429 Retry-After states the real seconds; RETRY-AFTER fixed).
- ~~Chaos: kill-9 mid-write~~ — DONE (4 kill delays -> integrity ok,
  zero orphan postings/bare docs, recovery writes+searches work).
- ~~RAG red-team corpus~~ — DONE: 201 committed cases / 16 families, all
  green through the real service + API; found REDTEAM-FILTERS (P2:
  unknown filter keys -> 500 with internals; fixed to a clean 400).
- ~~SECURITY.md / RUNBOOK.md~~ — DONE (STRIDE matrix, 8 residual risks
  with removal paths; ops procedures executed or test-covered).
- ~~Fresh-clone test~~ — DONE (clone -> pip install -> README boot ->
  /health + /search 200).
- ~~Load smoke~~ — DONE (2 passed: /search + /ask + /ask/stream bounded).
- mutmut: BLOCKED natively on Windows (WSL-only binary; no Linux
  interpreter configured on this host). Compensating evidence: 14+
  manual mutation checks in the worklog. Run mutmut in Linux CI to close.

Still open for the NEXT session (in risk-reduction order):
1. **Coverage lift to gates** (92/85 overall — now ~89 after new tests;
   security-critical 97/90: storage 90.7, files 81.3 need work).
2. **Schemathesis** property-based API fuzzing against the OpenAPI.
3. **Soak test** (30-60 min) for memory/FD/thread leaks; full p50/p95/p99
   load curve at committed budgets.
4. **Spam-farm benchmark** + registrable-domain decision (Phase D).
5. **Refuse-gate improvement campaign** (train-split-only tuning).
6. **ST-arm benchmark re-run** under corrected NDCG + real-embedder
   floors recheck.
7. **Phase D features**: GDPR erasure, delete-by-URL cascade proof,
   API versioning, sticky-routing warning, ruff/mypy curated configs
   (the 800-baseline triage), noqa config wiring.
8. **Skeptic passes + convergence rounds 2-3** (mission minimum): fresh
   adversarial re-read of changed+unchanged code, re-run fuzzers with
   new seeds.

## Reproduce

All commands + outputs are in docs/REVIEW_WORKLOG.md (S1.0-S1.14) and
the temp review_runs logs; the suite line to reproduce everything:
`python -m pytest -q` (offline), `NEXUS_RUN_MODEL_TESTS=1 python -m
pytest -q` (model-gated arm). Differentials:
`python -m pytest tests/core/test_bm25_differential.py
tests/links/test_pagerank_differential.py tests/core/test_fusion_properties.py
tests/core/test_vector_recall.py -q`.
