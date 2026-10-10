# WP14 Worklog — Pre-Phase-7 hardening (running log)

Environment: Windows 11, PowerShell 5.1. Primary interpreter: **CPython 3.12.14**
(uv-managed) in a fresh venv at `%TEMP%\opencode\wp14-venv` (pinned
requirements + CPU torch via `PIP_EXTRA_INDEX_URL`, + pytest-cov). Second
interpreter for the parity diff: repo `.venv` CPython 3.14.7. Branch:
`wp14` (off `phase-7` @ 3c1564f = tag `v0.6.0-phases-1-6`). All runs from the
repo root with `NEXUS_ENV=dev NEXUS_EMBEDDER=hash:384` unless stated.
Docker: CLI 29.8.0 present; **engine down** (`npipe dockerDesktopLinuxEngine`
not found) — Docker verifications below are UNVERIFIED-locally unless a later
entry says otherwise.

## Step 0.1–0.2 — baselines (before ANY code change)

| Run | Command | Result |
|---|---|---|
| Offline suite (run 1) | `python -m pytest -q` | **1 failed, 843 passed, 7 skipped, 614 subtests** (432.83 s) — flake, see N1 |
| Offline suite (run 2) | same | **844 passed, 7 skipped, 1 warning, 614 subtests** (419.83 s) — matches the WP13 baseline |
| Model-gated | `NEXUS_RUN_MODEL_TESTS=1 python -m pytest -q` | **849 passed, 2 skipped, 614 subtests** (385.75 s) — matches |
| Load smoke | `NEXUS_RUN_LOAD_SMOKE=1 pytest tests/test_load_smoke.py` | **1 passed** (6.69 s) |
| Graph bench | `NEXUS_RUN_GRAPH_BENCH=1 pytest tests/evaluation` | **15 passed, 2 skipped, 3 subtests** (91.77 s) |
| Duplicate-test lint | `python scripts/dev/check_duplicate_tests.py` | `no duplicate test function names` |
| Env-docs lint | `python scripts/dev/check_env_docs.py` | `env docs consistent: 30 documented vars` |
| rerank_benchmark | `python -m nexus_search.evaluation.rerank_benchmark` | **runs, exit 0** — full metric table (hybrid vs hybrid+rerank: p@1 1.0/1.0, ndcg@10 0.9245/0.9227, latency 3.4/9.7 ms). H7's "may be broken" REFUTED; unreferenced/untested CONFIRMED (0% coverage) |
| Python 3.14 diff | `.venv\Scripts\python.exe -m pytest -q` | **844 passed, 7 skipped, 1471 warnings, 614 subtests** (401.31 s) — identical outcomes; only warning volume differs (upstream slowapi deprecation, 3.14-only) |
| Skip gates (offline) | grep of tests | 7 skips = 3× test_embedders (NEXUS_RUN_MODEL_TESTS), 1× test_graph_benchmark (NEXUS_RUN_GRAPH_BENCH), 2× test_semantic_benchmark (NEXUS_RUN_MODEL_TESTS), 1× test_load_smoke (NEXUS_RUN_LOAD_SMOKE). No xfail; no custom pytest markers anywhere; `pytest.ini` has only `testpaths` (no markers section — inert since no marks are used) |

N1 — **crawl-delay timing flake** (`tests/test_regressions.py::TestCrawler::test_crawl_delay_holds_with_many_workers`):
failed in suite run 1 (serial, machine under background load) and in the `-n auto`
run; **0/10 failures isolated** (2.72 s each); CI (serial, 17 green runs) never hit
it. Mechanism (read from the code): `reserve_slot` spaces slots exactly by the
delay; the server records arrival inside per-connection handler THREADS, and under
CPU contention a delayed handler start compresses recorded gaps below the 0.25 s
tolerance. The politeness implementation is correct (its component contract is
separately pinned by `TestPoliteness.test_reserve_slot_spaces_out_concurrent_callers`).
Classification: **P2-ops (test-environment flake, not a production defect)**. Every
candidate fix is a test rewrite (measure dispatch-side instead of server-side),
which the WP14 rules forbid without an owner decision → ledgered, options
presented in the final report.

