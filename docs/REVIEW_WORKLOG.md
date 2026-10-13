# Review Worklog — review/hardening

Chronological log of the hostile engineering review. Every entry: date, action,
command(s), real output, decision. A fresh session must be able to resume from
this file + REVIEW_CHECKLIST.md + CLAIMS_LEDGER.md alone.

Conventions:
- `[E]` = evidence (command + output pasted or referenced)
- `[F]` = finding (gets an ID in REVIEW_CHECKLIST.md)
- `[D]` = decision with rationale
- PowerShell 5.1 / Windows, Python 3.14.7, repo at `C:\Users\Admin\Desktop\nexus search\nexus-search`

---

## 2026-10-13 — Session 1

### S1.0 Branch + environment established

```
[E] git checkout -b review/hardening   -> "Switched to a new branch 'review/hardening'"
[E] .venv\Scripts\python.exe --version -> Python 3.14.7
[E] pip list (key): fastapi 0.141.1, pytest 9.1.1, pytest-xdist 3.8.0, pytest-cov 7.1.0,
    httpx 0.28.1, sentence-transformers 6.1.0, torch 2.14.0, networkx 3.7 (NOTE: networkx
    is installed in the venv but is NOT in requirements.txt or requirements-dev.txt —
    no import found yet; if it's only used by dev scripts this is fine, else a finding.)
[E] pytest.ini: testpaths = tests (only)
[E] Repo tree mapped (full listing in git history of this commit). Source layout:
    nexus_search/{core,ingestion,crawler,ranking,links,rag,evaluation}
    tests/{core,crawler,evaluation,ingestion,links,rag,ranking,golden} + root tests
    Databases committed-ish in worktree (gitignored per .gitignore - verify):
    nexus_search.db, real_crawl.db, real_crawl_frontier.db, smoke.db, smoke_frontier.db
```

[D] Use existing .venv for the first baseline run (packages were installed from
the pinned requirements; pip freeze matches requirements.txt pins for the
runtime deps — spot-checked fastapi/uvicorn/pydantic/pypdf/requests versions).
A from-scratch clean-venv install is scheduled separately (A2 requirement).

Next: A2 baseline suite runs.

### S1.1 A2 baseline: full suite, 3x normal + 3x randomized + 1x xdist [E]

Foreword run + 6 background runs (review_baseline_runs.ps1 in temp opencode dir):

```
run1  normal      : 1168 passed, 13 skipped, 641 subtests passed in 439.02s
run2  normal      : 1168 passed, 13 skipped, 641 subtests passed in 462.60s
run3  normal      : 1168 passed, 13 skipped, 641 subtests passed in 451.91s
run4  random       : 1168 passed, 13 skipped, 641 subtests passed in 524.36s
run5  random       : 1168 passed, 13 skipped, 641 subtests passed in 413.08s
run6  random       : 1168 passed, 13 skipped, 641 subtests passed in 438.30s
run7  xdist -n auto: 1168 passed, 13 skipped, 641 subtests passed in 340.40s
```

- Plugin active: `randomly-5.0.0`, seed e.g. 695041316 printed with -v. Runs 4-6 genuinely randomized.
- Zero flakes at these seeds; no order-dependence; xdist-parallel green.
- pytest-randomly + hypothesis installed into the review venv (dev tools only, logged).

Skip inventory (the 13, via `-rs` on the gated files) [E]:
- tests/test_load_smoke.py:107,181 — NEXUS_RUN_LOAD_SMOKE (2)
- tests/evaluation/test_graph_benchmark.py:30 — NEXUS_RUN_GRAPH_BENCH (1)
- tests/evaluation/test_rag_benchmark.py:72 (ST arm), :103,112 (real-LLM, never CI) (3)
- tests/evaluation/test_semantic_benchmark.py:64,68 — ST floors (2)
- tests/rag/test_gate_calibration.py:99,102 — ST gate floors (2)
- tests/core/test_embedders.py:159,184,278 — ST model (3... listed)
Total 13 skips — ALL env-gated paths = UNVERIFIED in default CI (finding A4-2
in the making). Note tests/crawler/test_fetcher.py:133 skips only if
playwright IS installed (it is not -> path runs).

