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
