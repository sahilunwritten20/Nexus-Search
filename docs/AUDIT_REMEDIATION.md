# Audit Remediation Log — Phases 1–6

Running log for the `fix/audit-phase1-6` remediation. One entry per work
package (WP0–WP10). "Baseline" numbers are pre-fix measurements captured on
the branch point; "After" numbers are re-measured with
`scripts/dev/audit_repro.py` and the named benchmarks at the final gate.

## Environment (all runs)

- OS: Windows 11 (win32); shell: PowerShell 5.1
- Python: 3.14.7 (.venv); CI target: Ubuntu / Python 3.12 (CI workflow runs
  the full suite + image build + lints on it; not runnable locally — Unverified here)
- Key packages: fastapi 0.141.1, numpy 2.5.3, sentence-transformers 6.1.0,
  torch 2.14.0, requests 2.34.2, sqlite3 (stdlib)
- Suite env: `NEXUS_ENV=dev NEXUS_EMBEDDER=hash:384` unless stated
- Model-gated suite: `NEXUS_RUN_MODEL_TESTS=1` with cached
  `sentence-transformers/all-MiniLM-L6-v2`

## WP0 — Baseline capture

| Item | Baseline (pre-fix) |
|---|---|
| Offline suite | **634 passed, 3 skipped** (262 s) — matches audit |
| Model-gated embedder suite | **20 passed, 0 skipped** (83 s) |
| Test functions defined vs collected | 638 defined / 637 collected (1 shadowed duplicate, BUG-13) |
| Golden keyword snapshot | `tests/golden/keyword_baseline.json` — 15 queries, 54 results; regression test `tests/core/test_golden_keyword.py` green |
| Junk-query `understand_query` CPU | 229 chars: **4.87 s** · 629: **13.50 s** · 1689: **42.04 s** · 2000: **43.12 s** (BUG-01) |
| Hybrid pagination | offset=50 → **0 results** while total=120; top_k=100 → **50 results** (BUG-03/04) |
| URL-variant authority | 4 variant links to one page → **4 edges**, `authority_for` **miss** (BUG-02) |
| `record_edge` ingest scaling | 250 edges: **2.05 s** · 500: **4.32 s** · 1000: **31.93 s** — quadratic (BUG-05) |
| `record_edges` bulk 10k edges | **0.17 s** — but unnormalized, uncapped, resets `first_seen` (BUG-09) |

Baseline machine-readable: `docs/baseline_repro.json`
(`python scripts/dev/audit_repro.py --json <path>` regenerates).

Network probe: DNS for `quotes.toscrape.com` resolves (35.211.122.109) —
outbound network available; used by WP9.

## WP1 — BUG-01: spell-correction CPU DoS

- Status: **FIXED**
- Root cause: `correct_spelling` expanded the full distance-2 edit frontier
  (Norvig-style set expansion, ~O(len²·26²) strings) per out-of-vocabulary
  term; junk queries hit the "no dist-1 hit" path and always paid the full
  dist-2 expansion. It ran before the query cache on every request.
- Fix (defense in depth, all layers tested):
  1. `ranking/query.py`: distance-2 correction now scans the vocabulary's
     length window once per term with an early-exit restricted-Damerau
     (OSA) check — identical hit set and tie-breaking, bounded cost. Terms
     longer than `NEXUS_SPELL_MAX_TERM_LEN` (20) are never corrected;
     dist-2 only up to `NEXUS_SPELL_D2_MAX_TERM_LEN` (12); at most
     `NEXUS_SPELL_MAX_CORRECTED_TERMS` (8) terms corrected per query
     under one shared `NEXUS_SPELL_CANDIDATE_BUDGET` (4000 comparisons).
     On exhaustion the query is returned un-rewritten with
     `QueryUnderstanding.spelling_exhausted=True` — never an error.
  2. `core/query_parser.py`: parse memoized per query string (pure
     function; defensive copies per call), terms capped at
     `NEXUS_MAX_QUERY_TERMS` (128), phrases at 64, with `*_truncated` flags.
  3. `core/api.py`: cache lookup happens BEFORE any understanding work;
     understanding is computed lazily, only on the paths that need it.
     Queries over `MAX_QUERY_WORDS` (128) words are rejected pre-parse
     with a clear 400 — the same default as the term cap, matching how
     real search engines bound query complexity.
  4. `core/storage.py` + `core/bm25.py`: batched `document_frequencies` /
     `postings_for_terms` (one IN query per search instead of 2 round trips
     per term), consumed with the SAME set-iteration and accumulation
     order, so scores stay byte-identical (golden-pinned).
- Evidence (baseline -> after, `understand_query` CPU on junk):
  229 chars 4.87 s -> **2 ms**; 629: 13.50 s -> **2 ms**; 1689: 42.04 s ->
  **6 ms**; 2000: 43.12 s -> **5 ms**. Acceptance (<100 ms) met with >40x
  margin; >128-word API shapes are 400-rejected pre-parse.
  `tests/core/test_search_dos.py` proves a normal request answers < 1 s
  during a 50-request junk flood (stable across 3 runs) and that
  under-guard junk (100 words) drains without 5xx.
- Tests: `tests/ranking/test_spell_budget.py` (14 new),
  `tests/core/test_search_dos.py` (5 new). Existing spelling-quality tests
  unchanged and green.
- Decision (recorded): `NEXUS_SPELL_CANDIDATE_BUDGET` default 4000
  comparisons — generous at prototype vocab scale, degrades honestly
  (partial dist-2 coverage) on very large vocabularies; documented.

## WP2 — BUG-02: Phase 6 graph node identity split

- Status: **FIXED**
- Root cause: `crawler/pipeline.py` recorded `to_url` raw (fragments, utm
  params, trailing slash, host case, default ports) while page URLs were
  frontier-normalized; `authority_for` is an exact-match lookup, so scores
  missed and PageRank mass fragmented across URL variants of one page.
- Fix:
  - `links/graph.py`: ONE identity choke point (`LinkGraph._norm_pair`) used
    by `record_edge`, `record_edges` and `authority_for` — no caller can
    bypass normalization; self-links are detected post-normalization.
  - `crawler/url_utils.py`: `normalize_url` now also decodes percent-escapes
    of UNRESERVED characters (RFC 3986); reserved escapes never decoded.
  - `crawler/cli.py`: `normalize-links` command — idempotent data migration
    for pre-fix rows: merges duplicate groups (earliest `first_seen`,
    latest `last_seen`, recrawl anchor/rel semantics with non-empty
    fallback), drops post-normalization self-links, one transaction.
- Evidence: audit repro, before: `{"edges_stored": 4, "authority_found":
  false}`; after: `{"edges_stored": 4 (4 distinct sources — by design),
  "authority_found": true, "authority": 0.7}` — the variant target resolves
  to ONE node with merged in-link mass (test-pinned: 6 variant inlinks ->
  inlink_count 6 on one node).
- Tests: `tests/links/test_graph_normalization.py` (9 new), incl. a live
  local-server e2e (three variant links -> one edge, non-zero authority).
- docs/PHASE6_PLAN.md §6 ("edges are keyed by normalized URLs") is now TRUE
  and verified by test, not by editing text.

## WP3 — BUG-03 + BUG-04: hybrid pagination + top_k truncation

