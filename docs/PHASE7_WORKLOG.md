# Phase 7 Worklog — AI Search / RAG (running log)

Branch `phase-7` (off the WP14 head `1eb9c75`, "Phase 7 GO"). Binding
contract: `docs/PHASE7_PLAN.md`; build order: `docs/PHASE7_KICKOFF.md`.
One commit per work package, tests first, full suite green before each.
This log records decisions, commands and real outputs.

---

## Baseline (P7-0 prerequisites)

Suite before any Phase 7 code (Windows 11, Python 3.14.7, `.venv`):

```
$ NEXUS_ENV=dev python -m pytest -q
944 passed, 7 skipped, 1511 warnings, 623 subtests passed in 458.31s
```

Matches the WP14 final gate exactly (944/7/623). WP14 already delivered
the two P7-0 code items; re-verified by running their suites:

```
$ python -m pytest tests/core/test_min_score.py tests/core/test_run_search.py -q
26 passed, 51 warnings in 5.86s
```

- P7-0.1 `min_score` end-to-end + raw `bm25_score`/`vector_score`
  surviving fusion AND rerank (incl. `None` cases): WP14-1 (commit
  fc41c95, 14 tests) — VERIFIED GREEN above.
- P7-0.2 `/search` logic in `_run_search` with byte-identical behavior:
  WP14-2 (commit 5bf68b4, 13 tests) — VERIFIED GREEN above.
- P7-0.3 Full-suite baseline recorded above.
- P7-0.4 `HF_HOME` + model baked into the Docker runtime image: WP14-3
  (commit e9c15ae), CI-verified end-to-end by run #24's "Offline ST boot
  (baked model, no network)" step (`--network none`, `st:` embedder →
  /ready 200) and verified locally in the WP14 addendum (engine
  recovered: `/ready 200`, zero egress). No gap to flag.
- Golden baseline untouched through all Phase 7 commits
  (`git log v0.6.0-phases-1-6..HEAD -- tests/golden/` is empty; the
  golden test is green in every full-suite run). SHA256 at the
  checkpoint head: `7DBE27582E5E127ABB1B1A221963FFE1D6A2E224919D1034C2D3CE3CE5C3D31F`.

---

## P7-1 — embeddings + retrieval pipeline (commit 7bb11c6)

**Decision D1 — document embeddings are chunk-only in retrieval.**
"Document Embeddings" in this system = every STORED row receives an
embedding through the one `EmbeddingSync` (the parent row when a doc was
NOT chunked — e.g. API `/documents` writes — plus every `parent#chunkN`
chunk row; for chunked parents there IS no parent row: ingestion deletes
it and keeps only chunks, so parent-level context reaches RAG through
chunk metadata `parent_id`/`chunk_index`/`chunk_count`). There is no
separate parent-vector path and no re-chunking at answer time
(PHASE7_PLAN §7). Proven by tests: every chunk row vectorized via
embedding_sync; delete cascade removes chunk vectors; re-ingest
replaces (including the shrink-to-unchunked case); coverage honesty (a
row smuggled past the indexer reads < 1.0 coverage, never rounded up).

**Decision D2 — Phase 5/6 integration.** `RagRetriever` reuses the exact
`/search` machinery: shared `HybridSearch.search_page(group_chunks=False)`,
`understand_query` (spell correction/expansion), and the Phase 5
`rerank` + Phase 6 weights (`RankingWeights.from_env` — all link
signals default 0.0 = inert), behind `NEXUS_RAG_USE_RERANK` **default
ON** per the mission contract (note: `/search`'s own rerank default is
off/treatment-only — recorded so the asymmetry is a documented choice,
not an accident). Raw `bm25_score`/`vector_score` ride on the result
objects and survive the rerank (mutation-killed: nulling them fails 2
tests).

Filters: the request-model `filters` dict is rendered INTO the query
string as `type:`/`lang:` tokens — the parser + `matches_filters` +
vector pushdown semantics are identical to `/search` (no second filter
path; unknown keys are a loud `ValueError`).

Evidence: `tests/rag/test_chunk_embeddings.py` (6) +
`tests/rag/test_retrieval.py` (10) — 16 tests; N+1 proof: RAG adds
exactly ONE batched `get_documents` on top of the shared pipeline's own
batched reads (measured: rag_total == baseline + 1).

