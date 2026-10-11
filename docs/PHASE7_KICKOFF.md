# Phase 7 Kickoff — AI Search / RAG build order (docs only; nothing here is implemented)

WP14 verdict: **GO** (see the WP14 section in `docs/AUDIT_REMEDIATION.md`
for the evidence; the binding contract is `docs/PHASE7_PLAN.md`).

Everything Phase 7 needs from Phases 1–6 is in place and test-pinned:
`min_score` (raw cosine floor) is reachable end-to-end through
`HybridSearch.search_page` and `/search?min_score=`; raw `bm25_score` /
`vector_score` survive fusion AND rerank on the wire; `/ask` reuses the
shared `_run_search` pipeline (one `HybridSearch` instance — BUG-06
discipline); the A/B retention precedent is pinned (`purge_older_than`);
the plan's guard rails (unconditional key-gating, LLM stub, fencing) are
written down. No RAG/LLM code exists yet — that is deliberate.

## Build order (each step ships behind its test gate; do not reorder)

### 1. Calibrate the refuse-gate on the WP10 fixture
- Measure raw-score distributions (top vector cosine, top BM25 raw score,
  hybrid agreement) for answerable vs unanswerable questions on
  `nexus_search/evaluation/semantic_benchmark/fixture.py` (hash-embedder
  determinism keeps this reproducible in CI).
- Output: frozen floor numbers + the agreement rule, committed as a CI
  regression floor (fail the suite on drift — same pattern as the WP10
  CI floors).
- **Gate:** a new test module pins the calibrated floors against the
  fixture; suite green; golden keyword untouched (retrieval changes: NONE).
- Explicitly forbidden: using fused RRF/weighted scores as the signal
  (WP12-B2 retraction stands).

### 2. Fenced prompt builder + injection tests (with the stub LLM)
- Implement the provider interface + the STUB implementation first
  (deterministic canned answers, no network, no key — CI never needs egress).
- Prompt builder: generated delimiter frame (content cannot open/close its
  own fence), instruction-pattern stripping with per-redaction metadata,
  plain-text answer contract (§2b sanitization applies to model OUTPUT too).
- **Gate:** an injection corpus (payloads seeded as documents must be
  QUOTED, never obeyed — regression floor, not best-effort) + unit tests
  for the fence/stripper edge cases; all green against the stub.

### 3. `/ask` (auth, caps, timeouts)
- Mount `Depends(require_api_key)` UNCONDITIONALLY (never `_READ_AUTH` —
  WP14-8). Reuse `_run_search` + `search_page(min_score=<calibrated>)`.
- Enforce BEFORE the LLM call: request caps, per-key token budget (with the
  per-worker caveat documented), ≤30 s hard timeout, bounded retry with
  jitter, LLM concurrency semaphore (default 4), visible 429/Retry-After.
- Citation verification per §3: every citation must resolve to a retrieved
  chunk id; all-failed-citations answers are refused with metadata.
- **Gate:** auth/caps/timeout/citation tests against the stub; refuse-gate
  calibration tests still green; load smoke extended to /ask.

### 4. `/ask/stream` (SSE)
- Same auth/caps; connection dies visibly on the LLM timeout with a
  truncated-answer marker (never a silent hang); transcript caps (§5)
  enforced per turn; session ids treated as untrusted bounded strings.
- **Gate:** streaming tests with the stub (incl. timeout truncation);
  no-5xx under the existing 50-way concurrency probe extended with /ask.

### 5. Answer-quality benchmark (WP10 protocol, answer-specific)
- Labeled fixture: answerable / partial / unanswerable / injection
  categories; 60/40 dev/test split, results on held-out only, paired
  bootstrap CIs, coverage gate 100%; unanswerable = hard refusal floor.
- **Gate:** the answer-quality suite + the retrieval floors + the injection
  floors all run in CI; docs updated to the measured numbers (no aspirational
  claims).

## Standing rules while building

- ONE shared `HybridSearch`; ONE SQLite connection set; new tables only
  through `core/migrations.py`; no second retrieval path (PHASE7_PLAN §7).
- Keys only via env/secret store; never logged, never on /health//metrics
  (poisoned-key log test from day one).
- Fused scores never gate refusal; raw signals only.
- Every step: failing tests first, then code, then the full suite, then
  commit. The WP14 gate numbers (944/7/623 offline) are the baseline to
  beat — no test is deleted, skipped or loosened.
