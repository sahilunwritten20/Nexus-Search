# Phase 6 — Link Intelligence Implementation Plan

Status: **drafted → built → proven** (this doc is written before code; the
benchmark claim in §3 is revised — measured, not guessed — once Stage 3 runs).

What Phase 6 is: an OFFLINE web-graph layer that fills the ranking stack's
biggest honest gap — `RankingWeights.source_authority` and `.popularity`
(currently hardcoded 0.0) become real signals computed from how pages link
to each other. Nothing on the request path gets slower; nothing new can be
attacked from outside.

Non-negotiable inheritance from SPEC.md: honesty over completeness theater.
Areas we cannot verify in this environment (real web-scale spam farms,
production traffic) are stated as such — the heuristics are documented as
**heuristics with known limits**, tested against synthetic graphs with known
ground truth.

---

## 1. Architecture Decision Record: authority algorithm

### Options considered

**A. Raw inbound link count.** O(E) once, no iteration.
- Pro: trivially cheap, trivially explainable ("50 people link here").
- Con: maximally gameable — one spammer registering 1,000 pages on one
  domain outranks everything; learns nothing about link QUALITY.

**B. Classic iterative PageRank (damping d=0.85, power iteration).**
O(E) per iteration, typical convergence 20–40 iterations on small graphs.
- Pro: the reference answer for "how much does the web vouch"; quality
  flows through links, so a link FROM a well-linked page counts more;
  turnkey numeric stability (stochastic vector, no convergence hazards at
  this size); offline by nature.
- Con: pure PageRank is famously farmable by closed cliques/sites that
  cross-link with no external endorsements; needs the anti-farm weighting
  below to be honest at all.

**C. HITS (hubs/authorities).** Mutual reinforcement per query topic.
- Pro: captures "good curators vs good targets" duality.
- Con: two eigenproblems, more sensitive to graph noise, per-topic framing
  maps awkwardly onto a precomputed single authority column, and at our
  scale pays machinery tax for zero evidence of better outcomes.

### Decision: B, with two anti-farm corrections applied at edge weight

PageRank (d=0.85) with weighted edges:
- `nofollow`/sponsored/ugc links pass **zero** mass (recorded, excluded —
  standard SEO convention, and spammers marking their own links is free
  signal).
- Reciprocal pairs (A→B and B→A both present) get edge weight **0.25** in
  both directions. Mutual admiration is endemic in spam cliques and rare in
  citations.
- Self-links (A→A) are dropped at ingestion.

Post-iteration, per-URL score gets a domain-diversity factor:
`authority(u) = pagerank(u) × (0.5 + 0.5 × min(inlink_domains(u)/10, 1))"
— 10 genuinely distinct linking domains caps the multiplier; a clique of
pages on ONE/two domains can approach at most ~0.55× of that ceiling no
matter how high the raw PR cycles. `popularity(u) = log1p(inlink_domains) /
log1p(100)` — bounded [0,1), shaped for attention, not trust.

Honest stated limit (do not oversell): this is a **heuristic**, defeated by
sufficiently distributed farms (many real domains under one controller) —
that class of adversary needs the ML/behavioral tooling explicitly deferred
to Phase 8+. The mission's own success case (3-page mutual clique vs 50
distinct-domain inlinks) IS provably handled (Stage 3 test).

### Why not "fill it from click/popularity counters"

Nothing in Phases 1–5 observes clicks (that's Phase 7's problem; the A/B
log records queries, not clicks). We do not fabricate a popularity signal.
`popularity` comes from the graph (inbound domain diversity), the only real
attention data available.

---

## 2. Schema (versioned migrations, per-store convention)

Store name: `link_graph` (same SQLite file as documents/edges — NEXUS_DB).
Migration v1:

```sql
CREATE TABLE IF NOT EXISTS link_edges (
    from_url TEXT NOT NULL,
    to_url TEXT NOT NULL,
    anchor_text TEXT NOT NULL DEFAULT '',
    rel_attrs TEXT NOT NULL DEFAULT '',
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    PRIMARY KEY (from_url, to_url)
);
CREATE INDEX IF NOT EXISTS idx_link_edges_to ON link_edges (to_url);
CREATE INDEX IF NOT EXISTS idx_link_edges_from ON link_edges (from_url);