## Step 0.3 — pytest audit

- **Coverage** (`--cov=nexus_search --cov-branch`, 844/7 green, 471.64 s):
  **total 87%**. Modules under 85% line coverage (triage in the Step 0 report):
  `core/backup.py` 41%, `core/query_parser.py` 84%, `core/vector_store.py` 84%,
  `crawler/cli.py` 60%, `crawler/fetcher.py` 83%, `crawler/pipeline.py` 84%,
  `crawler/security.py` 84%, `evaluation/authority_benchmark.py` 81%,
  `evaluation/benchmark.py` 83%, `evaluation/dataset.py` 53%,
  `evaluation/graph_benchmark.py` 75%, `evaluation/rerank_benchmark.py` **0%**,
  `evaluation/scale_benchmark.py` 44%, `evaluation/semantic_benchmark/__main__.py`
  **0%**, `evaluation/semantic_benchmark/runner.py` 77%,
  `ingestion/connectors/files.py` 81%, `ingestion/connectors/mime.py` 76%.
  `core/api.py` is 85% (at, not under, the bar).
- **Runs**: `-n auto` → 843+1 flake (N1 above), 318.76 s; durations top:
  semantic coverage-gate 221 s, embedder-unavailable 137 s, degraded boot 75.6 s
  (all under xdist contention on a 2-core machine). `-W error::ResourceWarning`
  → **844/7 clean** (no resource leaks). No SQLite lock errors in any serial run.
- **Repo-root hygiene**: the suite leaves NO new files in the repo root (all
  suites use tmp dirs; pre-existing untracked `*.db` artifacts date to 10/6 and
  earlier — the wp12 a1/a2 ones are H11, fixed in Item 6g).
- **Mutation spot-checks** (patch → targeted tests → revert; `git status` clean
  after): **7/7 KILLED** —
  `require_api_key` wrong-key acceptance KILLED; `_rate_limit_key` any-key-own-bucket
  KILLED; `_blocked_ip` never-blocks KILLED; `_pinned_getaddrinfo` pins-ignored
  KILLED; `Indexer.delete_document` chunk-cascade-removed KILLED;
  `_check_zip_payload` infinite-bounds KILLED; `read_file` size-guard-disabled
  KILLED. **No surviving mutant → no missing tests on these guards.**

## Step 0.4 — FastAPI audit (probe scripts in %TEMP%\opencode, repo untouched)