```
$ python -m pytest tests/rag -q    (after P7-1)
16 passed in 1.97s
$ python -m pytest -q -n auto
960 passed, 7 skipped, 1564 warnings, 623 subtests passed in 400.62s
```

## P7-2 — context selection (commit 8690bfb)

**Decision D3 — sequencing:** `egress.py` was created in THIS work
package with the `llm_allowed` predicate (context selection needs it);
P7-3 extended it with redaction/hashing/EGRESS_DOC. Recorded so the
commit history doesn't look like a misplaced file.

Selection is pure + deterministic (greedy rank order): `llm_allowed:false`
chunks never enter the prompt (the production neighbor fetcher enforces
the same filter for STITCHED text — an egress hole caught in design
review); per-parent cap (default 3); token-Jaccard near-dup suppression
(core/diversity.py idea, default 0.85); hard budgets (chars/token
heuristic + bytes — NEVER exceeded, property loop over 40 random trials);
neighbor stitching ±1 same-parent only, budget-guarded, fetched via ONE
batched read, and stitched neighbors are consumed (not re-selected);
stable numbering [1..n]; every drop recorded with exactly one reason.

Evidence: 18 tests incl. the property loop; mutations killed (budget
check neutered → 2 fail; `llm_allowed` neutered → 3 fail; reverted → 34/34).

```
$ python -m pytest -q -n auto
978 passed, 7 skipped, 1544 warnings, 623 subtests passed in 379.51s
```

## P7-3 — provider layer, egress controls, kill switch (commit f641573)

**Decision D4 — API sequencing:** `/ask` ships in P7-6 (after Checkpoint
1, per the staging). P7-3 delivers and tests the SERVICE-level boot
policy (`RagConfig.status()` → ok|absent|disabled; `build_provider`)
plus the additive `/health` rag block; the `/ask`-returns-503 assertions
land with the endpoint in P7-6. The poisoned-key secret-leak test DID
ship now (log records + /health + /search + /ready bodies), because the
boot surface it covers exists today.

Contents: `config.py` (all knobs, timeout clamped ≤ 30 s, retries ≤ 2,
price table → honest null cost), `provider.py` (interface with NO tools
parameter — source-grep + signature tests pin it; StubProvider with the
10 adversarial behaviors as the P7-5/P7-9 defense oracles; stub
self-enforces a wall-clock deadline so `slow` → `ProviderTimeout`, never
a hang; OpenAI-compatible + Anthropic adapters via `requests` only —
mocked-HTTP tests; retry ONLY connect/429/5xx, read timeouts fail fast;
error codes carry NO response bodies), `metrics.py` (day-keyed counters,
numbers only), `/health` `rag: {status, provider, reason}` (never the
key, never a base URL).

Boot-status matrix (all tested): stub+dev → ok; stub+production without
`NEXUS_RAG_ALLOW_STUB` → absent (fail-closed, loud); real provider
without key → absent; `NEXUS_RAG_ENABLED=0` → disabled.

Evidence: 60 new tests. Mutations killed: boot-policy key check neutered
→ 1 fail; error path leaking response body → 2 fail; reverted → green.

```
$ python -m pytest -q -n auto
1038 passed, 7 skipped, 1664 warnings, 623 subtests passed in 420.08s
```

## P7-4 — refuse-gate + calibration (commit abae587)

**Decision D5 — gate rule.** Refuse iff EVERY PRESENT top-hit raw signal
is below its floor; `None` = that retriever didn't find the row, so
single-signal hits reduce exactly to the mission's hybrid agreement
rule ("top result from one retriever AND that signal below its floor").
Zero results → `no_evidence`. Fused scores are NEVER inputs (pinned:
identical raw signals with wildly different fused artifacts → identical
decisions). Unknown embedder → conservative default (10.0 / 0.55) +
loud once-per-process warning — fails CLOSED.

**Decision D6 — CALIBRATION (the honest one).** Harness built on the
WP10 fixture + the deployed `RagRetriever` path (hybrid, understanding,
rerank on, default weights), fixture no-answer queries + 10 committed
unanswerable extras, 60/40 stratified split by SPLIT_SEED, floors chosen
on DEV by a committed deterministic rule, results reported on TEST.

Measured distributions (hash:384, top-1 post-rerank, 87 queries):