[F] CLAIMS R-1 (README.md:165 "844 passed, 7 skipped, 614 subtests offline") =
FALSE/stale: real numbers today are 1168 passed / 13 skipped / 641 subtests.
P3 doc finding; roadmap A3 will fix all counts.

[D] A2 clean-venv install + model-gated run scheduled after static analysis
(so pip-audit and the lockfile check inform each other).

### S1.2 A1 code reading in progress

Read in full so far (notes in review notes below): core/tokenizer.py,
core/bm25.py, core/query_parser.py, core/storage.py, core/filters.py,
core/normalize.py, core/indexer.py, core/models.py, core/migrations.py,
core/hybrid_search.py, core/embedders.py, core/vector_store.py,
core/api.py (1154 lines), core/embedding_sync.py, core/query_cache.py,
core/diversity.py, core/vector_maintenance.py, core/backup.py,
core/reindex.py.

Finding candidates so far (each needs RED test before any fix):
- [F?] STORAGE-1 (P2): rag_session_prune deletes by session_id IN (subquery)
  without scope pairing — a stale session under scope A deletes a FRESH
  same-named session under scope B (storage.py:427-431). Cross-tenant prune.
- [F?] STORAGE-2 (P3): _day_key uses time.localtime — daily RAG budgets are
  local-time keyed; multi-TZ worker fleets disagree on "today"
  (storage.py:347-348). Also clock-skew sensitive.
- [F?] API note: /metrics has no rate limiter (every other route does).
- [F?] models.py metadata: deep-nested JSON may raise RecursionError (not
  ValueError) in the json.dumps validator -> 500 instead of 4xx. Needs test.
- [F?] query_parser: `type:""` (empty quoted filter value) creates an empty-
  value filter (matches nothing) while word-form `type:` is a no-op —
  inconsistent (query_parser.py:210 vs 226). P3.
- [F?] api.py add_document catches sqlite3.Error only; RecursionError or
  MemoryError from pathological metadata would surface as 500 (bounded by
  the 100k serialized bound, so likely fine — verify).

None of these are FIXED yet — RED tests first (Phase B/E flow).

### S1.3 A1 complete: full source sweep + ARCHITECTURE.md

[E] Every module under nexus_search/ read in full this session (core 19 files,
ingestion 12, crawler 11, ranking 6, links 3, rag 16, evaluation 10 — fixture
files for semantic/rag benchmarks skimmed for structure, to be re-read in
their per-phase audits). docs/ARCHITECTURE.md written from code.

Additional finding candidates from the sweep (RED tests pending):
- [F?] SSRF-PIN-1 (P1): `pin_dns_for_url` returns nullcontext() when its
  own re-validation fails (security.py:166-176) — under adversarial
  round-robin DNS (one public A + one private A) the subsequent
  session.get performs a REAL unpinned lookup and can connect to the
  private IP. Redirect hops multiply retries. The fix direction: pin
  failure must BLOCK the fetch (FetchBlocked), never fall back to live
  DNS. Also mixed-record sets: resolve_validated_addresses returns None
  if ANY record is private (good) — but the *pinned* path only runs when
  validation passes; the failure mode is the hole.
- [F?] UTC-1 (P2): storage.py `_day_key` uses time.localtime while
  budget.py:23/52-53 and api.py:979 claim per-UTC-day budgets. Doc/code
  contradiction; multi-TZ fleets also disagree on "today".
- [F?] STREAM-1 (P2): ask_streaming enforces a per-token-GAP timeout but
  no TOTAL stream duration/size cap — a hostile OpenAI-compatible
  endpoint can trickle tokens forever, growing final_text_parts unboundedly.
- [F?] SESSION-DEAD-CHECK (P3): session.py:205-208 TTL check compares
  against `now - session.last_used` where last_used was JUST set to now
  (line 201) — dead code; the real check is line 209 (max(updated_at)).
- [F?] ASK-VS-STREAM (P3): pipeline.ask builds the prompt with the RAW
  query (line 224) but ask_streaming uses the CONDENSED standalone query
  (line 419) — /ask and /ask/stream show the model different question
  text on follow-ups. One of the two is wrong; ask() looks intended.
- [F?] NDCG-EMPTY (P3): evaluation/metrics.py ndcg_at_k returns 1.0 when
  a query has NO relevant docs (ideal_dcg==0) — a "perfect" score for an
  unanswerable query; will fail the differential test vs ranx.
