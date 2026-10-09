# Nexus Search — Phase 1-3 Upgrade

# WP13 — Phases 1-6 sign-off release (NEW)

## What shipped
- **Encoding regression fix** (blocking): the WP12 script-coherence
  scoring mojibaked spaced Korean euc_kr/cp949, GBK-with-spaces/digits,
  and Danish/French/Dutch cp1252 with 2+ of one colliding letter (ø/û/ë
  flipped to cp1257). Both shares now use one denominator (non-space,
  non-digit, non-punctuation chars), legacy decodes with >5% symbols are
  rejected, Baltic evidence is the non-colliding Ø/ø only (cp1257 is
  accepted solely as the detector's top guess), CJK candidates are ranked
  by share + common-character coherence, and a coherence floor kills the
  symbol-free ISO-table mojibake class. 75-row round-trip table + WP11
  differential fixture + detector-stub matrix pin it; CI runs the suite on
  charset-normalizer 3.5.1 AND 3.4.6.
- **Scale benchmark**: selective-query scenario (Zipfian topics, ~1-5%
  match) beside the labelled match-everything worst case. Selective
  hybrid at 10K docs: **27 ms** (worst case: 1,384 ms, labelled).
- **Open-ledger fixes**: related_searches fallback is O(query grams) per
  request (was O(vocabulary); parity-pinned), benchmark cleanup failures
  are logged instead of swallowed.
- **Verification closed**: Docker `compose build` + `GET /ready` verified
  locally (200 `{"ready":true}`); Python 3.12 parity verified (844/7/614,
  identical to 3.14); packaging via git archive verified junk-free.
- **Docs**: docs/PHASES_1-6_CHECKLIST.md (per-phase sign-off with real
  run numbers); WP13 section in docs/AUDIT_REMEDIATION.md (root causes,
  before/after tables, decision ledger).

## Upgrade notes
- No public API changes, no new dependencies. The golden keyword
  baseline is byte-identical.
- Test counts: **844 passed / 7 skipped offline, 849 / 2 model-gated**
  (Windows 3.14 and Ubuntu 3.12 identical).

# Phase 6 — Link Intelligence (NEW)

## What shipped
- **Link graph store** (`links/graph.py`): versioned SQLite store for
  (from_url, to_url, anchor_text, rel_attrs) edges. Anti-flood caps: 1000
  distinct outbound per source page, 500 per domain pair. Recrawls update
  last_seen, never duplicate.
- **Authority computation** (`links/authority.py`): PageRank (d=0.85,
  ≤30 iters, tol 1e-6) with anti-farm corrections: nofollow/sponsored/ugc
  edges pass zero mass, reciprocal pairs discounted to 0.25 both ways,
  self-links dropped. Domain-diversity factor: `authority = PR_norm ×
  (0.5 + 0.5 × min(inlink_domains/10, 1))` — a 3-page single-domain clique
  cannot out-rank a page with 10+ distinct-domain inlinks regardless of raw
  PR cycling. Popularity = log1p(inlink_domains)/log1p(100). Shadow-swap
  atomic writes (crash never leaves partial scores).
- **Extractor upgrade** (`ingestion/connectors/web.py`): `extract_page()`
  now returns `Link(url, anchor_text, rel)` objects; `rel` carries
  nofollow/sponsored/ugc; anchor text capped at 512 chars.
- **Crawler wiring** (`crawler/pipeline.py`, `crawler/cli.py`): every
  fetched page feeds the graph; the graph NEVER triggers a fetch (SSRF
  boundary intact). CLI `authority --recompute/--show/--stats`.
- **Ranking integration** (`ranking/signals.py`, `ranking/ranker.py`,
  `core/api.py`): `source_authority` and `popularity` signals now read from
  the graph via `SignalContext.link_intel`; unknown URLs get NEUTRAL (0.5).
  Weights default 0.0 — operators opt in via `NEXUS_AUTHORITY_WEIGHT` /
  `NEXUS_POPULARITY_WEIGHT` (clamped [0,1]). `/search/explain?rerank=true`
  surfaces per-result feature values + blended score.

## Rollback
Set both env weights to 0 + restart. No reindex, no data migration — the
signal is purely additive on the rerank path and disappears immediately.

## Known limits (documented in docs/PHASE6_PLAN.md)
- Heuristics only, no ML spam classifier — distributed farms under one
  controller can still fool domain-diversity weighting (Phase 8+ tooling).
- Authority quality is validated on synthetic graphs with known ground
  truth (hub > clique > orphan), not real web-scale spam (no production
  graph exists to validate against — stated plainly, not papered over).

## Upgrade completed

This release strengthens the existing Phase 1-3 foundation without replacing the working architecture.

### Search core
- Added a dependency-free query parser.
- Added quoted phrase recognition.
- Added `type:` / `doc_type:` and `lang:` / `language:` filters.
- Added title relevance boosting.
- Added phrase relevance boosting.
- Improved snippets to start near a matched term or phrase.
- Added safe `top_k` bounds at the API boundary.

### Web ingestion
- Added canonical URL extraction.
- Preserved canonical URL in document metadata.

### Crawler
- Added sitemap XML parsing helper.
- Crawled-page metadata now includes URL and canonical URL.

## Verification

`python -m unittest discover -s tests -v` → **113 tests, 113 passed, 0 failed**.

FastAPI remains environment-dependent; the API source is syntax-valid, while the underlying index/search/storage behavior is covered by tests.