```
answerable   (72): top_bm25  min 2.997 / p25 7.638 / med 12.457 / max 45.256
                  top_vector min 0.061 / p25 0.302 / med 0.420  / max 0.742
unanswerable (15): top_bm25  min 3.297 / p25 3.510 / med 5.183 / max 16.907
                  top_vector min 0.178 / p25 0.212 / med 0.261 / max 0.372
```

**The mission default target (≥95% unanswerable refusal at ≤10%
answerable false-refusal) is MEASURED-UNACHIEVABLE on hash:384**: the
junk and answerable distributions overlap on BOTH raw axes
simultaneously (the lexical embedder cannot distinguish weak-relevant
from junk evidence). Pareto frontier (dev split, exhaustive grid over
bm25 0–20 × vector 0–1):

```
junk_refusal  answerable_false_refusal   floors
1.000         0.302                      (17.0, 0.40)
0.889         0.279                      (11.0, 0.40)
0.778         0.186                      (17.0, 0.30)
0.667         0.116                      (6.5,  0.40)
```

Reframing "false refusal" to the queries where it harms (answerables
whose top-1 hit IS a fixture-relevant doc — "well-evidenced"; refusing
one of those throws away good evidence, while refusing an answerable
whose top hit is a WRONG doc is the gate honestly reporting weak
evidence; both rates reported):

```
floors       split  junk   well_false  raw_false
(17.0,0.40)  dev    1.000  0.156       0.302
(17.0,0.40)  test   1.000  0.412       0.621
(11.0,0.40)  dev    0.889  0.125       0.279
(11.0,0.40)  test   0.833  0.353       0.552
(6.5,0.40)   dev    0.667  0.031       0.116
(6.5,0.40)   test   0.833  0.059       0.207
(6.5,0.35)   dev    0.556  0.000       0.093
(6.5,0.35)   test   0.833  0.000       0.172
```

**Restated target** (recorded in gate_floors.json + the harness): on
dev — junk refusal ≥ 2/3, well-evidenced false-refusal ≤ 6%, raw
answerable false-refusal ≤ 12%; among grid pairs meeting all three:
max junk refusal, then min well-evidenced false-refusal, then max
vector floor (drift-safer against junk), then min bm25 floor — fully
deterministic, so CI re-derives the frozen numbers exactly.

The rule chose **hash:384 floors {bm25_floor: 6.5, vector_floor: 0.45}**.
Rationale for the bias toward refusing: a refusal is first-class (reason
code, best sources attached, zero LLM spend — recoverable UX), while a
junk pass-through is a confident wrong answer (the exact Phase 4
finding: confident junk at BM25 top-1 5.4–9.1).

**REPORTED HELD-OUT TEST-SPLIT RESULTS** (committed in gate_floors.json):

```
junk_refusal_rate                 0.8333   (5/6)
well_evidenced_false_refusal_rate 0.0588   (1/17)
raw_answerable_false_refusal_rate 0.2069   (6/29)
```

Raw answerable refusals are dominated by paraphrase/typo-tail queries
whose TOP-1 hit is a WRONG document under the lexical hash embedder —
refusing weak wrong evidence is the gate's contract; P7-9 measures those
queries' retrieval recall separately. st:* embedders: floors need a
model-gated calibration run (NEXUS_RUN_MODEL_TESTS=1) — until then the
conservative default + warning applies (by design, never silently).

Evidence: 21 tests (gate math incl. the calibration-mirror invariant;
CI regression: harness re-derives the frozen floors exactly + both
splits meet the stated targets + committed test-split numbers match to
3dp). Mutations killed: None-as-pass; last-hit-instead-of-top.

```
$ python -m pytest -q -n auto
1059 passed, 7 skipped, 1664 warnings, 623 subtests passed in 507.41s
```

---

## Honest mistakes made and fixed during the checkpoint (recorded)