- [F?] ROBOTS-GAP (P2): stdlib robotparser — no * / $ wildcards
  (documented); REDIRECT TARGETS are never robots-checked (fetcher follows
  hops; politeness only gates the original URL).
- [F?] METRICS-NOLIMIT (P3): /metrics is the only key-gated route without
  @limiter.limit.
- [F?] SUGGEST-LOCK (P3): Suggester._from_log reads log.conn directly,
  bypassing ExperimentLog.lock (thread-safety).
- [F?] ZIP-TXT (P3): a ZIP renamed .txt sniffs as application/zip ->
  _CONTENT_READERS miss -> falls to extension reader -> indexed as
  replacement-char garbage instead of refused (files.py:846-851).
- [F?] README env table NEXUS_MAX_DECOMPRESSED_BYTES=536870912 vs code
  67108864 (.env.example agrees with code) — stale doc row.
- [F?] POLITENESS-MONO (P3): reserve_slot/time_until_allowed use
  time.time() (wall clock) — clock skew can violate crawl-delay pacing;
  monotonic is the right clock.

Model-gated run (claim R-2): NEXUS_RUN_MODEL_TESTS=1 ->
1176 passed, 5 skipped, 641 subtests in 504s [E] — ST path green with
cached MiniLM; README's "849 passed, 2 skipped" stale (R-2 = FALSE,
numbers grew).

### S1.4 Convergence round 1 — first fixes (RED -> GREEN -> mutation)

#### SSRF-PIN-1 (P1) FIXED — pin fallback connected to private IPs
[E] RED: tests/crawler/test_dns_pinning.py::
    test_pin_validation_failure_never_falls_back_to_live_dns
  pre-fix output: "AssertionError: 200 is not None : fetch must be
  refused, not 200" — a REAL HTTP round-trip to a 127.0.0.1 listener
  succeeded through the nullcontext fallback (rebind DNS: validate_url
  lookup public, pin lookup private, connect lookup private).
[F] The old test tests/crawler/test_security_edges.py::
    test_unvalidatable_host_is_nullcontext asserted the fail-open
  behavior; corrected to test_unvalidatable_host_blocks_entry (asserts
  PinnedResolutionBlocked on entry).
[D] Fix: security.pin_dns_for_url returns _BlockedPin (raises
  PinnedResolutionBlocked on __enter__) when pin-time validation fails;
  fetcher catches it alongside FetchBlocked at all 4 refusal sites
  (fetch/fetch_bytes/fetch_robots/fetch_text). allow_private_hosts
  (dns_pin=None) unaffected; robots-fetch pin failure now fails CLOSED
  (unreachable -> domain disallowed for the run).
[E] Mutation: reverting the fix branch to nullcontext -> the new test
  fails (200 != None); restored -> 6/6 pass.
[E] Crawler suite: 131 passed post-fix.