- Status: **FIXED** (+ one latent paging bug the fix's own walk test exposed)
- Root cause: the fused candidate pool was sized by `candidates` (API
  default 50) regardless of the requested window. offset >= pool sliced an
  empty tail while `total_results` claimed the full match count (BUG-03);
  top_k > pool silently truncated (BUG-04).
- Latent bug found by the new walk test: a pool that merely GROWS with
  offset re-ranks the boundary between requests — pages overlapped
  (120 fetched, 95 unique). The pool must be a function of the query, not
  the page.
- Fix: effective pool = max(candidates, offset+top_k), clamped by
  `NEXUS_MAX_CANDIDATES` (default 1000); pool sized ONCE via a cheap
  indexed `matching_doc_count` (WP8 refinement); API raises a clear 400
  when `offset+top_k` exceeds the cap and clamps `candidates` to it;
  KEYWORD mode untouched (BM25 native offset, golden-pinned); SEMANTIC
  caveat documented (pass `candidates` for strict window stability).
- Evidence: audit repro, before: offset50 -> 0 results (total 120),
  top_k=100 -> 50; after: **50 results at offset=50** (full page),
  **100 results at top_k=100**; walk test tiles 120 docs with no gaps/dups.
- Deliberate existing-test change (documented in the commit):
  `test_hybrid_total_is_true_match_count` pinned the BUG-04 behavior itself;
  it now pins the fixed contract.
- Tests: `tests/core/test_pagination_contract.py` (11 new).

## WP4 — BUG-06: shared HybridSearch + BUG-05: indexed domain-pair cap

- Status: **both FIXED**
- BUG-06: per-request HybridSearch construction discarded the BM25
  doc-token memo across requests. Fix: `search_page`/`explain` take
  per-call `bm25_weight`/`vector_weight` overrides; the API serves
  /search, facets and explain from ONE module-level instance.
- BUG-05: the pair cap ran `LIKE '%//host/%'` per new edge — O(E²) ingest,
  missed root URLs, matched subdomains by pattern accident. Fix: link_graph
  migration v2 adds `from_domain`/`to_domain` + index (idempotent backfill
  at open); the cap is an indexed COUNT. Subdomain policy DECIDED and
  documented: exact-host (no PSL dependency; farms still bounded by the
  1000/page cap). LinkGraph now sets `journal_mode=WAL` like every other
  store (per-edge commits were paying a rollback-journal fsync).
- Evidence: record_edge 250/500/1000 edges, before -> after:
  2.05/4.32/31.93 s (quadratic) -> **0.77/1.16/2.42 s (linear)**.
- Tests: `tests/core/test_shared_searcher.py` (7),
  `tests/links/test_domain_cap.py` (8). Existing link_graph
  migration-version tests bumped 1->2 (the runner working as designed).

## WP5 — Phase 6 completion

- Status: **DONE** — all 8 previously-missing bullets implemented with
  tests, plus the operational pieces. Migration v3 (link_graph): is_internal
  (exact-host policy, -1 -> backfilled), url_anchors, domain_authority,
  graph_meta. Highlights:
  - Traversal: neighbors(out|in|both, limit<=500), iterative bounded BFS,
    key-gated `GET /graph/neighbors` (+ `/graph/report` summary).
  - Components: iterative union-find, deterministic ids; the fake
    `test_disconnected_components` replaced with real count/size assertions.
  - Dead-link detection: frontier-4xx cross-ref CLI (`dead-links`), read-only;
    Orphan detection: `orphans` CLI, seeds excluded.
  - anchor_relevance signal: url_anchors aggregated once per recompute
    (512-char cap); `NEXUS_ANCHOR_WEIGHT` default 0.0; zero-weight identity
    test-pinned (graph vs no-graph byte-identical at 6 dp).
  - domain_authority: PR-weighted aggregate; `authority_for` falls back only
    behind `NEXUS_DOMAIN_AUTHORITY_FALLBACK` (default off).
  - Incremental recompute: edge-set version counter (inserts and anchor/rel
    changes bump; pure last_seen refresh does not; version captured BEFORE
    reading edges so mid-compute writes force the next pass; `--recompute`
    forces explicitly).
  - Graph cache: in-memory authority reads, invalidated on in-process swap
    AND cross-process writes (data_version polling) — rerank pays no
    per-candidate SQL.
  - Background PageRank: `NEXUS_AUTHORITY_RECOMPUTE_INTERVAL` (0=off
    default), overlap-guarded worker, stops on lifespan shutdown and on
    first hard error (loudly).
  - Benchmarks: `evaluation/graph_benchmark.py` — **10k pages / 100k edges:
    recompute 3.2 s against the committed 30 s bound (9x margin), ingest
    18.5 s, converged 13 iters** (PHASE6_PLAN §3 now carries the measured
    number); `evaluation/authority_benchmark.py` — mission shape: authority
    off NDCG@10 0.541 -> on **1.000 (+0.459)**, hub to rank 1.
  - Spam caps on normalized URLs test-pinned (variant edges collapse before
    the pair cap; reciprocal discount survives normalization).
- Promoted BUG-10 from WP6 (full-suite flakiness hit first): `_VOCAB_CACHE`
  is now a WeakKeyDictionary — id()-keyed entries could be served to a
  different Storage after address reuse.
- Tests: `tests/links/test_graph_analysis.py` (22),
  `tests/links/test_graph_api.py` (9), `tests/evaluation/` (+4).
- Suite after WP5: 724/4.

## WP6 — Hygiene, security, database, Phase 2

- Status: **DONE**
  - BUG-07: 4xx (except 408/429) terminal in `frontier.mark_error` — live
    local-server test proves a dead link is fetched exactly ONCE (was 3x).
  - BUG-08: `/search?debug=true` key-gated like `/search/explain` when a
    key is configured (401 without, 200 with; open in dev by design).
  - BUG-09: `record_edges` rewritten — normalization at the choke point,
    per-source + per-domain-pair caps via running counters, ON CONFLICT
    upsert preserving `first_seen` (was INSERT OR REPLACE resetting
    history).
  - BUG-13: all THREE shadowed duplicate tests renamed with real variants
    (AST scan now enforces zero via the CI lint).
  - `NEXUS_TRUST_PROXY=N` rate-limit keying (default off: spoofed
    X-Forwarded-For ignored; tested both ways).
  - `security.py`: `socket.getaddrinfo` monkey-patch moved to explicit
    idempotent `install_dns_pinning()` (subprocess test proves import
    alone patches nothing); crawler pipeline installs it.
  - IPv6-literal SSRF tests: ::1, ::ffff:127.0.0.1, fe80::/link-local,
    fc00::/ULA, 2001:db8::/documentation all blocked; global literal passes.
  - storage v2: `idx_documents_added_at` + batched `added_at_map` — the
    freshness sort no longer does a get_document per result.
  - Phase 2 markdown reader: front matter/fences/headings/links/images/
    emphasis/list markers handled, everything stays searchable.
  - MIME: magic-byte sniffing (PDF %PDF-, Office zips by internal parts,
    text-vs-binary) with content-first routing — a lying extension (real
    PDF/docx named .txt) routes by its real bytes; binary garbage refused.
  - A/B retention CLI: `python -m nexus_search.ranking.ab purge --days N`.
- Tests: `tests/test_wp6_hygiene.py` (23).

## WP7 — Documentation, config, ops

- Status: **DONE**
  - Embedder default: code now defaults `NEXUS_EMBEDDER=hash:384` — code,
    README, .env.example and compose all agree (offline-safe first boot;
    `st:...` stays one explicit env away, pre-baked in the image).
  - `NEXUS_RERANK_WEIGHTS`: JSON override for any subset of the 13 rerank
    signal weights (validated, clamped [0,1], garbage logged-not-fatal).
  - `core/backup.py`: SQLite online-backup snapshots (consistent under WAL),
    `--with-frontier`; restore roundtrip test proves the snapshot answers
    searches.
  - README: every Phase 6 bullet carries an explicit status marker matching
    shipped code; env table covers every knob; PHASE4_PLAN historical
    banner; SPEC.md Phase 5/6 honesty rows updated to post-remediation
    reality.

## WP8 — Gaps, scope boundaries, CI, load smoke

- Status: **DONE**
  - CI: duplicate-test-name lint, env-var/documentation consistency check
    (25 vars verified), graph benchmark smoke + authority benchmark steps.
  - `tests/test_load_smoke.py` (NEXUS_RUN_LOAD_SMOKE=1): **200 mixed
    requests / 20 workers -> p95 881 ms (bound 2000 ms), zero 5xx,
    starvation probe 882 ms**.
  - `evaluation/scale_benchmark.py` (1K/5K/10K) exposed super-linear hybrid
    growth (x22 for x10 docs) -> fixed: pool sized once via indexed
    `matching_doc_count` (no fetch-then-refetch) + BM25 candidates
    batch-fetched in one IN query. **10K hybrid 3491 -> 1958 ms; 5K->10K
    now linear; keyword output stays golden-identical.**
  - Deferred items explicitly labeled (see README/SPEC): click signals
    (Phase 7), LTR training (Phase 7+), A/B analysis (production traffic),
    Prometheus/structured logging (Phase 10), multi-process politeness +
    distributed rate limiting (Phase 8). Known-limitations section documents
    robots wildcards, single-writer SQLite, heuristic-only spam detection.

## WP9 — Real public crawl validation

- Status: **DONE — and it found a real bug (BUG-14)**
  - Ran the PRODUCTION crawl path against the real internet
    (`quotes.toscrape.com`, the repo's own seeds.txt sandbox): SSRF + DNS
    pinning + placeholder-UA refusal active, concurrency=1, 1 s crawl
    delay, 25-page/depth-1 cap, wall-clock watchdog.
  - **BUG-14 (found live, fixed with a regression test):** Windows
    `getaddrinfo(host, service=None)` returns type-0 entries; the pinned
    resolver's strict type filter dropped every pinned entry -> ALL real
    external fetches failed ("no pinned address") and robots fail-closed
    killed the crawl. Fix: a type-0 pin serves any requested type. The unit
    tests had hand-built SOCK_STREAM pins and never caught the Windows
    shape — `test_type_zero_pinned_entry_serves_any_requested_type` pins
    it forever.
  - After the fix: **25/25 pages, 0 errors, 137,541 bytes in 60.8 s**
    (0.41 pages/s, politeness-delayed), 44 docs indexed, **512 REAL edges,
    authority over 119 pages converged in 7 iters / 0.09 s, top authority =
    goodreads.com/quotes + zyte.com (footer links on every page — exactly
    right), 0 dead links, 94 linked-but-unfetched (depth-cap artifact,
    honestly reported), 0 orphans.** Keyword vs hybrid totals differ as
    expected over the crawled corpus.
  - Deliverables: `docs/REAL_CRAWL_REPORT.md` + re-run rules in the
    smoke script's docstring. Deliberate decision (recorded): the UA is an
    honest audit-validation identity; the target is a scraping-practice
    sandbox (no fabricated contact claims; the placeholder-UA refusal
    stays active and passes it).

## WP10 — Labeled semantic-quality benchmark

- Status: **DONE**
  - `evaluation/semantic_benchmark/`: deterministic 336-doc / 77-query
    fixture across 6 categories (paraphrase/synonym/exact/multi/typo/
    no-answer), construction-ground-truth labels (protocol + the
    no-human-second-pass limitation documented), committed 60/40 dev/test
    split; results on held-out test only; paired bootstrap CIs; no-answer
    top-1 score distributions; coverage gate (refuses below 100%).
  - Measured (docs/SEMANTIC_BENCHMARK.md): **st beats hash on paraphrase
    NDCG@10 0.625 vs 0.351 and synonym 0.869 vs 0.715 — the core semantic
    claim VERIFIED**; hash's trigram features rescue typos (0.430 vs
    keyword 0.000); keyword wins multi-intent (honest finding); keyword
    serves CONFIDENT junk on no-answer queries (top-1 scores 5.4-9.1)
    while hybrid tops out at 0.03 — the desired low-confidence behavior.
    Overall st-vs-hash CI crosses zero (opposite category winners) —
    documented; read per-category.
  - Regression floors: hash floors in default CI (keyword 0.55, hash 0.65,
    typo-rescue 0.40, exact 0.85); st floors (paraphrase >= 0.60 AND st >
    hash) behind `NEXUS_RUN_MODEL_TESTS=1` — all pass locally with the
    cached model.
  - Defaults were NOT tuned on the test split — shipped defaults measured
    as-is.

## FINAL VERIFICATION GATE (executed 2026-10-03)

| Gate | Result |
|---|---|
| Offline suite | **760 passed, 7 skipped** (5:54) — skips are model/benchmark/load gated |
| Model-gated suite (`NEXUS_RUN_MODEL_TESTS=1`) | **765 passed, 2 skipped** (5:08) |
| Full 10k/100k graph benchmark (`NEXUS_RUN_GRAPH_BENCH=1`) | **passed** — recompute 3.2 s < 30 s bound |
| Original repros (before -> after) | junk 43.12 s -> **0.005 s**; offset=50: 0 -> **50 results**; top_k=100: 50 -> **100**; URL-variant authority: miss -> **found (0.7)**; ingest 1000 edges: 31.93 s -> **2.42 s (linear)**; 10k bulk: 0.17 -> 0.62 s (caps+domains now computed, still sub-second) |
| Authority on/off boot validation | default weights: ranking unchanged; `NEXUS_AUTHORITY_WEIGHT=0.2` + restart: **hub reorders to #1** |
| Golden keyword identity | `test_golden_keyword` green — keyword output byte-identical through every change |
| Load smoke | 200 reqs / 20 workers: **p95 881 ms** (bound 2000 ms), zero 5xx |
| Backup restore roundtrip | snapshot restores and answers searches (test-pinned) |
| Git hygiene | `git ls-files`: no DB files, no secrets, no .env |
| Docs consistency | `check_env_docs`: 25 documented vars, zero undocumented; `check_duplicate_tests`: zero |

## Unverified (honest limits)

- **Linux/Python 3.12**: not runnable on this Windows machine. The CI
  workflow (updated this branch) runs the full suite + image build + non-root
  assertion + /health probe + both new lints on Ubuntu/3.12 — results will
  appear on the first push. Command to close locally, if desired:
  `python -m pytest -q` under a 3.12 interpreter.
- **Docker boot probe**: Docker is not installed on this machine; the
  same CI job builds the image and probes /health.
- **Web-scale authority quality**: heuristics are validated on synthetic
  graphs + the real sandbox crawl, not against production spam farms
  (none exists to validate on) — stated in SPEC.md.

## WP11 — Phase-7 readiness hardening (STEP 0 audit + P0/P1/P2 fixes)

Scope: read-only audit of the modules NOT covered by WP0-WP10
(`core/hybrid_search.py`, `core/query_parser.py`, `ranking/*`,
`crawler/frontier.py`+`pipeline.py`+`cli.py`, OOXML/PDF connectors,
`ingestion/pipeline.py`+`dedup.py`+`failures.py`, `core/reindex.py`,
`core/backup.py`, `core/migrations.py`), then test-first fixes for the
task-listed P0/P1/P2 items and every confirmed P0/P1 audit finding.

### STEP 0 — audit report (read-only; no code changed)

Baseline at audit time: `760 passed, 7 skipped` (offline, Python 3.14.7,
Windows 11). Findings below; "task item" = already on the P0/P1/P2 work
list, "new" = found by this audit pass.

| Sev | Where | Finding / repro | Fix | Status |
|---|---|---|---|---|
| P0 | core/api.py:290 | `_READ_AUTH` built from `require_api_key` defined 4 lines LATER; fresh process + `NEXUS_REQUIRE_AUTH_FOR_READS=1` -> `NameError` at import (repro'd in a subprocess: `NameError: name 'require_api_key' is not defined`; the reload-based test masks it). | task item P0-1 | fixed (P0-1) |
| P0 | core/api.py:249 | `_rate_limit_key` buckets by sha256 of ANY `X-API-Key` value; rotating random keys = fresh bucket per request = unlimited `/search` budget from one IP. | task item P0-3 | fixed (P0-3) |
| P0 | ingestion/connectors/files.py:28 | `_detect_encoding` trusts charset-normalizer's first guess; on 3.5.1 (pinned!), "Café"(cp1252) -> `utf_16_be`, "São Paulo" -> `big5` (repro'd); on 3.4.x the audit found `mac_latin2` variants. Short-sample guesses are luck, not detection. | task item P0-4 | fixed (P0-4) |
| P0 | .github/workflows/test.yml | Task said "deleted in working tree". ACTUAL state: tree clean, file tracked and identical to origin/main; GitHub default branch shows the FULL tree (workflows, docs/, Dockerfile, compose, scripts/dev, automation/). The "smaller tree" snapshot was stale. Remaining: push WP11 and confirm the run is green. | task item P0-2 | verified + CI watched (P0-2) |
| P1 | links/authority.py:132-141 | Outflow normalization uses DISCOUNTED weights (`out` sums `wts`), so scaling every edge of a closed clique by 0.25 cancels: repro'd — 4-site clique + 1 honest inlink gives IDENTICAL PageRank at RECIPROCAL_FACTOR 1.0 vs 0.25. Discount is a no-op exactly where it matters (closed farms). | task item P1-5 | fixed (P1-5) |
| P1 | core/bm25.py:_highlight | `highlight=true` emits content HTML unescaped: repro'd `hello <img src=x onerror=alert(1)> world` -> `<mark>hello</mark> <img src=x onerror=alert(1)> world`. Stored-XSS for any UI that renders snippets as HTML. | task item P1-6 | fixed (P1-6) |
| P1 | crawler/fetcher.py:_render_with_browser | Playwright navigates itself: subresource/DNS/redirect validation bypassed entirely; `render_js` reachable via constructor with no env gate. | task item P1-7 | fixed (P1-7) |
| P1 | ingestion/connectors/files.py (docx/xlsx/pptx readers) | NEW: zip-container readers (docx/xlsx/pptx) have NO decompressed-size guard. `NEXUS_MAX_INGEST_BYTES` caps the FILE (64 MiB), not the payload — a ~200 KiB "docx" whose XML inflates >512 MiB is fully materialized by python-docx/openpyxl/python-pptx (zip-bomb class). PDF reader also has no page-count bound ("huge pages" class). | new finding -> P1-12a | fixed (P1-12a) |
| P2 | core/vector_store.py:284-290 | `search()` copies `_matrix` AND `_doc_ids` AND `_doc_types` AND `_languages` per query; the last two are never used for results; matrix copy is O(n·dim·4) per query. | task item P2-8 | fixed (P2-8) |
| P2 | core/api.py:106 + core/vector_store.py:216 | API embeds synchronously per write (`EmbeddingSync(batch_size=1)`); `add()` `np.vstack`s per append -> quadratic bulk ingest; no bulk flush after `/documents/bulk`. | task item P2-9 | fixed (P2-9) |
| P3 | crawler/pipeline.py:327 | `except Exception: pass` in `close()` — swallows real shutdown errors (e.g. frontier commit failure) silently. Pattern the audit was told to hunt. | new finding -> P1-12b | fixed (P1-12b) |
| P3 | ingestion/connectors/files.py:58 | `except Exception: pass` in `_detect_encoding` — intentional degrade-to-utf-8 but fully silent (no log line). | fold into P0-4 | fixed (P0-4) |
| P3 | evaluation/semantic_benchmark/runner.py:264,272 | `except Exception: pass` in benchmark cleanup paths (offline eval tool, no request path). | report only | not fixed (accepted) |
| P3 | ranking/suggestions.py:58-95 | `related_searches` trigram fallback scans the whole vocabulary per request when the query log is empty — O(corpus vocab) CPU, bounded by index size, rate-limited endpoint. | report only | not fixed (accepted at prototype scale) |
| P3 | core/reindex.py:52-66 | Shadow build materializes title+content of the whole corpus in RAM (snapshot dict) — CLI maintenance path, bounded by corpus size, documented. | report only | not fixed (accepted) |
| P3 | core/models.py:29 | `/documents/bulk` worst case 500 docs x 10 MB content in one request body (uvicorn has no default body cap) — bounded by design constants but large; recommend a proxy-level body cap in deployment. | report only | not fixed (accepted) |
| P3 | crawler/frontier.py:210 | `add()` commits per URL — per-link fsync cost during crawls, not a correctness issue (single-writer by design). | report only | not fixed (accepted) |
| OK | core/hybrid_search.py | Fusion math, pagination bounds (`max_candidates`), fallback paths (EmbedderUnavailable vs generic, reason recorded) — clean. Candidate fetches batched; N+1s fixed in WP8. | — | — |
| OK | core/query_parser.py | Bounded memo (512, defensive copies), term/phrase caps, boolean gate — clean. | — | — |
| OK | ranking/{ranker,signals,features,query,ab}.py | All signals pure; spell-correction budgets (BUG-01) intact; WeakKeyDictionary vocab cache (BUG-10) intact; log parameterized + locked. | — | — |
| OK | crawler/frontier.py (SQL/threading) | Single conn + RLock, parameterized SQL; `_ensure_column` f-strings are internal constants only. `next_batch` placeholders are `?`-only. Multi-step writes are single-transaction commits. | — | — |
| OK | crawler/{cli,pipeline}.py (paths) | DB paths from CLI args only (operator, not remote input); UA refusal on public path; dead-letter stance intact. | — | — |
| OK | ingestion/{pipeline,dedup,failures}.py | One shared ingest path; failures recorded AND re-raised (never swallowed); dedup locks correctly; SQLite all parameterized. | — | — |
| OK | core/{reindex,backup,migrations}.py | Shadow-swap atomic; backup uses online API and raises on failure; migrations per-store versioned, transactional, no executescript. | — | — |
| OK | crawler/security.py, sitemap.py, politeness.py | DNS pinning + validate_url solid (WP9); sitemap defusedxml + gzip cap; politeness fail-closed contract intact. (sitemap docstring has a stray markdown link — P3-11.) | — | — |

"Before" evidence captured live on the pre-fix tree (Python 3.14.7,
charset-normalizer 3.5.1):

- P0-1 subprocess boot: `NameError: name 'require_api_key' is not defined`
- P1-5 closed 4-clique + honest inlink: PageRank `farm0=0.257402,
  farm1..3=0.237533` IDENTICAL at factor 1.0 and 0.25
- P1-6: `'<mark>hello</mark> <img src=x onerror=alert(1)> world'`
- P0-4 on pinned 3.5.1: `Café` -> `utf_16_be`, `São Paulo` -> `big5`,
  `Café Mug` -> `utf_16_be` (mojibake); `Zürich Straße Müller` -> `cp1250`
  (rescued by the existing cp125x fallback); long Shift-JIS -> `cp932`,
  GBK -> `gb18030` (correct — must not regress).

Per-item entries follow (one commit each).

### WP11 items — root cause, fix, evidence

| Item | Status | Root cause -> fix | Before -> after evidence | Tests added |
|---|---|---|---|---|
| P0-1 read-auth fresh-process NameError | **fixed** (4d40e89) | `_READ_AUTH` referenced `require_api_key` 4 lines before its definition; only reload-based tests masked it. Fix: auth defs moved ABOVE `_READ_AUTH`, order documented in-file. | Fresh-process boot: `NameError: name 'require_api_key' is not defined` -> boots; /search 401 without key, 200 with key. | `tests/core/test_fresh_boot.py` (2 subprocess tests; the flag-off path too) |
| P0-2 CI workflow deleted | **verified, nothing to restore** (2f6d81b, watch continues) | Premise stale: tree clean, `.github/workflows/test.yml` tracked and identical on origin/main; GitHub default branch shows the FULL tree (workflows/docs/Dockerfile/compose/scripts/dev/automation) — the "smaller tree" snapshot was outdated. Pushed WP11 commits; CI result checked at the final gate (below). | `git status` clean; `git ls-tree origin/main` shows all paths. | — |
| P0-3 rate-limit bypass via random keys | **fixed** (28ca170) | `_rate_limit_key` hashed ANY `X-API-Key` value into its own bucket; rotating values = unlimited budget. Fix: bucket only on `hmac.compare_digest(key, NEXUS_API_KEY)`; unset key = header ignored; else fall through to XFF/IP. NOTE: `test_limit_is_scoped_per_api_key` ENCODED the vulnerable semantics (per-key buckets with NO configured key); it was adapted to the new contract — configured key, valid-key bucketing asserted PLUS wrong-key-shared-bucket assertions — strictly stronger, not loosened. | 70 reqs / 70 random keys: all 200 (bypass) -> 429 appears at req 61. TRUST_PROXY tests unchanged and green. | `test_random_api_keys_cannot_bypass_limit`, `test_random_keys_ignored_when_no_api_key_configured`; `test_limit_is_scoped_per_api_key` rewritten per above |
| P0-4 encoding detection on short files | **fixed** (bc98f6f) | Detector's first guess trusted unconditionally; short samples misguess even on the pinned charset-normalizer 3.5.1. Fix: BOM -> strict UTF-8 -> detector, with candidate-scan verification: Western-family guesses prefer strict cp1252 (now incl. mac_latin2/mac_roman); CJK-family guesses kept only when their decode is CJK-heavy (measured margin ~100% vs ~12%); BOM-less utf_16/32 kept only on NUL-interleave or >=32-byte CJK output; silent `except: pass` now logs at debug. Honest limit documented: tiny genuine BOM-less UTF-16 CJK may flip to cp1252 (rare; BOM'd UTF-16 caught earlier). | On pinned 3.5.1: 'Café' -> `utf_16_be`, 'São Paulo' -> `big5` (mojibake) -> both cp1252, exact roundtrip. Validated on charset-normalizer 3.4.6 AND 3.5.1 (pip swap, restored; requirements pin unchanged). | `TestShortWesternEncoding` (8 tests: cp1252 trio + sentence, longer Shift-JIS/GBK roundtrips, CJK-survival guard incl. big5 rescue via candidate scan) |
| P1-5 reciprocal discount no-op | **fixed** (e91417e) | Outflow normalized by the DISCOUNTED weight sum — every edge AND denominator scaled by the factor, so closed farms cancelled. Fix: normalize by UNWEIGHTED out-degree; withheld mass goes to the uniform (dangling-style) pool — shared by everyone, never channeled into the farm; PageRanks sums to 1 exactly. README/SPEC now document the exact-hostname domain-diversity limit (~10 subdomains saturate; public-suffix dep deliberately not added). | Closed 4-clique + honest inlink: farm PR 0.257402/0.237533 IDENTICAL at factor 1.0 vs 0.25 -> farm members drop (0.237533 -> 0.187357), farm total drops, honest rises (0.030 -> 0.140). PR sum = 1.0 both factors (assertAlmostEqual 1e-5). Authority benchmark holds: retrieval order [clique,hub,orphan] -> [hub,clique,orphan], NDCG@10 0.541 -> 1.000; graph-benchmark smoke: 500 pages, converged, 0.14 s. | `TestReciprocalDiscountEffectiveness` (4 tests) |
| P1-6 highlight XSS | **fixed** (8604cdd) | `_highlight` emitted content HTML verbatim between mark tags. Fix: matching still on RAW snippet (no offset shift), every text segment + match text `html.escape`d after segmentation; mark tags never escaped; protected-region logic unchanged; highlight=False stays byte-identical plain text (documented). | `hello <img src=x onerror=alert(1)> world` -> `<mark>hello</mark> <img src=x onerror=alert(1)> world` (payload intact) -> same payload escaped in output; exactly ONE live mark pair on the decoy case. Golden keyword test untouched and green. | `TestHighlightEscaping` (9 tests: script/img payloads, quotes/&/angles, entity-internal match, no-target path, decoy, CJK, protected-region) + `test_highlight_false_stays_plain_and_byte_identical` |
| P1-7 render_js SSRF | **fixed** (b2ced78) | Playwright navigated + loaded subresources itself, bypassing redirect-hop validation and DNS pinning entirely; no env gate. Fix: `NEXUS_RENDER_JS=1` opt-in (constructor warns + disables otherwise); `page.route("**/*")` validates EVERY subrequest with `validate_url`; final URL validated; residual DNS-rebinding risk documented in the docstring + README (Chromium resolves its own DNS — pinning cannot apply). | Pre-fix (stash-verified): no interception existed, render path reachable without env gate. Post-fix: private subrequest (127.0.0.1:8080, 169.254.169.254) -> `route.abort()`; public -> `continue_()`; private final URL -> None (plain HTTP used). Default config: "playwright" never in sys.modules after a fetch. | 4 new tests (env gate, never-imports-default, subrequest abort matrix, final-URL validation); 2 existing render tests adapted to set NEXUS_RENDER_JS (scenario preserved) |
| P1-12a OOXML zip-bomb + huge-page PDF | **fixed** (3dc8293) | `NEXUS_MAX_INGEST_BYTES` caps the FILE, not the PAYLOAD: docx/xlsx/pptx are zip containers whose declared decompressed sizes can be gigabytes from a ~200 KiB file; PDF had no page bound. Fix: `_check_zip_payload` sums DECLARED sizes from the zip central directory (no decompression) against `NEXUS_MAX_DECOMPRESSED_BYTES` (default 512 MiB) before any reader runs; PDF page count checked against `NEXUS_MAX_PDF_PAGES` (default 10_000) before extraction. | Pre-fix (stash-verified): bomb-shaped docx/xlsx parsed and returned text. Post-fix: 5 MiB-declared entries with a 1 MiB bound -> refused with a logged warning; legit OOXML fixtures still parse. | `TestDecompressionBombGuard` (7 tests) |
| P1-12b pipeline.close swallow | **fixed** (ba8d3ac) | `except Exception: pass` per closer hid real shutdown errors (e.g. frontier commit loss). Fix: per-closer try/except logging `close of <name> failed: <exc>`; remaining closers still run. | Pre-fix: failing fetcher.close vanished silently. Post-fix: warning logged with the exception text, frontier/blocklist closers still executed. | `TestCloseNeverSilentlySwallows` (1 test) |
| P2-8 per-query matrix copies | **fixed** (a428bae) | `search()` copied `_matrix` (n*dim*4 bytes) + `_doc_ids` + two never-used-for-results columns per query. Fix: matmul runs INSIDE `_matrix_lock` (produces a private scores array); only `doc_ids` (n*8 bytes) copied, same lock hold -> no torn pairs; unused column copies deleted. Concurrency tests unchanged and green. | scale_benchmark semantic_ms: 1K 4.7 -> 3.9, 5K 10.2 -> 4.9, 10K **11.9 -> 6.1 (2.0x)**. Caveat noted honestly: the second benchmark run showed ~2.8x drift on the UNRELATED vocab_cold SQLite query (machine load), so the build_seconds/vocab numbers are noise-dominated; the semantic path is pure in-process matmul and its 2x is the direct copy-removal. | existing `test_concurrent_add_remove_search_consistency` etc. pass unchanged |
| P2-9 embedding in write path + quadratic matrix | **fixed** (1bf26ee) | `EmbeddingSync(batch_size=1)` on the API + `np.vstack` per append + FULL `_load_matrix` reload per upsert_batch = quadratic bulk ingest. Fix: amortized-capacity buffers with exact-row views (append O(1) amortized); upsert_batch merges in memory (per-batch full reload removed; `_maybe_reload` keeps the external-write contract); `NEXUS_EMBED_BATCH` config (default 1 = previous behavior); `/documents/bulk` flushes once at the end. | 5,000-doc bulk ingest through the API: old default 433.3 s -> new default 409.5 s -> **NEXUS_EMBED_BATCH=64: 75.7 s (5.7x)**. 10K build in scale_benchmark no longer reloads the store 156x. | `test_amortized_bulk_add_correctness`, `test_upsert_batch_no_full_reload_but_visible`, `TestApiBulkEmbedBatch` (2) |
| P3-10 tokenizer allowlist | **implemented — user-approved** (611a901) | `tokenize("C++ vs C# node.js")` dropped the tech terms ('c','vs','c','node','js'). Fix: boundary-guarded allowlist regex (`c++`, `c#`, `f#`, `node.js`, `.net`) scanned before segment tokenization; fast path (single `search`) keeps allowlist-free input byte-identical; allowlist terms never stemmed. INDEX-AFFECTING: `reindex --shadow` REQUIRED for existing deployments (documented in README). Approval flow honored: failing tests first, diff + golden impact presented, user chose "Implement it". | Failing pre-fix: 5 new tests. Golden baseline: **byte-identical, no regeneration needed** (corpus contains no allowlist terms — verified with an empty `git diff` on keyword_baseline.json). `reindex --shadow` demo on a copy: "node.js" -> d1, "c++" -> d2 retrieved from reindexed postings. Suite 800 -> 807 passed. | `TestTechAllowlist` (7 tests: whole terms, boundaries, no-stem, surrounding text, CJK interplay, fast-path identity) |
| P3-11 repo/docs cleanup | **fixed** (494e039) | CI hygiene step fails on tracked `*.db`/`*.pyc`; `*.swp`/`*.swo` gitignored; README env-table duplicate rows (NEXUS_MAX_QUERY_TERMS / NEXUS_SPELL_* / NEXUS_MAX_CANDIDATES) de-duplicated keeping the richer row; "Phase 10 -> Deployment" heading filled with an honest planned-not-implemented status; stray markdown link removed from `crawler/sitemap.py`'s docstring. `check_env_docs` green (29 documented vars). | `git ls-files` has no *.db/*.pyc (verified); README table now single-rowed. | — |
| P3-13 warnings + parity | **fixed / noted / deferred** (67596a1) | `TestHandler` -> `_TestHandler` (no pytest collection warning). Starlette TestClient httpx deprecation: NOT switched (moving to httpx2 would break the pinned httpx 0.28.1/starlette 1.7.0 stack — noted as upstream). slowapi `asyncio.iscoroutinefunction` DeprecationWarning: upstream in slowapi 0.1.10, not our code — noted. Python 3.12 parity: no 3.12 interpreter exists on this machine — **unverified locally**; the CI workflow (Ubuntu/3.12) is the parity check and its result is reported at the gate. | Suite green with rename; warnings summary unchanged otherwise. | — |