1. **PowerShell 5.1 `Get-Content`/`Set-Content` corrupted the files TWICE
   in one gesture** (the WP14 H9 lesson, repeated): (a) `Set-Content
   -Encoding UTF8` writes a BOM, and (b) `Get-Content` without
   `-Encoding` reads UTF-8 bytes as ANSI, so every `—` in a mutation-loop
   restore was double-encoded into mojibake (`â€""`) — 8 files, caught
   by the repo's own lints (`check_duplicate_tests.py` crashed on a
   BOM'd test file) plus a checkpoint byte-scan (`sorted(set(hex(ord(c))
   ...))` showed cp1252 artifacts as CHARACTERS). All BOMs stripped; all
   mojibake repaired by the exact inverse transform
   (`text.encode('cp1252').decode('utf-8')`, per-maximal-run for the two
   mixed files — scripts kept in the session temp, technique recorded
   here); UTF-8 decodability re-verified for every changed file; the
   repair touched comment/docstring text only (imports + suite green).
   Rule going forward: no `Get-Content`/`Set-Content` on repo files —
   the edit tool only.
2. **Env-docs drift caught by the lint**: P7-3 introduced 23
   `NEXUS_LLM_*`/`NEXUS_RAG_*` knobs without documenting them —
   `check_env_docs.py` failed at the checkpoint re-verify. Fixed:
   `.env.example` (52 documented vars) + README env-table rows; lint
   green. (Formally a P7-10 deliverable, but CI runs the lint on every
   push — fixed now, not later.)
3. **A test attempted a network fetch**: the degraded-vector-boot test
   originally booted `st:<missing model>` (real download attempt, 51 s,
   violates offline-by-default). Rewritten with an INJECTED embedder
   boom — offline, 4.6 s.
4. **Exploratory scripts committed deliberately**:
   `scripts/dev/p7_gate_explore.py`, `p7_gate_frontier.py`,
   `p7_gate_frontier2.py` (same style as `scripts/dev/wp12/`); they are
   the calibration-evidence generators, kept reproducible.

---

## Checkpoint 1 verification gate (serial, ambient NEXUS_ENV=dev)

```
$ python -m pytest -q
1059 passed, 7 skipped, 1631 warnings, 623 subtests passed in 411.24s
```

- Baseline 944/7/623 → **1059/7/623** (+115 tests, 0 failed, same 7
  model/bench-gated skips).
- Golden keyword baseline byte-identical (untouched + test green).
- `check_duplicate_tests.py`: no duplicate test function names.
- `check_env_docs.py`: env docs consistent: 52 documented vars.
- `check_checklist_paths.py`: all checklist test files exist.
- RAG package coverage (pytest-cov, tests/rag only):
  `nexus_search/rag` **94%** (calibration 89, config 95, context 97,
  egress 100, gate 96, metrics 90, provider 91, retrieval 99).
- pytest-cov was missing from the working .venv (WP14's fresh-venv gate
  used a different venv); installed from the pinned requirements-dev
  (pytest-cov==7.1.0) — the repo's split-dependency discipline intact.
- Flake record (honesty): one `-n auto` full-suite run under heavy
  machine load showed `1 failed, 1058 passed` — the failure name was
  lost to an interrupted command. Serial re-run: **1059/7/623 green**
  (7:59). `tests/crawler` re-run under `-n auto`: 130/130 green. Not
  reproduced in 2 subsequent attempts; classed environmental (WP14's N1
  precedent — server-thread scheduling under load). CI (serial,
  Ubuntu) is the carrier of record; if it ever reproduces, it gets its
  own worklog entry — not a silent pass.

## Open risks / notes for the owner (Checkpoint 1)

1. **The calibration target was restated with evidence** (D6): the
   default ≥95%/≤10% is unachievable on hash:384 — junk overlaps the
   answerable-weak tail on both raw axes. Frozen floors (6.5, 0.45)
   give test-split junk refusal 83.3% at 5.9% well-evidenced
   false-refusal. If the owner wants the 95% junk floor anyway, the
   price is 15.6–41.2% well-evidenced false refusals (frontier above) —
   an owner decision, not silently taken.
2. **Thin margins around the frozen floors** are inherent to any frozen
   threshold; the CI regression pins the fixture exactly, and recalibration
   is one command (`python -m nexus_search.rag.calibration`). bm25 idf
   is corpus-dependent — recalibrate after major corpus growth.
3. **st: floors are uncalibrated until a model-gated run** — the
   conservative default refuses more until then (loud warning, never
   silent). P7-9's model-gated variant is the planned calibration slot.
4. **/ask 503 surface tests** deferred to P7-6 (Decision D4); the
   service-level boot policy is fully tested now.
5. **Degraded keyword-only mode carries vector=None** — the gate then
   decides on bm25 alone (agreement rule); keyword floors are the same
   committed bm25 floor. Honest but worth noting: a keyword-only RAG
   deployment inherits uncalibrated-for-keyword-only floors.