#### STORAGE-1 (P2) FIXED — rag_session_prune cross-scope deletion
[E] RED: tests/rag/test_sessions.py::
    test_prune_never_deletes_another_scopes_fresh_session —
  pre-fix removed=3 (both scopes' rows deleted by bare session_id);
    test_prune_uses_session_last_activity_not_oldest_turn —
  pre-fix pruned a live session because ONE OLD TURN existed.
[D] Fix (storage.py:rag_session_prune): deletion keyed on
  (scope, session_id) PAIRS, staleness = MAX(updated_at) per pair
  (same rule the RAM store uses).

#### ORDER-1 (P2) FIXED — suite order-dependence via leaked env
[E] RED (reproduced on PRISTINE tree via git stash):
  `pytest tests/rag/test_sessions.py` alone -> 10 failed / 9 errors —
  tearDown's `importlib.reload(api)` raises RuntimeError when the
  restored env has no NEXUS_API_KEY, and _boot's first import executes
  the boot check under ambient env. Root cause: the whole suite passes
  only because tests/core/test_api.py:12 leaks `NEXUS_ENV=dev` into the
  process at module import; RAG/WP6 test teardowns also reload api with
  NEXUS_DB unset -> opens/creates ./nexus_search.db in the REPO ROOT
  (observed: cross-process DB races when two suite runs share the CWD;
  the worktree's nexus_search.db/smoke.db artifacts).
[D] Fix: tests/conftest.py sets an explicit session baseline
  (NEXUS_ENV=dev, NEXUS_EMBEDDER=hash:384, throwaway session NEXUS_DB)
  — setdefault so operator-provided values win; tests/_api_reset.py
  reset_api() reloads api under a boot-safe dev env + throwaway DB then
  restores env exactly; the 5 duplicated teardowns now call it.
[E] Post-fix isolated runs (fresh process, single files):
  test_sessions.py 14 passed; ask_api+ask_stream+egress 36 passed;
  test_wp6_hygiene.py 23 passed; test_api.py 56 passed.

#### A5 static-analysis baseline (first pass) [E]
- pip-audit: "No known vulnerabilities found" (pinned set, today's DB)
- bandit: 21 findings — B608x11 (f-string placeholder SQL: all
  parameterized VALUES; placeholders are '?'-join construction — safe,
  to be documented in SECURITY.md), B405 (sitemap.py imports the
  Element TYPE from stdlib but parses via defusedxml — false positive,
  needs a # nosec with justification), B311x6 (benchmarks, seeded
  Random — fine), B101/B110 (benchmark assert / CLI close cleanup).
- vulture: unused `response` params in api.py (slowapi decoration
  artifact), unused `statistics`/`io` imports, 1 redundant if.
- ruff (no config): ~800 findings, top: UP045(205) I001(115)
  RUF100-unused-noqa(94!) ISC004(60) F401(57) BLE001(26) F841(24)
  S110(9). A curated ruff config + triage is a Phase C task; RUF100
  interacts with the suppression-justification rule (A5-7).
- ruff format --check: nexus_search/__init__.py has a UTF-8 BOM
  (the P7 "mojibake hygiene" left one behind).
- mypy (no config): 155 errors. Strict config + real-error triage
  pending (A5-3).
- Full ruff/bandit/mypy/vulture outputs preserved in the opencode
  temp review_runs dir; key counts recorded here.

### S1.5 Convergence round 1 — UTC-1 + STREAM-1 (RED -> GREEN -> mutation)

#### UTC-1 (P2) FIXED — budget day keys were local time, docs say UTC
[E] RED: tests/core/test_storage.py::test_day_key_is_utc_not_local —
  mocked localtime (+14h / -10h views of the same instant) made
  pre-fix _day_key return the LOCAL calendar day; fails pre-fix.
[D] Fix: storage._day_key and rag/metrics._day_key now use time.gmtime
  (the contract budget.py's docstring + the 429 Retry-After comment
  already stated). One-time upgrade note: existing rag_token_usage rows
  carry local-day strings; "today" resets once on upgrade.
[E] Mutation: revert to localtime -> test fails; restored -> pass.

#### STREAM-1 (P2) FIXED — unbounded SSE accumulation
Two halves:
1. pipeline.ask_streaming: per-gap watchdog did not bound a trickling
   stream. Now: total-chars cap (2 * max_tokens * chars_per_token) and
   total-duration cap (10 * timeout); exceeded -> truncated event
   reason "stream_limit" + done (same shape as provider_timeout).
   [E] RED: tests/rag/test_pipeline.py::TestStreamIsBounded (firehose
   provider, 50k deltas; pre-fix streamed everything, no truncated).
   [E] Mutation: disabling the char-cap branch -> test fails; restored.
2. provider adapters: _parse_stream buffered the ENTIRE SSE body inside
   the worker thread (network-bound memory: a fast hostile endpoint can
   deliver far more than max_tokens within the join(timeout) window).
   Now: text cap (max_tokens*8 chars, floor 1024) aborts the read with
   ProviderError("provider_stream_too_large") (maps to the clean error
   event; nothing of the flood is buffered).
   [E] RED: tests/rag/test_provider_http.py::
     test_streaming_far_beyond_budget_is_cut_not_buffered (500 x 100-char
   deltas at max_tokens=32; pre-fix buffered all 50k chars).

[E] Regression after both: full rag+storage packages 239 passed/2 skipped;
  the background RANDOMIZED full-suite run with fix batch 1: 1171 passed,
  13 skipped (order-independence now by design, not by leaked env).