Environment notes recorded honestly:
- charset-normalizer was temporarily swapped 3.5.1 -> 3.4.6 for the P0-4
  matrix, then restored to the pinned 3.5.1 (requirements.txt untouched).
- During the session playwright 1.63.0 APPEARED in the USER
  site-packages (not installed by this work; not in requirements.txt). It
  made `test_render_js_fallback_without_playwright` skip locally
  ("playwright installed"); on CI (no playwright) it runs. No repo change
  was needed either way.
- The second scale_benchmark run (P2-8 "after") showed ~2.8x drift on
  the unrelated vocab_cold SQLite query — background machine load; the
  5,000-doc bulk timer was therefore re-run in a quiet window and is the
  number quoted for P2-9.

### WP11 CI saga (reported honestly, per the ground rules)

- Runs #8 (P0-1 push) and #9 (full WP11 push) were **RED** on Ubuntu/3.12
  while the whole suite was green locally on Windows/3.14: 16 failures,
  all `sqlite3.OperationalError: unable to open database file` in
  `importlib.reload(api)` teardowns.
- Root cause (read from the CI logs via the API): three test classes set
  `NEXUS_DB` to their own `tempfile.mkdtemp()` dir, never restored it,
  and their tearDown `rmtree`s the dir. On Windows the rmtree FAILS
  (SQLite file locks), so the dir survives and the stale env value stays
  openable — green by accident. On Linux the rmtree succeeds, leaving
  `NEXUS_DB` pointing at a deleted path; every later teardown that
  restores that ambient value then crashes at reload. The latent
  landmines predate WP11 (WP11's new files only disturbed the
  ambient-value chain that had kept them masked).