- **Boot matrix**: `NEXUS_ENV` unset + no key → **RuntimeError, exit 1** (exact
  message: "NEXUS_API_KEY is not set and NEXUS_ENV != 'dev' … Refusing to
  boot"); explicit `NEXUS_ENV=production` + no key → same. Dev boots. ✓
- **Route matrix** (TestClient, 3 boots):
  - dev (no key): all 13 routes open (writes 201/200, reads 200). ✓
  - production + key: writes/explain/metrics/graph 401/401/201|200
    (none/wrong/right); reads (`/search`, `/documents/{id}`, `/suggest`,
    `/related`) open 200; `/health`,`/ready` open. `/docs`,`/redoc`,
    `/openapi.json` → **404** in production, **200** in dev. ✓
  - `NEXUS_REQUIRE_AUTH_FOR_READS=1`: all reads 401/401/200; health/ready stay
    open (probe contract). ✓
- **Malformed/oversized inputs** (dev): q>2000 chars → 400; q>128 words → 400;
  boundary values (exactly 2000 chars / 128 words) → 200; empty q → 400; bad
  cursor → 400 `invalid cursor`; cursor `offset:99999` → clamped to 10 000 then
  **400 window contract** (offset+top_k 10010 > 1000) — correct, documented
  behavior; offset=1000&top_k=10 → 400 with the exact bound in the message;
  mode=bogus → 422; bulk 501 docs → 422 (max_length=500 enforced); bulk 500 →
  200 `{"indexed":500,...}`; invalid JSON body → 422; content > 10 MB → 422;
  doc_id > 512 chars → 422; metadata > 100k serialized → 422. ✓
- **Rate limit** (boot `NEXUS_RATE_LIMIT=3/minute`): req 1-3 → 200 with
  `X-RateLimit-Remaining` 2/1/0, `X-RateLimit-Limit: 3`; req 4-6 → **429** with
  `Retry-After: 60`. ✓
- **CORS**: absent by default (no ACAO header on any response); with
  `NEXUS_CORS_ORIGINS=https://app.example.com`: preflight OPTIONS → 200 with
  `Access-Control-Allow-Origin` exact-match + allowed methods/headers; actual
  GET carries ACAO. ✓
- **Response models**: `/search` body validates against `SearchResponse`;
  `/documents/{id}` 200 validates against `DocumentOut` (404 bodies are the
  documented error shape). Note: `POST /documents`, `/documents/bulk`,
  `DELETE`, `/suggest`, `/related`, `/graph/*`, `/health`, `/ready`, `/metrics`
  declare NO response_model (P3, ledgered).
- **OpenAPI (dev)**: all 16 `/search` query params + all 12 routes present. ✓
- **Secrets**: `/health` and `/metrics` bodies contain no configured key. Error
  bodies (400/404/422 probes) contain no tracebacks/paths/internal state. ✓
- **Concurrency** (live uvicorn, production+key): 50 concurrent `/search` +
  5 writes → **55/55 non-5xx** (50×200 + 5×201), 0 exceptions, 0 SQLite lock
  errors in server log, 1.28 s wall. ✓

## Step 0.5 — security and secrets sweep

- `git ls-files`: **no** `.env`, `*.db`, `*.db-wal/-shm`, `*.pyc`, `*.swp`
  tracked. `.gitignore` covers `.env*` (with `!.env.example`), `*.db*`,
  `*.swp/swo`, `__pycache__/`, `seeds.txt`, logs. ✓
- Grep for `api[_-]?key|secret|token|password|bearer` across
  `nexus_search/ scripts/ automation/ docs/ .env.example`: every hit is either
  legit auth plumbing (`os.environ.get("NEXUS_API_KEY")`, constant-time
  compare, hashed bucket keys), documentation, or the CI probe key
  `ci-secret` in `.github/workflows/test.yml` (an ephemeral value for the
  container boot probe, not a real secret). **No hardcoded secrets.** ✓
- `ingestion/connectors/files.py`: no remote-input path exists (the connector
  takes operator-supplied roots; `read_file`/`iter_files` are local-tool
  surface). Symlink-escape probe **UNVERIFIED locally** — Windows symlink
  creation needs a privilege this account lacks (`WinError 1314`); zip handling
  guarded (mutation-killed) and covered by `TestDecompressionBombGuard`. ✓
- `crawler/fetcher.py`: redirect hops re-validated (regression test kills the
  mutant class); body size cap + max_bytes refusal tested
  (`test_oversized_body_is_rejected`); time limits documented in fetcher. ✓

## Step 0.6 — hypotheses H1–H13 (verdicts; evidence above / in items)

| H | Verdict | Evidence |
|---|---|---|
| H1 min_score unreachable | **CONFIRMED** (reading; failing test written test-first in Item 1) | `VectorStore.search(min_score=...)` exists (vector_store.py:384,440); `_vector_candidates` calls `search()` without it; `search_page` has no such param; `/search` has none |
| H2 /search monolith | **CONFIRMED** | api.py:404–607, one ~204-line function |
| H3 baked model unreachable in runtime image | **CONFIRMED** (code); docker boot UNVERIFIED-locally (engine down) | builder downloads to root's `~/.cache/huggingface`; runtime copies only `/opt/venv`; no `HF_HOME`; runs as appuser |
| H4 compose passes 6 of ~31 NEXUS_* | **CONFIRMED** (6 entries: DB, API_KEY, ENV, CACHE_TTL, RATE_LIMIT, EMBEDDER) | docker-compose.yml |
| H5 checklist names missing files | **CONFIRMED** | `tests/ranking/test_ab.py`, `test_query_parser.py`, `test_hybrid*.py` do not exist; robots tests live in `tests/crawler/test_politeness.py` (no `robots` file); A/B + purge ARE tested in `test_ranker.py` |
| H6 version drift / packaging | **CONFIRMED** | `version="0.4.0"` (api.py:73) vs tag v0.6.0; `__init__.py` empty; no pyproject.toml; pytest/pytest-xdist/httpx in production `requirements.txt` (§Testing) |
| H7 rerank_benchmark orphan | **CONFIRMED** (unreferenced, 0% cov) / **REFUTED** ("may be broken" — it runs, exit 0) | Step 0.2 run |
| H8 env-docs drift | **PARTIAL** | README missing: NEXUS_CORS_ORIGINS, NEXUS_MAX_ENTRY_BYTES, NEXUS_MAX_INGEST_BYTES, NEXUS_OCR, NEXUS_RERANK_WEIGHT_CLICK, NEXUS_STEMMING (confirmed); spell-budget trio are covered by the "NEXUS_SPELL_MAX_TERM_LEN etc." row (not literally missing); "checker only checks documented vars" **REFUTED** (it scans code); **NEW N2**: `NEXUS_STOPWORDS` is promised by a tokenizer docstring but read NOWHERE in code — documenting it in .env.example would document a lie → fix the docstring instead |
| H9 CRLF + dash | **PARTIAL** | `git ls-files --eol`: 5 tracked files have **worktree CRLF / index LF** (vector_store.py, wp12/r5b_reload_lock_stall.py, wp13_wp11_baseline.json, links/test_authority.py, test_end_to_end.py) — renormalize NOT warranted (index already LF); AUDIT_REMEDIATION.md + scripts/dev/smoke_check.py are LF (**refuted**); seeds.txt is untracked with a single stray CR (refuted); `.gitattributes` line 3 carries byte 151 (cp1252 em dash) — **confirmed** |
| H10 click/LTR phase drift | **CONFIRMED** | signals.py:11,163,176 say "Phase 7"; PHASE7_PLAN defers click-feedback learning to Phase 8+; README Phase 5 lists "Click Signals" + "Learning-to-Rank Framework" without interface-only tags |
| H11 wp12 a1/a2 leave DBs in repo root | **CONFIRMED** | `wp12_a1_test.db{,-wal,-shm}`, `wp12_a2_test.db{,-wal,-shm}` present (dated 10/6), scripts use relative DB paths |
| H12 test.yml exists | **CONFIRMED** | tracked, matches audit-log description (charset matrix 3.5.1+3.4.6, docker job, lints); the "absent from zip" premise was stale (matches WP11 P0-2's earlier finding) |
| H13 n8n workflow defects | **ALL CONFIRMED** | Qdrant Upsert `id: chunk.index` (collision across docs); both Qdrant URLs are `__PLACEHOLDER_VALUE__`; SSRF regex misses 172.16/12, IPv6 (incl. ::1/fe80::/fd00::), numeric-IP encodings, no redirect re-validation, no pinning; robots parse is prefix-only with no Allow precedence/wildcards; RAG prompt has NO fencing/injection stripping; confidence `0.4+0.15×sources` is not a retrieval signal. JSON parses; 75 nodes; all connections resolve by name; no secrets embedded (credentials via n8n credential types) |

NEW findings from my own runs: N1 (crawl-delay flake, P2-ops, ledgered),
N2 (NEXUS_STOPWORDS docstring promises a knob that no code reads — P1 honesty
defect), N3 (check_env_docs allowlist masks OCR/STOPWORDS entries), N4
(symlink probe UNVERIFIED locally — privilege), N5 (7 routes declare no
response_model — P3).

## Items — per-item log

Full per-item root-cause/before-after/commit table: WP14 section in
`docs/AUDIT_REMEDIATION.md`. Commit chain on branch `wp14` (off `phase-7`):

1. `7032eba` docs — Step 0 report
2. `fc41c95` Item 1 — min_score end-to-end (14 tests)
3. `5bf68b4` Item 2 — _run_search extraction (13 tests, zero test edits)
4. `e9c15ae` Item 3 — Dockerfile HF_HOME/baked-model fix (docker UNVERIFIED-local)
5. `a7e632c` Item 4 — compose env_file pass-through (verified via compose config)
6. `ed1939c` Item 5 — test_ab.py + checklist accuracy + paths lint in CI
7. `3868bd8` Item 6a — __version__ 0.6.0
8. `d779bd2` Item 6b — requirements-dev.txt split
9. `fc69003` Item 6c/6d — rerank smoke + NEXUS_STOPWORDS honesty fix + env docs
10. `5ed7f5f` Item 6e/6f/6g — dash, CRLF worktree, phase-drift docs, wp12 temp dirs
11. Item 6h — packaging verified: 240 entries, zero junk (no repo change)
12. Item 7 — coverage-gap tests (backup CLI, parser edges, vector-store,
    security edges, mime, CLI dispatch, seed_from_sitemap): 63 new tests;
    total 87% -> 89% (branch on)
13. Item 8 — PHASE7_PLAN guard rails (/ask unconditional key-gating,
    per-worker limiter must-copy note, LLM stub interface)
14. Item 9 — n8n Qdrant point-id collision fixed (deterministic UUID of
    hash+chunk index), n8n.md reference-only label + H13 limits, JSON
    re-validated (75 nodes, connections resolve, no secrets)

## Final gate (fresh venv, 2026-10-11)

- Offline: **944 passed, 7 skipped, 623 subtests** (was 844/7/614)
- Model-gated: **949 passed, 2 skipped** (was 849/2)
- Load smoke **1 passed**; graph bench **16 passed, 2 skipped** (+1 rerank smoke)
- Lints: duplicate-tests clean; env-docs 30 vars; checklist paths 6 full + 38 bare
- rerank_benchmark: exit 0, identical metrics, now smoke-tested
- Coverage: **89%** (was 87%); branch coverage on both runs
- Golden: byte-identical (empty `git diff phase-7..HEAD` on the baseline)
- Route matrix/boot-refusal/concurrency probes: all re-run from the fresh
  venv, identical to Step 0 (the 3 dev_matrix "failures" are documented
  probe-expectation artifacts, not app defects)
- Docker: UNVERIFIED-locally (engine down all session despite launch
  attempts; CI docker job carries it)
- Repo-root litter: zero new files across all runs; stale wp12_a*_test.db*
  scratch deleted

## Honest mistakes made and fixed during WP14 (recorded)

- PowerShell `Get-Content`/`WriteAllText` roundtrip mojibaked SPEC.md and
  PHASE6_PLAN.md mid-session (caught immediately by re-reading the output;
  both reverted via `git checkout` and redone with a safe editor).
- The golden "before hash" comparison was corrupted once by PowerShell's
  UTF-16 redirect; the correct proof is `git diff` on the blob (recorded).
- First mutation-script run: ModuleNotFoundError (sys.path), fixed; first
  test run of my own min_score suite had a harness bug (no EmbeddingSync
  attached -> zero vectors) and a 2.0 floor outside the API's [-1,1] bound;
  both fixed before any production code was touched.
