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