- Fix: save/restore `NEXUS_DB` in `TestQueryCacheApi`
  (tests/core/test_query_cache.py), the `tests/test_audit_validation.py`
  API class, and `TestApiRerankToggle` (tests/ranking/test_ranker.py) —
  the same hygiene `test_pagination_contract.py` already practiced.
  Commits 3c55bf3 (16 -> 2 CI failures) and 8a92495 (2 -> 0).
- Run #11 (8a92495): **GREEN — test + docker jobs both pass on
  Ubuntu/Python 3.12** (docker job = image build + non-root assertion +
  /health probe, which also stands in for the local docker probe this
  machine cannot run).

### WP11 final verification gate (executed 2026-10-05/06, Windows 11, Python 3.14.7)

| Gate | Result |
|---|---|
| Offline suite (`NEXUS_ENV=dev`, plain `python -m pytest -q`) | final count after P3-10 was **807 passed, 7 skipped** (the "800 passed" figure below was the pre-P3-10 run; WP12 re-verified from HEAD 3511b86 and measured exactly 807/7 — corrected here, see WP12) |
| Model-gated suite (`NEXUS_RUN_MODEL_TESTS=1`) | final count after P3-10 was **812 passed, 2 skipped** (the "805" below was the pre-P3-10 run; WP12 re-measured 812/2 from HEAD 3511b86) |
| Load smoke (`NEXUS_RUN_LOAD_SMOKE=1`) | **1 passed** (8.75 s) |
| Graph bench (`NEXUS_RUN_GRAPH_BENCH=1`, `tests/evaluation`) | **11 passed, 2 skipped** (1:20) |
| `scripts/dev/check_duplicate_tests.py` | **no duplicate test function names** |
| `scripts/dev/check_env_docs.py` | **env docs consistent: 29 documented vars** (was 25) |
| Golden keyword snapshot | untouched and green through every change |
| GitHub Actions (Ubuntu/Python 3.12) | run #11 **GREEN** (test + docker), after the two documented-and-fixed red runs #8-#10; final pushes #12/#13 also **GREEN** (test + docker) — head 611a901 |
| Docker `compose build` + `/ready` locally | **unverified locally** — Docker CLI shim present but no engine/Docker Desktop on this machine; the CI docker job (build + non-root + /health probe) is green |
| Python 3.12 local parity run | **unverified locally** (no 3.12 interpreter on this machine) — CI's green run on 3.12 is the parity evidence |

---

# WP12 � Independent re-verification of the external review (HEAD 3511b86) + remediation

An independent reviewer re-audited the repo at HEAD 3511b86 without
fastapi/pytest/httpx/Docker (could only run ~695 unittest-style tests).
WP12 re-verified every claim with the full environment, then fixed each
CONFIRMED finding test-first. Reproduction scripts:
`scripts/dev/wp12/`.

## Part A � claim verification (own evidence; command + output in scripts)

