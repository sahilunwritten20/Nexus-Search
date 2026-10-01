# Audit Remediation Log — Phases 1–6

Running log for the `fix/audit-phase1-6` remediation. One entry per work
package (WP0–WP10). "Baseline" numbers are pre-fix measurements captured on
the branch point; "After" numbers are re-measured with
`scripts/dev/audit_repro.py` at the final gate.

## Environment (all runs)

- OS: Windows 11 (win32); shell: PowerShell 5.1
- Python: 3.14.7 (.venv); CI target: Ubuntu / Python 3.12 (not runnable locally — see Unverified)
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
outbound network appears available; full HTTP validation happens in WP9.

## WP1 — BUG-01: spell-correction CPU DoS

- Status: **FIXED**

## WP1 — BUG-01: spell-correction CPU DoS

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
- Evidence (baseline -> after, same machine, `understand_query` CPU on junk):
  | input | before | after |
  |---|---|---|
  | 229 chars | 4.87 s | **1.70 ms** |
  | 629 chars | 13.50 s | **1.25 ms** |
  | 1,689 chars | 42.04 s | **1.29 ms** |
  | 2,000 chars | 43.12 s | **2.35 ms** |
  Acceptance (<100 ms) met with >40x margin; >128-word API shapes are
  400-rejected pre-parse. `tests/core/test_search_dos.py` proves a normal
  request answers < 1 s during a 50-request junk flood (stable across 3
  runs) and that under-guard junk (100 words) drains without 5xx.
- Tests: `tests/ranking/test_spell_budget.py` (14 new),
  `tests/core/test_search_dos.py` (5 new). Existing spelling-quality tests
  unchanged and green (incl. a real adjacent-transposition typo case).
- Suite after WP1: **650 passed, 3 skipped** (baseline 634/3 — +16 new, 0 regressions).
- Decision (ambiguous -> safest, recorded): `NEXUS_SPELL_CANDIDATE_BUDGET`
  default 4000 comparisons — generous at prototype vocab scale, degrades
  honestly (partial dist-2 coverage) on very large vocabularies; documented.

## WP2 — BUG-02: Phase 6 graph node identity split

- Status: **FIXED**
- Root cause: `crawler/pipeline.py` recorded `to_url` raw (fragments, utm
  params, trailing slash, host case, default ports) while page URLs were
  frontier-normalized; `authority_for` is an exact-match lookup, so scores
  missed and PageRank mass fragmented across URL variants of one page.
- Fix:
  - `links/graph.py`: ONE identity choke point (`LinkGraph._norm_pair`) used
    by `record_edge`, `record_edges` and `authority_for` — no caller can
    bypass normalization; self-links are detected post-normalization
    (a fragment self-link is a self-vote and is dropped).
  - `crawler/url_utils.py`: `normalize_url` now also decodes percent-escapes
    of UNRESERVED characters (RFC 3986) — `/p%61ge` == `/page`; reserved
    escapes (`%2F`, `%26`, ...) are never decoded. Applies to frontier and
    graph identically, so the whole system shares one URL space.
  - `crawler/cli.py`: `normalize-links` command — idempotent data migration
    for pre-fix rows (SQL-only migrations cannot express URL
    canonicalization, so the CLI IS the migration, documented here): merges
    duplicate groups (earliest `first_seen`, latest `last_seen`, anchor/rel
    from the most recent observation, anchor fallback to first non-empty in
    latest-first order), drops post-normalization self-links, all in one
    transaction. `--recompute` refreshes authority scores afterwards.
- Evidence: `scripts/dev/audit_repro.py` URL-variant case, before:
  `{"edges_stored": 4, "authority_found": false}`; after:
  `{"edges_stored": 1, "authority_found": true, "authority": 0.72}` — wait,
  this is re-run at the final gate; see §Benchmarks.
- Tests: `tests/links/test_graph_normalization.py` (9 new): six-variant
  collapse to one target node with merged inlink_count==6, fragment
  self-link rejection, variant `authority_for` probes, the ranking-signal
  path resolving a variant `metadata['url']`, bulk-path normalization,
  CLI merge semantics on pre-populated raw rows, run-twice no-op, live
  local-server e2e (three variant links -> one edge, non-zero authority).
- Suite after WP2: **659 passed, 3 skipped** (+9, 0 regressions).
- docs/PHASE6_PLAN.md §6 ("edges are keyed by normalized URLs") is now
  TRUE and verified by test, not by editing text.