CREATE TABLE IF NOT EXISTS authority_scores (
    url TEXT PRIMARY KEY,
    pagerank REAL NOT NULL,
    inlink_count INTEGER NOT NULL,
    inlink_domains INTEGER NOT NULL,
    authority REAL NOT NULL,      -- pagerank × diversity factor
    popularity REAL NOT NULL,     -- log1p(domains)/log1p(100)
    computed_at REAL NOT NULL
);
```

Recompute writes into `authority_scores_shadow` + one-transaction swap
(same shadow-swap discipline as core/reindex.py): a crashed or observed
recompute never leaves a half-written scores table. Ingestion caps (bound
growth): max 1,000 distinct outbound edges recorded per source page, max 500
edges per (source domain → target domain) pair.

---

## 3. Complexity budget — with a measured number

- Graph build (query + group edges): **O(E)**, one pass over link_edges.
- One PageRank iteration: **O(E)** sparse power iteration (each edge visited
  once, numpy scatter-add).
- Convergence: **O(k·E)** for k iterations (k ≤ 30 typical, d=0.85, tol 1e-6).
- Memory: O(V + E) integer/float arrays — 100k edges ⇔ ~3 MB.

**Committed number: full recompute (build + iterate + write) completes in
under 30 seconds for 10,000 pages / 100,000 edges on a single core.**

Measured (WP5 remediation, evaluation/graph_benchmark.py, Windows/Python
3.14): **3.2 s** recompute for 10,000 pages / 99,991 edges, converged in 13
iterations; bulk ingest of the same graph 18.5 s; all 10,000 URLs scored,
9,997 with aggregated anchors. Machine-readable: docs/graph_benchmark_full.json.
Reduced smoke mode (500 pages / 5k edges) runs in CI; full mode behind
NEXUS_RUN_GRAPH_BENCH=1.

---

## 4. Failure modes (each maps to a SAFE ranker value, never a crash)

| Case | Ranker observes | Why it's safe |
|---|---|---|
| Empty graph (no edges yet) | authority=0.5 (NEUTRAL), popularity=0.5 | No signal ≠ penalty; retrieval order unchanged |
| Single-node graph | same NEUTRAL (no inlinks) | nothing to learn from |
| Disconnected component | base mass pages only; sane PR continuation | dangling redistribution handles it |
| Page linking to itself | edge dropped at ingestion | self-votes never count |
| 10,000-in-link hub | per-edge PR correctly diluted (1/N out); domain-diversity factor caps inflation | scale without blowup |
| Recompute crashes mid-way | previous `authority_scores` intact (shadow swap never commits partial) | old scores > corrupt scores |
| Doc deleted mid-recompute | its scores expire with the next successful recompute; live reads fall back to NEUTRAL for unknown URLs | stale rows are inert, never wrong-ranked forever |
| rel=nofollow farm | zero passing mass | convention-honoring |

The `rerank(..., storage=None, link_intel=None)` path (unit tests, offline)
keeps the documented zero-data behavior: NEUTRAL placeholders.

---

## 5. Rollout plan (defaults safe, single-action rollback)

- **Off by default.** `RankingWeights.source_authority` / `.popularity`
  remain 0.0 unless the operator sets `NEXUS_AUTHORITY_WEIGHT` /
  `NEXUS_POPULARITY_WEIGHT` (floats 0–1 recommended; out-of-range values
  are clamped to [0,1] with a warning). New factory:
  `RankingWeights.from_env()`; `api.py`'s rerank path uses it.
- Edges are always recorded by the crawler (cheap), but they only influence
  ranking when weights are non-zero AND rerank is on (`rerank=true` or the
  A/B treatment bucket) — two existing switches stack correctly.
- **Turn on:** set the env weights, restart, recompute via
  `python -m nexus_search.crawler.cli authority --recompute --db <db>`.
- **Rollback:** set weights to 0.0 (or unset) + restart. No data migration,
  no reindex — the signal is additive and disappears immediately.
- Inspect: `authority --show <url>`, `authority --stats`.

Phase-5's zero-weight invariant holds: with weights 0 the ranker output is
byte-identical to the retrieval order (existing tests pin this).

---

## 6. Non-goals (Phase boundaries are load-bearing)

- No real-time graph updates on the request path (recompute is offline/CLI).
- No distributed graph store or external DB (Phase 8+ territory).
- No ML spam classifier (heuristics only, documented as such).
- No clicks-based popularity (no click instrumentation exists; Phase 7).
- No fetching "to check" a link. **Link intelligence only CONSUMES pages the
  SSRF-validated crawler already fetched** — it never triggers network I/O.
- No change to normalized URL semantics: edges are keyed by normalized URLs.

---

## 7. Files touched / added

```
nexus_search/links/__init__.py
nexus_search/links/graph.py        # store: edges + caps + shadow-safe scores
nexus_search/links/authority.py    # PageRank + corrections + recompute
nexus_search/ingestion/connectors/web.py   # extract_page carries anchor/rel
nexus_search/crawler/pipeline.py           # records edges after ingest
nexus_search/crawler/cli.py                # authority subcommand
nexus_search/ranking/ranker.py             # RankingWeights.from_env, ctx
nexus_search/ranking/features.py           # SignalContext.link_intel
nexus_search/ranking/signals.py            # real authority/popularity reads
nexus_search/core/api.py                   # rerank path uses from_env; explain
docs/PHASE6_PLAN.md, README.md, SPEC.md, docs/CHANGELOG_UPGRADE.md
tests/links/*, tests/ranking/test_authority_integration.py, evaluation link eval
```