| Claim | Verdict | Evidence |
|---|---|---|
| P0-1 read-gating broken in fresh process | **NOT REPRODUCED (already fixed)** | `scripts/dev/wp12/a2_read_auth_fresh_process.py`: fresh uvicorn, `NEXUS_ENV=production NEXUS_API_KEY=k NEXUS_REQUIRE_AUTH_FOR_READS=1` -> `/search` 401 no key, 401 wrong key, 200 correct key, POST /documents 201 |
| P0-3 rate-limit bucket bypass | **NOT REPRODUCED (already fixed)** | `scripts/dev/wp12/a1_rate_limit_random_keys.py`: 70 reqs, 70 random X-API-Key values from one IP -> 10x 429 after the 60/min budget; verified key still 200 |
| R1 stale content hash after external delete | **CONFIRMED** | `r1_reviewer_exact.py` at HEAD: `1 h-gone`; at cb64c36 (worktree): `1 None`. User-visible: long-lived manager re-upsert of same text -> `unchanged` (skipped), vector never returns |
| R2 RRF ignores raw similarity; no default min_score; weighted top-1 pinned | **CONFIRMED** | `r2_rrf_ignores_similarity.py`: sim 0.92 vs 0.05 both -> 0.03279; nonsense query hits 0.578/0.341; weighted top-1 = 2.0 (= w_bm25+w_vector) for weak and strong signals alike |
| R3 N+1 per hybrid query | **CONFIRMED** | `r3_hybrid_n_plus_one.py` (1,500 docs, hash:384): 936 / 1,629 get_document calls (reviewer: 886/1,661) |
| R4 filtered search O(corpus) | **CONFIRMED** | `r4_filter_scan_cost.py`: type:pdf 13.0/61.7/175.9 ms at 500/1,500/3,000 docs (linear); cause pinned by `r4b_filter_cause.py`: 412 get_document calls on a 300-doc corpus (one full-content read per vector via `allowed`) |
| R5a per-add O(n) doc_id scan | **CONFIRMED** | `r5_merge_and_lock_costs.py`: per-add 1.87 -> 5.42 ms from 2K -> 5K rows (superlinear) |
| R5b reload fills buffers under _matrix_lock | **CONFIRMED** | `r5b_reload_lock_stall.py`: 50K-row reload holds the matrix lock 337 ms; concurrent search peak 33 ms vs 0.001 ms median |
| R6 single-byte legacy encodings mojibake as cp1252 | **CONFIRMED** | `r6_single_byte_mojibake.py`: 0/10 round-trips (cp1251/koi8_r/cp1253/cp1254/cp1250, short+long) |
| R7 render_js gaps | **CONFIRMED (code-level)** | fetcher.py:128-133 `if self.url_validator and ...` no-ops with validator=None; `page.route("**/*")` cannot see WS/SW; context had no `service_workers="block"` |
| R8 zip-bomb default too generous | **CONFIRMED** | `r8_small_check.py`: 20 MiB declared XML -> 223 s parse, 100 MiB tracemalloc peak (~5x amplification) -> the 512 MiB default admits ~2.5 GB RSS from a <1 MiB file; 166 MiB version exceeded 600 s |
| R9 review zip contained untracked junk; no packaging script | **PARTIAL** | working tree still holds untracked `nexus_search_frontier.db` (gitignored) + `__pycache__` � a naive zip would include them; `.git/COMMIT_EDITMSG.swp` NOT present now; no packaging script existed; CI hygiene rejected only `*.db/*.pyc` (not `*.swp`); `.gitattributes` already pinned LF via `* text=auto eol=lf` (Dockerfile/*.sh made explicit) |
| R10 "hybrid tops out at 0.03" is not a confidence signal | **CONFIRMED** | 2/61 = 0.0328 is the RRF formula ceiling for a doc ranked #1 in BOTH retrievers (R2 evidence); PHASE7_PLAN's unanswerable rationale was built on it -> fixed in WP12-2 |
| WP11 doc counts inconsistent ("800 passed" vs "800 -> 807") | **CONFIRMED** | real fresh runs at HEAD 3511b86: offline **807/7**, model-gated **812/2**; WP11 gate rows said 800/805 (pre-P3-10) � corrected above; README said 760/765 (stale) � updated to 829/834 (post-WP12) |

Unverified/limits: charset-normalizer 3.4.x behavior (only 3.5.1 installed
locally; CI pins 3.5.1); the exact bytes of the reviewer's zip (not
available) � only the working-tree preconditions were checked.

## Part B � fixes (test-first, one commit each)

| Item | Status | Commit | Tests added | Before -> after |
|---|---|---|---|---|
| WP12-1 (R1, R5b) reload REPLACES `_content_hashes`; buffers built OUTSIDE the lock and swapped under it; `get_content_hash` refreshes (upsert re-embeds after external delete); `_mem_version` guard so a concurrent in-process add can never be clobbered by a swap | **fixed** | bbae388 | `test_external_delete_clears_stale_content_hash`, `test_manager_reembeds_after_external_delete` (both failed pre-fix: stale `'h-gone'` / `'unchanged' != 'created'`) | `1 h-gone` -> `1 None`; manager upsert `'unchanged'` -> `'created'`, vector back in DB; matrix lock no longer held during 50K reload fill |
| WP12-2 (R2, R10) PHASE7_PLAN section 6 + unanswerable rewritten: fused RRF/weighted scores are rank/normalization artifacts, NEVER a refuse signal; gate = RAW signals (vector `min_score` cosine floor, BM25 raw-score floor, hybrid agreement), calibrated on the WP10 fixture + CI regression floor. Added: model-output sanitize/escape rule (P1-6 class), markdown image/link auto-render ban (exfiltration), slowapi in-process-limiter caveat for per-key token caps | **done (docs only, no RAG code)** | a7ba947 | n/a (plan text) | 0.03 retraction documented; refuse-gate contract now measurable |
| WP12-3 (R4) vector filter pushdown = ONE `Storage.get_documents_meta` read (no content column) + O(n) set-lookup mask in `VectorStore.search(allowed_ids=...)`; np.isin on object arrays rejected (measured 290 ms at 3K docs) | **fixed** | dd0e9a2 | `test_wp12_pushdown.py` (2; parity type:/lang:/-type:/NOT + read-counts meta==1/get_document==0; both failed pre-fix) | filtered hybrid at 3K docs 176 -> 85 ms; plain/filtered ratio 3x -> 1.3x; 412 -> 112 full-doc reads (remainder = R3 class) |
| WP12-4 (R3) every hybrid stage batched: `_vector_candidates` gates, `_merge_results` vector-only branch, semantic loop, `_diversify`, BM25 cache seeded from its own batch fetch + filter-only queries (meta scan + batch) | **fixed** | 6939ef3 | `test_wp12_n_plus_one.py` (2; get_document==0 and batches<=5; failed pre-fix at 100 calls) | per-query get_document: 936/1,629 -> **0**; batched reads <= 5 |
| WP12-5 (R5a) `_id_to_idx` doc_id->row dict maintained by merge/swap-remove/reload; add/remove O(1) | **fixed** | 8c6fe9d | `test_wp12_row_index.py` (3: merge-O1 timing � 2,000 in-place merges on a 10K matrix, scan path measured ~2.4 s, bound 0.5 s; 10K-add generous bound; invariant across ops; failed pre-fix AttributeError) | 2K in-place merges on 10K rows: ~2.4 s -> < 0.5 s; SQL commit now dominates add() (profile: 82%) |
| WP12-6 (R6) script-coherence codec scoring (cp1251/koi8_r/cp1253/cp1254/cp1250/cp1257 + iso8859-x); Latin specific-letter sets exclude cp1252-colliding letters; CJK guesses win within a +0.10 margin; chosen codec logged at INFO; residual limits documented (boundary-only Latin script letters, Icelandic �/�, very short samples) | **fixed** | 6097aad | `TestLegacySingleByteScripts` (6 methods / 10 subtests + cp1252 guards for m�/�; all failed pre-fix) | 0/10 -> **10/10** round-trips; TestShortWesternEncoding green (3.5.1) |
| WP12-7 (R7) render_js refused without url_validator (warn+disable) unless explicit `render_js_allow_private`; context created with `service_workers="block"`; WS via `route_web_socket` when the build has it (ws->http scheme mapping); docstring corrected; pipeline maps `allow_private_hosts` to the new flag | **fixed** | f73e070 | 4 fake-playwright tests (refusal, SW-block, WS route matrix, WS-absence; failed pre-fix). Two existing fallback tests adapted to the new constructor contract (`render_js_allow_private=True`), assertions unchanged | unvalidated-browser path no longer reachable by default |
| WP12-8 (R8) zip-bomb defaults: 64 MiB total (was 512) + NEW 32 MiB per-entry cap (`NEXUS_MAX_ENTRY_BYTES`); env overrides kept; .env.example updated (30 vars) | **fixed** | 68279fe | 4 tests (bounds pin 64/32, R8-shaped 75 MiB payload refused instantly � pre-fix it parsed for 600+ s, per-entry refusal, env override) | worst-case RSS ~2.5 GB -> ~320 MiB at python-docx's measured 5x amplification |
| WP12-9 (R9) `scripts/package.ps1` + `scripts/package.sh` build release zips via `git archive` ONLY; CI hygiene extended to `*.swp`; .gitattributes pins Dockerfile/*.sh LF explicitly | **fixed** | 592eb91 | packaging verified live: 225 entries, zero .db/.pyc/.swp/__pycache__/.git internals | no packaging script existed; CI hygiene blind to *.swp |
| WP12-10 (workflow.json expansion) | **SKIPPED � user decision 2026-10-09** | � | � | "skip it, move ahead" (the 75-node n8n stays as-is; reviewer marked it optional) |

## WP12 final verification gate (executed 2026-10-09, Windows 11, Python 3.14.7)

| Gate | Result |
|---|---|
| Offline suite (`NEXUS_ENV=dev`, `python -m pytest -q`) | **829 passed, 7 skipped, 21 subtests passed** (8:28) � was 807/7 at HEAD 3511b86; +22 net new tests, zero pre-existing tests loosened |
| Model-gated suite (`NEXUS_RUN_MODEL_TESTS=1`) | **834 passed, 2 skipped, 21 subtests passed** (7:05) � was 812/2 |
| Load smoke (`NEXUS_RUN_LOAD_SMOKE=1`) | **1 passed** (7.8 s) |
| Graph bench (`NEXUS_RUN_GRAPH_BENCH=1`, `tests/evaluation`) | **11 passed, 2 skipped** (1:37) |
| `scripts/dev/check_duplicate_tests.py` | **no duplicate test function names** |
| `scripts/dev/check_env_docs.py` | **env docs consistent: 30 documented vars** (NEXUS_MAX_ENTRY_BYTES added) |
| Scale benchmark (`python -m nexus_search.evaluation.scale_benchmark`) | 1K/5K/10K hybrid 61/985/2,083 ms; 5K->10K = 2.11x (linear; WP11's fix holds), semantic 1.6/2.6/4.3 ms (WP11: 3.9/4.9/6.1 � improved again); one run under heavy machine load, hybrid/keyword absolute numbers noisy |
| Skipped tests (all 7 offline) | 3x `test_embedders.py` (Requires sentence-transformers), `test_graph_benchmark.py:30` (full bench needs NEXUS_RUN_GRAPH_BENCH=1), 2x `test_semantic_benchmark.py:64/68` (st floors need NEXUS_RUN_MODEL_TESTS=1), `test_load_smoke.py:81` (needs NEXUS_RUN_LOAD_SMOKE=1) � all run green in their gated suites above |
| Docker `compose build` + `GET /ready` | **unverified locally** — Docker Desktop was launched fresh this session but its WSL engine never came up (`wsl -l -v` shows no distro; CLI API returns 500 over the named pipe). The CI docker job is the evidence: **GREEN** on the WP12 head (image build + non-root whoami assertion + boot + `/health` probe) |
| GitHub Actions (Ubuntu/Python 3.12) | run #15 on WP12 head e71c21e: **GREEN — both jobs (`test`, `docker`) success** (verified via the Actions API; CI additionally runs the graph-benchmark smoke, the authority on/off benchmark, the duplicate-test-name lint and the env-var documentation check) |

## Part C — Phase 7 readiness gate (verdict: READY)

- `docs/PHASE7_PLAN.md` updated per WP12-2 (commit a7ba947): fused scores
  reclassified as rank artifacts, refuse-gate = raw signals calibrated on
  the WP10 fixture + CI floor, output-sanitization/exfiltration rules,
  per-key-token-cap shared-state caveat.
- All P0 rows (WP11+WP12): fixed or NOT REPRODUCED-because-already-fixed
  (A1/A2 evidence above).
- All P1 rows: fixed (WP12-1 hash regression, WP12-2 plan).
- All P2 rows: fixed (WP12-3/4/5/6/7).
- P3 rows: WP12-8/9 fixed; WP12-10 skipped by user decision (optional per
  reviewer).
- CI green on GitHub Actions (Ubuntu/Python 3.12, both jobs) at the WP12
  head.
- B1 and B2 are done and CI is green: **the Phase 7 RAG implementation
  gate is open.**

---

# WP13 — Independent re-review remediation (blocking B6 + scale bench + ledger + sign-off)

Scope: the reviewer's re-audit of the WP12 zip found one blocking
regression (encoding, item 1), a benchmark-validity gap (item 2), open
ledger items (item 3), packaging/hygiene (item 4), unverified gates
(item 5), an n8n decision (item 6, user decision required — options
presented, NOT acted on) and the Phase 1-6 sign-off (item 7).

Baseline re-confirmed FIRST at the WP12 head b63bdb5 (ground rule 1):
**offline 829 passed, 7 skipped, 21 subtests (7:36)**, identical to the
WP12 gate; all 7 skips: 3x test_embedders (sentence-transformers),
test_graph_benchmark:30 (needs NEXUS_RUN_GRAPH_BENCH=1), 2x
test_semantic_benchmark:64/68 (need NEXUS_RUN_MODEL_TESTS=1),
test_load_smoke:81 (needs NEXUS_RUN_LOAD_SMOKE=1).

## Item 1 — BLOCKING: WP12-B6 encoding regression (ingestion/connectors/files.py)

Status: **FIXED** (commit ea10a5c, test-first: 454 test failures -> 0).

Root causes (both confirmed against the reviewer's exact samples before
fixing — `scripts/dev/wp13/b6_before_after.py`):

1. **(a) Baltic collision letters.** `_LATIN_SPECIFIC["baltic"]` = "
   ūėįųŗŪĖĮŲ" — every one of those letters sits on a byte that is a REAL
   cp1252 letter (byte table dumped programmatically: ū@0xFB=û, ė@0xEB=ë,
   į@0xE1=á, ų@0xF8=ø, ŗ@0xBA=º, Ū@0xDB=Û, Ė@0xCB=Ë, Į@0xC1=Á, Ų@0xD8=Ø).
   Two interior occurrences were enough to flip a file to cp1257 —
   independent of detector version (reproduced with a stubbed detector
   whose top guess was cp1252). The WP12 collision exclusion had been
   applied to the Central-European and Turkish sets only.
2. **(b) Mismatched denominators.** `_cjk_share` divided by ALL
   characters (spaces, digits, punctuation, Latin) while the
   `_legacy_script_codec` share divided by letters only: a spaced Korean
   sentence scored ~0.8 against a flat 1.0 for a mojibake decode, making
   the "+0.10 margin" meaningless.

Fix (minimal, measured at each step — probe scripts under
`C:\Users\Admin\AppData\Local\Temp\opencode\wp13_probe*.py` during
development; committed evidence scripts under scripts/dev/wp13/):

- Both shares now use ONE denominator: non-space, non-digit,
  non-punctuation characters (digits excluded — they carry no script
  information and would poison the Latin 0.9 / Cyrillic 0.85 floors for
  digit-heavy text, e.g. the reviewer's "2024年…" GBK sample).
- A legacy decode with >5% symbols among non-space chars (and >=2 of
  them) is rejected: CJK bytes read as single-byte legacy codecs measure
  5-46% box-drawing/symbol soup, real legacy text 0-2%.
- Baltic evidence reduced to the NON-COLLIDING letters only: **Ø/ø**
  (bytes 0xA8/0xB8 = ¨/¸ in cp1252, ˇ/¸ in cp1250, box-drawing in koi8_r).
  Æ/æ were additionally excluded because cp1250 puts Ż/ż on those bytes
  (0xAF/0xBF). cp1257 is otherwise accepted ONLY as the detector's own
  top guess — every other Baltic letter collides with real cp1252
  letters (â ç è ì û …).
- Hardening the fix exposed four further mojibake thieves, all measured
  and fixed in the same commit:
  - ISO-8859 tables are symbol-free, so CJK bytes ALSO read as coherent
    Cyrillic/CE under iso8859-5/-2 (share ~1.0): a case-folded
    script-common-letter coherence floor (0.40) rejects them — real
    Russian 0.48-0.89, real Greek 0.615, cross-codec soup 0.07-0.32.
  - Latin-family evidence now also needs a hit-density ratio >= 0.25 of
    high-byte letters (real Polish/Turkish/Baltic 0.33-0.75; CJK
    mojibake 0.07-0.16), and ONE interior ą/ł/ż/Ø/ø hit suffices — their
    bytes are cp1252 SYMBOLS, the same decisive logic as the Turkish ıİ
    rule (a digits-variant Polish file with a single ł+ś used to fall
    back to cp1252).
  - CJK candidates from the detector's own list are ranked by (share,
    common-character coherence, rank) instead of first-explaining:
    cross-codec decodes (Korean bytes as GBK, GBK as cp949, Greek as
    johab) tie or beat the correct codec on share but yield rare
    characters (correct decode 0.20-0.81 coherence, cross-codec
    0.00-0.54, correct always highest). euc_jis_2004 joined the rankable
    families (real euc_jp files top-guess big5 — measured).
  - BOM-less utf_16 cjk-path evidence now requires coherence >= 0.2 too:
    Latin text read as utf_16_be lands 0.68-0.91 cjk share (ASCII pairs
    decode into Han/Ext-A ranges) but ~0 coherence; genuine CJK utf_16
    keeps share ~1.0 / coherence 0.4+; the NUL-interleave rule is
    unchanged.
  - cp1252-clean bytes never settle for a junk single-byte top guess
    (measured: cp775 topped a French file at the WP12 head).
- Very short samples (<64 B) log the chosen encoding at INFO (never a
  silent guess).

Before/after evidence — every reviewer sample, three heads
(scripts/dev/wp13/b6_before_after.py; WP11 = 3511b86 worktree, WP12 =
b63bdb5 worktree, WP13 = current):

| Sample | WP11 head | WP12 head | WP13 head |
|---|---|---|---|
| Korean euc_kr/cp949 "한국어 텍스트입니다 테스트 문장" | OK cp949 | **koi8_r** | OK cp949 |
| Korean euc_kr/cp949 "안녕하세요 저는 개발자입니다…" | OK cp949 | **cp1250** | OK cp949 |
| GBK/gb18030 spaces+Latin "Python 是一种 编程语言…" | OK gb18030 | **cp1250** | OK gb18030 |
| GBK/gb18030 digits "2024年 我们 发布了 3 个…" | OK gb18030 | **koi8_r** | OK gb18030 |
| Danish cp1252 (æ, ø, å x2+) | OK cp1252 | **cp1257** ("Hųyt") | OK cp1252 |
| French cp1252 (û x3) | OK cp1252 | **cp1257** ("sūr") | OK cp1252 |
| Dutch cp1252 (ë x4) | OK cp1252 | **cp1257** ("Zoė") | OK cp1252 |
| Russian koi8_r ~250 B paragraph | OK koi8_r | OK koi8_r | OK koi8_r |
| Russian koi8_r mixed Latin (the ->cp1257 class) | OK koi8_r | OK koi8_r | OK koi8_r |

WP12 head: **12/14 mojibake; WP13 head: 0/14** — on charset-normalizer
3.5.1 AND 3.4.6. The Russian rows passed at WP12 by detector-rank luck
(the reviewer's paragraph ranked cp1257 higher); WP13 pins them
structurally: a stubbed detector leading with cp1257 loses to the
Cyrillic coherence scan (test_russian_paragraph_never_flips_to_cp1257).

Tests added (tests/ingestion/test_wp13_encoding.py; all failed pre-fix —
454 failures — and pass post-fix; existing P0-4/B6 tests untouched):
- Table: reviewer samples + 11 languages x their codecs (cp1252, euc_kr,
  cp949, gbk, gb18030, big5, shift_jis, euc_jp, cp1251, koi8_r, cp1253,
  cp1254, cp1250) x short/~250 B/~2 KB x with/without spaces, digits,
  punctuation — **75 rows, exact round-trip**.
- Differential vs WP11 (fixture tests/ingestion/wp13_wp11_baseline.json,
  generated by scripts/dev/wp13/gen_wp13_fixture.py against a 3511b86
  worktree): **51 rows WP11 decoded correctly; zero regressions** (the
  24 WP11-failed rows are headroom WP13 also fixed — e.g. every
  Russian/Greek/Turkish/Polish row, Japanese euc_jp, French cp775).
- Detector-independent: from_bytes stubbed to fake top guesses (cp1252,
  cp949, gb18030, mac_latin2, utf_16_be, big5) — correct output for
  every row under every stub (Japanese rows additionally get cp932 +
  euc_jis_2004 in the list: without a Japanese codec in the candidate
  list the correct answer does not exist — documented limit, not guessed
  around).
- cp1257 rules pinned via stubs (top-guess acceptance, non-colliding Øø
  evidence, Danish/French/Dutch/Russian never flip).
- CI: the test job is now a charset-normalizer matrix (3.5.1 pinned +
  3.4.6) — both green locally; see the gate below for the Actions run.

Documented residuals (in the files.py docstrings): genuine cp1257
Baltic files rely on the detector's top guess (their letters are all
cp1252-colliding — charset-normalizer top-guessed cp1258 for the
Latvian sample, measured); CJK-vs-CJK ambiguity when the wrong CJK
codec also fully explains the bytes (indistinguishable without language
modeling — the detector's ranking carries those); interior ¹³¿ in
no-space compounds ("50m³Wasser") can still read as cp1250 (pre-existing
WP12 trade); all-caps-Latin-as-utf_16 residual.

## Item 2 — Scale benchmark: selective-query scenario

Status: **DONE** (commit 6179d0c... see git log; test-first).

The reviewer's finding: every generated document contained "python
search", so each query matched 100% of the corpus and latency grew
linearly BY CONSTRUCTION (2,083 ms at 10K on a loaded Windows box vs
47 ms at 2K on a quiet Linux box were both every-doc-matches runs).
The benchmark now runs TWO scenarios per size: **match_all** (the old
corpus, labelled "worst case, every doc matches" in every row) and
**selective** (deterministic seeded Zipfian corpus — 50 topics, s=1.2,
500-word secondary vocabulary; query = topic + rare-secondary; match
band 1-5% asserted at build time).

Measured at the WP13 head (machine state, honestly recorded: Intel
i3-7020U 2.30 GHz, 4 GB RAM, background VS Code + Chrome active —
"quiet" was not fully achievable; single run, nothing else measured in
parallel):

| Scenario | Docs | hybrid ms | keyword ms | semantic ms | match |
|---|---|---|---|---|---|
| match_all (worst case) | 1K | 61.2 | 34.2 | 1.7 | 100% |
| selective | 1K | 3.1 | 0.9 | 1.9 | 2.7% |
| match_all (worst case) | 5K | 883.2 | 693.5 | 4.2 | 100% |
| selective | 5K | 9.8 | 3.1 | 2.6 | 3.0% |
| match_all (worst case) | 10K | 1383.8 | 1249.4 | 3.4 | 100% |
| selective | 10K | **27.0** | 13.6 | 3.4 | 2.8% |

Selective hybrid at 10K = **27.0 ms << the ~200 ms threshold** — the
reviewer's profiling conditional does not fire; no hot-path changes
made (the match_all x22.6 growth is the labelled every-doc-matches worst
case; selective growth is x8.7 for x10 docs). Tests:
TestSelectiveBenchmarkScenario (2) — the 1-5% band and the deterministic
construction are pinned at smoke size.

## Item 3 — Open-items ledger (WP0-WP12) and cheap fixes

Status: ledger below; the two fix-now items fixed test-first (commit
3f9bc96); the rest decision-tabled.

| Item | Sev | Decision | Reason |
|---|---|---|---|
| related_searches trigram fallback scanned the whole vocabulary per request (WP11 P3) | P3 | **fixed (WP13-3)** | <30 min: inverted gram->terms index built once per vocabulary change; per-request cost O(query grams x postings); output parity pinned by an in-test naive reference |
| semantic_benchmark cleanup `except: pass` (WP11 P3) | P3 | **fixed (WP13-3)** | <30 min: `_close_quietly` — per-closer logging, remaining closers still run (WP12-1b class); the finally-path had bundled 4 closers in one try (a first failure leaked the rest) |
| charset-normalizer 3.4.x behavior unverified (WP12 Part A) | P2 | **fixed (WP13-1)** | CI matrix leg 3.5.1+3.4.6; both green locally; encoding tests detector-independent |
| Docker compose build + /ready locally (WP11/WP12 gates: unverified) | P2 | **verified (WP13-5)** | engine recovered this session: build OK, GET /ready -> 200 {"ready":true} |
| Python 3.12 local parity (WP11 P3-13: unverified) | P2 | **verified (WP13-5)** | full suite on 3.12.14: 844/7/614 — identical outcomes to 3.14; only warning volume differs (upstream slowapi deprecation, 3.14-only) |
| single-worker in-process rate limiter (per-key token caps) | P1-ops | defer to Phase 8 | architectural: needs shared state (SQLite table or Redis) — PHASE7_PLAN §4 documents the operational limit and the requirement |
| render_js residual DNS-rebinding (Chromium resolves its own DNS) | P2 | accept, documented | cannot be fixed inside Playwright; docstring + README carry the "trusted crawls only" contract (WP12-7) |
| exact-hostname domain-diversity limit (~10 subdomains saturate) | P2 | accept, documented | PSL dependency deliberately not added (WP4 decision); farms still bounded by the 1000/page cap |
| reindex --shadow materializes corpus in RAM | P3 | defer | CLI maintenance path, bounded by corpus size; a streaming rewrite is Phase 7+ work, not <30 min |
| /documents/bulk worst-case body size (500 x 10 MB) | P3 | defer | proxy-level body cap recommended at deployment (audit's own recommendation); a code cap changes API contract |
| frontier add() commits per URL | P3 | defer | per-link fsync cost during crawls, not correctness (single-writer by design); batching changes durability semantics |
| web-scale authority quality | P2 | accept, documented | no production spam farm exists to validate against; stated in SPEC.md |
| slowapi + starlette deprecation warnings | P3 | accept, upstream | not our code (slowapi 0.1.10, starlette 1.7.0 pins); noted since WP11 P3-13 |
| WP12-10 n8n workflow expansion | optional | **user decision 2026-10-09: keep as-is** | 75 nodes stay; options re-presented in Item 6 below — awaiting the user's answer; not acted on |
| WP8-phase-deferred: click signals, LTR training, A/B analysis, Prometheus/structured logging, multi-process politeness + distributed rate limiting | planned | defer as designed | owned by Phase 7 / 7+ / production-traffic / Phase 10 / Phase 8 respectively |

## Item 4 — Packaging and repo hygiene

Status: **DONE** (no repo changes required — verification + local
cleanup). `git status` clean at every WP13 commit; no tracked
`*.db/*.pyc/*.swp` (`git ls-files` check); `.github/workflows/test.yml`
present (now with the charset matrix); `.gitattributes` pins LF for
Dockerfile and `*.sh` explicitly. The stray local artifacts the review
zip shipped — `nexus_search_frontier.db` and
`.git/.COMMIT_EDITMSG.swp` — are DELETED; `.gitignore` already covers
`*.db`, `*.db-wal/-shm/-journal`, `*.swp`, `__pycache__/`. Hand-off zip
built ONLY via scripts/package (git archive) and verified: **234
entries, zero** `nexus_search_frontier.db`/`__pycache__`/`.git`
internals/`.swp`/`.pyc`/`.db`.

## Item 5 — Verification of previously-"unverified" gates

| Gate | Result |
|---|---|
| `docker compose build` + `GET /ready` locally | **VERIFIED** (was unverified since WP11): after `docker pull docker/dockerfile:1` (transient builder-DNS failure resolved), `docker compose build` -> "Image nexus-search-api Built"; `docker compose up -d` -> `GET /ready` **200 `{"ready":true}`** after ~10 s; `GET /health` 200 `{"status":"ok",...}` |
| Python 3.12 locally | **VERIFIED**: uv-installed CPython 3.12.14 + pinned requirements (CPU torch wheel); `NEXUS_ENV=dev NEXUS_EMBEDDER=hash:384 python -m pytest -q` -> **844 passed, 7 skipped, 1 warning, 614 subtests (9:59)**. Differences from 3.14.7 (844/7/1471 warnings/614, 7:30): NONE in outcomes — only the warning volume (slowapi's deprecated `asyncio.iscoroutinefunction` fires on 3.14 only; upstream, documented since WP11 P3-13) |
| CI (Ubuntu/3.12, both charset legs + docker) | run result at the WP13 head recorded in the gate table below |

## Item 6 — n8n workflow (DECISION REQUIRED — presented, NOT acted on)

automation/nexus-search-workflow.json ("Nexus Search Core", 75 nodes)
reimplements pipeline stages on Postgres/Qdrant/OpenAI instead of
calling the Python API. The original ask was 300-350 nodes. Options:

- **A. Keep as-is** (current state; WP12 already recorded the user's
  "skip it, move ahead" from 2026-10-09). Effort: 0. The 75-node
  reference workflow stays automation-only material.
- **B. Expand to the requested 300-350 nodes calling the Nexus API over
  HTTP** (crawl -> ingest -> index -> search -> rerank stages as HTTP
  request nodes against this repo's documented endpoints; replace the
  Postgres/Qdrant/OpenAI reimplementation). Effort estimate: 1-2 days
  (design the stage graph, ~250-300 new nodes, HTTP node configs,
  credentials wiring, end-to-end test against a running local API);
  cannot be validated in CI (n8n has no test harness in this repo) —
  would be smoke-verified manually only.
- **C. Drop it** (delete automation/, note the removal in README).
  Effort: <30 min.

WP13's recommendation: A (it is optional per the reviewer, and B adds a
large untestable artifact). **Awaiting the user's decision.**

## Item 7 — Phase 1-6 sign-off and Phase 7 hand-off

- README test counts + status table synced to the real WP13 runs (see
  below); docs/PHASES_1-6_CHECKLIST.md written (one line per phase,
  only verified claims ticked).
- docs/PHASE7_PLAN.md re-read and CONFIRMED unchanged-accurate: the
  refuse-gate uses RAW signals (vector min_score = raw cosine floor;
  BM25 un-normalized raw-score floor; fused RRF/weighted scores are
  rank/normalization artifacts, never a confidence signal — §"answer
  refusal gate"); model output is sanitized/escaped and markdown
  image/link auto-rendering from model output is FORBIDDEN (§2b); the
  slowapi in-process limiter caveat (per-worker token caps) is stated
  (§4). Nothing contradicts the code — not edited.
- Tag `v0.6.0-phases-1-6` + `phase-7` branch created after CI green.

## WP13 final verification gate (executed 2026-10-09, Windows 11, Python 3.14.7 unless stated)

| Gate | Result |
|---|---|
| Offline suite (`NEXUS_ENV=dev`, `python -m pytest -q`) | **844 passed, 7 skipped, 1471 warnings, 614 subtests passed** (7:30) — was 829/7/21 at the WP12 head; +15 net new test functions (9 encoding + 2 scale-benchmark + 2 suggestions + 2 benchmark-cleanup), zero pre-existing tests loosened |
| Model-gated suite (`NEXUS_RUN_MODEL_TESTS=1`) | **849 passed, 2 skipped, 614 subtests** (6:55) — was 834/2 |
| Load smoke (`NEXUS_RUN_LOAD_SMOKE=1`, ambient `NEXUS_ENV=dev`) | **1 passed** (6.85 s) |
| Graph bench (`NEXUS_RUN_GRAPH_BENCH=1`, tests/evaluation) | **15 passed, 2 skipped, 3 subtests** (1:35) |
| Skipped tests (all 7 offline) | unchanged classes: 3x embedders (ST), graph-bench gate, 2x semantic-st floors gate, load-smoke gate — all green in their gated suites |
| `scripts/dev/check_duplicate_tests.py` | **no duplicate test function names** |
| `scripts/dev/check_env_docs.py` | **env docs consistent: 30 documented vars** |
| Scale benchmark (both scenarios) | selective 1K/5K/10K hybrid 3.1/9.8/**27.0** ms (match 2.7-3.0%); match_all worst case 61/883/1384 ms; table above |
| Golden keyword baseline | **byte-identical** through every WP13 change (test green) |
| Docker `compose build` + `GET /ready` | **verified locally** (was unverified since WP11): build OK; /ready 200 `{"ready":true}`; /health 200 |
| Python 3.12 local parity | **844/7/614 — identical to 3.14** (warnings-only delta, upstream) |
| GitHub Actions (Ubuntu / 3.12, charset-normalizer **3.5.1 + 3.4.6** matrix legs + docker job) | pushed with the WP13 head; result recorded in the addendum below after the run completed |

Phase 1-6 sign-off: **DONE** (checklist at docs/PHASES_1-6_CHECKLIST.md).
Phase 7 hand-off: tag `v0.6.0-phases-1-6`, branch `phase-7`.

### WP13 CI addendum (recorded after the push)

**Run #17 on the WP13 head 1347ce9: GREEN — all three jobs succeeded**
(verified via the GitHub Actions API, 2026-10-09T18:17Z):

- `test (py3.12, charset-normalizer 3.4.6)` — success: hygiene check, FULL
  suite, both lints, graph-benchmark smoke, authority benchmark.
- `test (py3.12, charset-normalizer 3.5.1)` — success: same steps.
- `docker` — success: image build, non-root `whoami` assertion, boot +
  `/health` probe.

The blocking-encoding requirement holds in CI: the encoding tests pass
on BOTH charset-normalizer legs of the matrix, exactly as they did
locally (Windows/3.14 on 3.5.1 and 3.4.6).

# WP14 — Pre-Phase-7 hardening: STEP 0 report (audit BEFORE fixes)

Scope: install and RUN everything (the prior review was static-only), confirm
or refute its hypotheses H1–H13, discover what reading could not, then fix
test-first. Full command/output evidence: docs/WP14_WORKLOG.md (Step 0
sections). Baseline re-confirmed before any change: offline **844 passed,
7 skipped, 614 subtests** (py3.12.14 + py3.14.7 identical), model-gated
**849/2**, load smoke 1, graph bench 15/2, both lints clean, mutations
7/7 killed, FastAPI route matrix + boot-refusal + 50-way concurrency clean.

| # | Finding | Evidence (command → real output; details in WP14_WORKLOG) | Sev | Status | Action |
|---|---|---|---|---|---|
| H1 | min_score exists only in VectorStore.search; unreachable via HybridSearch.search_page and /search | read: vector_store.py:384/440 vs hybrid_search.py:209-217/407 (no param); failing test written test-first in Item 1 | P1 | confirmed | FIX (Item 1) |
| H2 | /search is one ~204-line function (api.py:404–607) | read + measured line span | P2 | confirmed | FIX (Item 2, pure refactor) |
| H3 | Dockerfile bakes MiniLM into the BUILDER stage only; runtime image has no model, no HF_HOME, runs as appuser → st: boot would download/hang at start | Dockerfile L14-18 vs L21-41; docker boot UNVERIFIED-locally (engine down) | P1 (ops honesty: README promises "pre-baked") | confirmed | FIX (Item 3) |
| H4 | docker-compose passes only 6 of ~31 NEXUS_* vars | docker-compose.yml environment block | P2 | confirmed | FIX (Item 4) |
| H5 | Checklist names files that don't exist: 	ests/ranking/test_ab.py, 	est_query_parser.py, 	est_hybrid*.py; robots tests actually live in 	est_politeness.py; A/B covered indirectly in 	est_ranker.py | Test-Path matrix: MISSING×2, no glob hits | P2 | confirmed | FIX (Item 5) |
| H6 | ersion="0.4.0" vs tag v0.6.0; empty __init__.py; no pyproject.toml; test deps in production requirements.txt | api.py:73; coverage (0 stmts); git ls-files; requirements.txt §Testing | P3 | confirmed | FIX (Item 6a/6b) |
| H7 | evaluation/rerank_benchmark.py unreferenced, 0% coverage | coverage report 0%; BUT it RUNS (exit 0, full metrics) — "may be broken" refuted | P3 | confirmed | FIX (Item 6c: smoke test + docs) |
| H8 | Env docs drift: README missing NEXUS_CORS_ORIGINS/MAX_ENTRY_BYTES/MAX_INGEST_BYTES/OCR/RERANK_WEIGHT_CLICK/STEMMING; NEXUS_STOPWORDS absent from .env.example; "checker checks only documented vars" | README var-grep diff vs code-grep; check_env_docs.py source DOES scan code (claim refuted); spell-budget trio covered by "etc." row | P2 (+NEW N2 below) | partial | FIX (Item 6d) |
| H9 | CRLF + non-ASCII dash | git ls-files --eol: 5 tracked files worktree-CRLF/index-LF → renormalize NOT warranted; .gitattributes byte 151 (em dash) confirmed; AUDIT_REMEDIATION.md/smoke_check.py LF (refuted); seeds.txt untracked, 1 stray CR (refuted) | P3 | partial | FIX (Item 6e, worktree + dash only) |
| H10 | signals.py says click logs are "Phase 7"; PHASE7_PLAN defers click-feedback learning to 8+; README Phase 5 lacks interface-only tags | signals.py:11,163,176 vs PHASE7_PLAN:191; README:382,391 | P2 (doc honesty) | confirmed | FIX (Item 6f) |
| H11 | scripts/dev/wp12/a1_*,2_* leave wp12_a*_test.db* in repo root | artifacts present (10/6); scripts use relative DB paths | P3 | confirmed | FIX (Item 6g) |
| H12 | .github/workflows/test.yml exists and matches the audit log (charset matrix, docker job, lints, hygiene) | file read; "absent from zip" premise stale (matches WP11 P0-2) | — | confirmed | none |
| H13 | n8n workflow: Qdrant Upsert id: chunk.index (cross-doc collision); placeholder Qdrant URLs; SSRF hostname regex (misses 172.16/12, IPv6, numeric IPs, redirects, rebinding); robots prefix-only; RAG prompt unfenced/no injection stripping; confidence 0.4+0.15×sources not a retrieval signal | node bodies extracted programmatically (75 nodes, JSON valid, connections resolve, no embedded secrets) | P2 (reference artifact) | all confirmed | FIX (Item 9) |
| N1 | NEW: 	est_crawl_delay_holds_with_many_workers flakes under CPU load (2 fails / 3 loaded runs incl. -n auto; 0/10 isolated; CI serial ×17 green). Politeness implementation correct (component test pins slot spacing); server-thread recording jitter is the mechanism | mutation + isolation matrix in worklog | P2-ops | NEW | ledger + owner decision (test rewrite forbidden by WP14 rules) |
| N2 | NEW: NEXUS_STOPWORDS is promised by tokenizer.py docstring ("=1 to enable") but read NOWHERE in code; stopword removal is unimplemented | repo-wide grep: 2 hits, both doc/allowlist text | P1 (documented-but-nonexistent knob) | NEW | FIX (Item 6d: correct docstring; do NOT document the lie in .env.example) |
| N3 | NEW: check_env_docs ALLOWLIST masks NEXUS_OCR/NEXUS_STOPWORDS rather than documenting them | check_env_docs.py:14-20 | P3 | NEW | FIX (Item 6d) |
| N4 | NEW: files-connector symlink probe UNVERIFIED locally (Windows privilege WinError 1314); no remote path input exists (operator-scoped) | probe output | P3 | NEW | document |
| N5 | NEW: 7 routes declare no esponse_model (POST/DELETE /documents, bulk, suggest, related, graph/*, health/ready/metrics) | route decorators | P3 | NEW | ledger |
| C1 | Coverage gaps <85%: backup.py 41% (CLI main 0%), query_parser 84%, vector_store 84% (min_score path untested — becomes tested in Item 1), crawler/cli 60%, fetcher 83% (playwright-gated), pipeline 84%, security 84%, files.py 81%, mime.py 76%, benchmark runners (0-81%, env-gated by design) | --cov --cov-branch term-missing | P2 | NEW | FIX (Item 7: add tests for behavior-bearing gaps; document why-not for env-gated/operator surfaces) |
| C2 | Mutations: ALL 7 spot-check targets killed (require_api_key, _rate_limit_key, _blocked_ip, dns pinning, chunk cascade, zip-bomb, size guard) | mutation harness, git clean after | — | NEW (positive) | none — guards are test-pinned |
| C3 | FastAPI audit: boot matrix, 13-route × 3-key matrix (dev/prod/read-auth), malformed-input battery, 429+Retry-After+X-RateLimit, CORS off/on, docs 404 prod / 200 dev, OpenAPI complete, no secret leaks, no internal-state leaks, 50-way concurrency + writes zero 5xx/lock errors | probe scripts (worklog §0.4) | — | NEW (positive) | none |

Fix items follow (test-first, one commit each). No RAG code is written by WP14.

## WP14 — fixes: per-item log (test-first, one commit each)

| Item | Status | Root cause -> fix | Before -> after | Commit | Tests |
|---|---|---|---|---|---|
| 1. min_score end-to-end | **done** | VectorStore.search(min_score) existed but no caller above passed it (H1). Plumbed search_page/_vector_candidates(min_score=...) (raw cosine, vector side only; None=-1.0 sentinel) + /search?min_score= (ge/le [-1,1], in the cache key, applied to the facet pass). Raw m25_score/ector_score pinned through fusion AND rerank incl. None cases. | 12 red tests pre-fix -> 14 green; golden byte-identical; OpenAPI documents the param | fc41c95 | tests/core/test_min_score.py (14) |
| 2. _run_search extraction | **done** | /search was one ~204-line function (H2). Body moved to module-level _run_search(...); endpoint = decorators + signature + one call. Zero behavior change (validation order, errors, status codes preserved). | gate set (golden, api, pagination, dos, shared_searcher, boolean, min_score, query_cache: 145) green with NO test edits; full suite 870 | 5bf68b4 | tests/core/test_run_search.py (13) |
| 3. Dockerfile baked model | **done (build verified by CI job only)** | Model downloaded into builder root's ~/.cache; runtime copied only /opt/venv (H3). HF_HOME=/opt/hf in builder, COPY --from=builder /opt/hf, chown appuser, HF_HOME+HF_HUB_OFFLINE=1 in runtime; bake string now the exact st:sentence-transformers/all-MiniLM-L6-v2 spec. | pre-fix: st: boot would download/hang offline; post-fix: model ships in image. Local docker UNVERIFIED (engine down all session, Docker Desktop launch attempted); CI docker job carries it | e9c15ae | (container probes in CI; compose config parse verified locally) |
| 4. compose env pass-through | **done** | Only 6 of ~31 NEXUS_* vars carried (H4). Optional env_file: .env (required:false) added; fail-closed NEXUS_API_KEY line and explicit defaults untouched. | docker compose config proves CORS/STEM/LLM-key vars from .env reach container env; missing key still errors | a7e632c | verification live (no unit-testable surface) |
| 5. A/B tests + checklist accuracy | **done** | No dedicated test file; checklist named 4 nonexistent files (H5). tests/ranking/test_ab.py pins assign_variant (determinism, order-flip invariance, 45-55% band, 5-way reach, empty raise), ExperimentLog recording, purge_older_than (aged-row deletion, idempotence, days>0 raise) — the PHASE7_PLAN PII-retention precedent. Checklist column rewritten to real files; scripts/dev/check_checklist_paths.py lint (full paths + bare names; negative control fails correctly) wired into CI. | 'MISSING tests/ranking/test_ab.py' -> exists+green; lint: 'checklist test files all exist: 6 full paths, 38 bare names' | ed1939c | tests/ranking/test_ab.py (9+5 subtests) |
| 6a. __version__ | **done** | FastAPI version hard-coded 0.4.0 vs tag v0.6.0 (H6). 
exus_search/__init__.py now owns __version__ = '0.6.0'; api imports it. | app.version 0.4.0 -> 0.6.0 | 3868bd8 | tests/core/test_version.py (2) |
| 6b. deps split | **done** | Test deps shipped in production requirements.txt (H6). requirements-dev.txt created (pytest, pytest-xdist, pytest-cov, httpx — grep-verified zero runtime imports); CI installs both; Docker runtime installs requirements.txt only. | fresh venv installed from the split files and ran the entire gate battery | d779bd2 | (install is the test) |
| 6c. rerank_benchmark | **done (kept, not deleted)** | H7 refuted 'may be broken' (exit 0) but confirmed 0% coverage + zero references. Kept: it is the Phase 5 A/B quick-check the framework exists for. Smoke test + README mention. | coverage 0% -> 86% | fc69003 | tests/evaluation/test_rerank_benchmark.py (1) |
| 6d. env docs | **done (adapted)** | H8 + NEW N2: README missing 6 var rows; NEXUS_STOPWORDS promised by a tokenizer docstring but READ NOWHERE — documenting it in .env.example would document a lie, so the docstring was corrected instead (stopword removal is NOT implemented; a real mode needs its own index-affecting work package). check_env_docs ALLOWLIST trimmed to test-only gates. README spell-row expanded to name all four spell vars. | README env table 23 -> 30 rows, no duplicates; check_env_docs green (30 documented) | fc69003, 5ed7f5f | lints |
| 6e. line endings + dash | **done** | H9 partial: 5 tracked files had CRLF worktree copies (index LF) -> re-smudged to LF via delete+checkout; no renormalize (index already LF); .gitattributes em dash (byte 151) -> ASCII. git ls-files --eol: zero w/crlf remain. AUDIT_REMEDIATION.md/smoke_check.py LF (H9 claim refuted); seeds.txt untracked (refuted). Golden + wp13 fixture blobs unchanged. | w/crlf 5 -> 0; nonascii bytes in .gitattributes 1 -> 0 | 5ed7f5f | git ls-files --eol |
| 6f. click/LTR phase drift | **done** | H10: signals.py/ranker.py/.env.example/README/SPEC/PHASE6_PLAN said click data arrives 'Phase 7' — PHASE7_PLAN defers click-feedback learning to 8+ and plans NO click data source. All seven spots now say Phase 8+; README Phase 5 rows tagged interface-only. | 7 drifted doc spots -> one consistent story | 5ed7f5f | doc consistency (grep re-run) |
| 6g. wp12 a1/a2 litter | **done** | H11: scratch DBs went to repo root via relative paths. Both scripts now mkdtemp. Stale wp12_a*_test.db{,-wal,-shm} litter (10/6) deleted from the worktree. | repo root: 6 litter files -> 0 | 5ed7f5f | scripts re-runnable (paths verified) |
| 6h. packaging | **done** | scripts/package.ps1 (git archive only) run at the WP14 head: 240 entries, zero .db/.pyc/.swp/__pycache__/.git junk. | zip clean | (no repo change needed) | — |
| 7. Step 0.3-0.5 gaps | **done** | Coverage <85% on behavior-bearing modules + uncovered function-level gaps. Tests added: backup CLI (41->95%), query_parser edges (84->98%), vector_store legacy-drop/factory (84->87%), security resolver/pinning edges all-DNS-mocked (84->97%), mime sniffing edges (76->91%), crawler CLI dispatch smoke (60->86%), seed_from_sitemap 0->covered, plus Items 1/5/6c tests. Remaining <85% documented: fetcher 83% (playwright-gated branches; WP12 fake-playwright tests cover the reachable paths), files.py 81% (tesseract-gated OCR + reader exception fallbacks; the zip/size/mime guards are mutation-killed), evaluation runners 44-83% + semantic __main__ 0% (env-gated benchmarks by design — run in CI via NEXUS_GRAPH_BENCH_SMOKE etc.). Mutations: 7/7 killed pre-fix, none needed new tests. | total coverage 87% -> 89% (branch on); suite 844 -> 944 | (per-area commits) | 63 new tests across 7 files |
| 8. Phase 7 guard rails (docs) | **done** | PHASE7_PLAN §4/§7 now bind: /ask + /ask/stream key-gated UNCONDITIONALLY (never inherit opt-in _READ_AUTH); per-worker limiter caveat marked must-copy into Phase 7 deployment docs; LLM provider behind a small interface with a stub (CI needs no key/network; stub doubles as injection oracle). | plan updated, no code | (WP14-8 commit) | — |
| 9. n8n workflow | **done** | H13 all confirmed. Qdrant Upsert id: chunk.index -> deterministic UUID-shaped f(content-hash, chunk-index) (collision killed); n8n.md labeled 'reference only: separate Postgres/Qdrant/OpenAI implementation, NOT the Phase 7 RAG path' with the full limits list (SSRF regex gaps incl. 172.16/12 + IPv6 + rebinding, robots prefix-only, RAG unfenced, confidence formula not a retrieval signal). JSON validated: parses, 75 nodes, all connections resolve by name, no secrets embedded. 300-350-node expansion stays a DEFERRED USER DECISION (ledger; do not act). | cross-doc chunk-N overwrites -> unique ids; sample: aaaaaaaa-aaaa-5aaa-8aaa-aaaaaaaa0000 vs bbbbbbbb-... | (WP14-9 commit) | validation script output in worklog |

## WP14 final gate (executed 2026-10-11, FRESH venv %TEMP%\opencode\wp14-venv-fresh, Python 3.12.14)

| Gate | Before (Step 0, same machine) | After (fresh venv, WP14 head) |
|---|---|---|
| Offline suite | 844 passed, 7 skipped, 614 subtests (run 2; run 1 had the N1 flake) | **944 passed, 7 skipped, 623 subtests** (6:26) — +100 tests, 0 failed, same 7 skips |
| Model-gated suite | 849 passed, 2 skipped | **949 passed, 2 skipped, 623 subtests** (6:39) |
| Load smoke | 1 passed | **1 passed** (6.34 s) |
| Graph bench | 15 passed, 2 skipped | **16 passed, 2 skipped, 3 subtests** (69 s; +1 = the new rerank_benchmark smoke lives in tests/evaluation) |
| check_duplicate_tests | no duplicates | **no duplicates** |
| check_env_docs | 30 documented vars | **30 documented vars** (trimmed allowlist, tighter contract) |
| check_checklist_paths | (did not exist) | **checklist test files all exist: 6 full paths, 38 bare names** |
| rerank_benchmark | runs, exit 0 (unreferenced, 0% cov) | **exit 0, identical metrics; smoke-tested + README-referenced, 86% cov** |
| Coverage (branch on) | 87% total, 726 missed | **89% total, 569 missed** |
| Golden keyword baseline | byte-identical | **byte-identical** (git diff phase-7..HEAD -- tests/golden/keyword_baseline.json empty) |
| FastAPI route matrix (3 boots) | all as documented | **all as documented** (re-run from the fresh venv; min_score in OpenAPI; 429/Retry-After/X-RateLimit intact; CORS off/on; docs 404 prod) |
| Boot refusal + 50-way concurrency | RuntimeError refused; 0 5xx | **same** (re-run) |
| Python 3.14 parity | 844/7 identical | not re-run at the head (CI's 3.12 legs are the parity carrier; 3.14 runs during Step 0 matched 3.12 exactly) |
| Docker build + --network none ST boot | engine down | **UNVERIFIED-locally** (engine down the entire session; Docker Desktop launch attempted, never came up). CI docker job (build + non-root + /health) is the carrier; compose config + env_file pass-through verified locally |
| CI on WP14 head | — | branch pushed; run number recorded in the addendum below (or UNVERIFIED if the run could not be observed) |

## WP14 updated open-items ledger

| Item | Sev | Decision | Reason |
|---|---|---|---|
| N1 crawl-delay timing flake under load | P2-ops | **owner decision required** (test rewrite forbidden by WP14 rules) | 2 fails / 3 loaded local runs (serial + xdist), 0/10 isolated, CI serial x17 green; politeness implementation correct (component test pins slot spacing); the server-thread recording jitter is environmental. Options: (a) leave (CI unaffected), (b) rewrite the test to measure dispatch-side times (changes the measurement point; needs owner sign-off), (c) raise the 0.25 s tolerance (loosening — forbidden without sign-off) |
| N4 files-connector symlink probe | P3 | accept, documented | no symlink privilege on this host (WinError 1314); no remote path input exists (operator-scoped connector) |
| N5 seven routes declare no response_model | P3 | ledger | current dicts fit; adding models is an additive Phase 7-era nicety |
| fetcher.py 83% / files.py 81% remainder | P3 | accept, documented | playwright-gated / tesseract-gated optional-dep branches; all guards mutation-killed |
| evaluation runners 44-83% | P3 | accept, documented | env-gated benchmark harnesses; CI runs graph smoke + authority bench + rerank smoke |
| Docker ST-offline boot | P2 | **CI-carried** | engine down locally; WP13 saw the same and it later recovered |
| n8n 300-350-node expansion | optional | **deferred user decision** (WP13 precedent) | large untestable artifact; not WP14 scope |

### WP14 CI addendum (recorded after the pushes, via the Actions API)

- **Run #22 on 3040c6e: GREEN — all three jobs** (both charset-normalizer
  matrix legs with the FULL suite + all three lints incl. the new
  checklist-paths check + graph smoke + authority benchmark; docker job
  = image build of the WP14 Dockerfile + non-root assertion + boot +
  /health probe). First CI run on any WP14 commit; no red run ever
  appeared on the branch.
- **Run #23 on a2a4c2c (README note): GREEN.**
- **Run #24 on d1a2185 (final head): GREEN — and it closes the last
  unverified gate.** The docker job's new step **"Offline ST boot (baked
  model, no network)" succeeded**: the container ran with --network none
  and NEXUS_EMBEDDER=st:sentence-transformers/all-MiniLM-L6-v2, and
  /ready answered **200** — the baked /opt/hf model loaded with
  HF_HUB_OFFLINE=1 and ZERO egress. Item 3's "UNVERIFIED-locally" is
  therefore superseded: locally unverified (engine down all session),
  **CI-verified end-to-end** — exactly the H3 test the mission specified.
- Python 3.14.7 parity at the WP14 head (local .venv, after all fixes):
  **944 passed, 7 skipped, 623 subtests** — identical outcomes to 3.12
  (warnings-only delta, upstream slowapi, documented since WP11 P3-13).

## PHASE 7 GO/NO-GO (every box earned with a command + real output)

- [x] min_score reachable end-to-end (/search?min_score= + search_page),
      raw m25_score/ector_score available to the refuse-gate (pinned
      through fusion AND rerank, incl. None cases) — tests/core/test_min_score.py
- [x] _run_search() exists; /search behavior unchanged (gate set green
      with ZERO test edits) — tests/core/test_run_search.py
- [x] ST embedder works inside the container OFFLINE — CI run #24 docker
      step "Offline ST boot (baked model, no network)": success
- [x] compose carries every NEXUS_* var incl. LLM-related ones
      (env_file verified live via compose config)
- [x] A/B retention tested (tests/ranking/test_ab.py); checklist paths all
      exist (new CI lint green on both matrix legs)
- [x] suite green on 3.12 (944/7/623, fresh venv) AND 3.14 (944/7/623);
      golden byte-identical; coverage 87% -> 89% (not lower)
- [x] no secrets in the repo (sweep clean); no tracked artifacts
      (CI hygiene step green; package zip 240 entries, zero junk)
- [x] CI green on the WP14 head: runs #22, #23 and #24 all GREEN
      (run #24 = final head d1a2185)

**Phase 7: GO.** Build order + per-step test gates: docs/PHASE7_KICKOFF.md.

### WP14 hostile re-read of the full branch diff (recorded as required)

The full git diff phase-7..HEAD (38 files, +1672/-87) was re-read
adversarially before sign-off. Risks found and what was done:

1. **The _run_search move could have silently dropped behavior** (validation
   order, 	op_k clamping, response shape). Disposition: the gate set
   (golden keyword, test_api, pagination, dos, shared_searcher, boolean,
   min_score, query_cache) passed with ZERO test edits, and
   tests/core/test_run_search.py pins the exact 18-input signature +
   every error path — drift is now mechanically impossible.
2. **Reload-based API tests can litter the repo root** (the WP11 NEXUS_DB
   lesson). Disposition: re-checked the repo root after every suite run —
   zero new files; 
exus_search.db mtime unchanged (10/3, pre-WP14).
   The new test classes save/restore NEXUS_DB per the WP11 hygiene pattern.
3. **Doc-edit encoding hazards** (I mojibake'd SPEC.md/PHASE6_PLAN.md once
   via a PowerShell roundtrip mid-session). Disposition: both reverted via
   git checkout immediately and redone with a safe editor; final files
   verified valid UTF-8; recorded in the worklog's honest-mistakes section.
4. **env_file: required:false needs compose >= 2.24** — a version
   constraint I introduced. Disposition: documented in the README section
   and the compose file comment.
5. **HF_HUB_OFFLINE=1 could break the DEFAULT (hash) boot** if anything
   touched the hub at import. Disposition: the CI docker job boots the
   default embedder (run #22 step "Boot the container and probe /health":
   success) AND the offline ST boot (run #24 step 6: success) — both paths
   proven on the modified image.
6. **The dev_matrix probe shows 3 "failed" checks** — all probe-expectation
   artifacts, not app defects (delete-order 404, cursor-clamp-then-window
   400, 404-body-vs-DocumentOut). Documented in the worklog so nobody
   "fixes" correct behavior later.
7. **Add-Content writes CRLF worktree copies of the docs** (git warned
   once). Disposition: harmless — index is normalized to LF by
   	ext=auto eol=lf (verified git ls-files --eol shows no tracked
   CRLF-index files), same class as the pre-existing H9 worktree artifacts.

### WP14 final CI record

- Run #22 (3040c6e): GREEN — first WP14 push; full suite on both charset
  legs, all lints incl. the new checklist-paths check, docker build/boot.
- Run #23 (a2a4c2c): GREEN.
- Run #24 (d1a2185): GREEN — includes the new "Offline ST boot (baked
  model, no network)" docker step: SUCCESS (H3 verified end-to-end).
- Run #25 (55bfc01): GREEN.
- **Run #26 (10554d4 — the final head at sign-off): GREEN.**
- The go/no-go box "CI green on the WP14 head" is earned by run #26.
