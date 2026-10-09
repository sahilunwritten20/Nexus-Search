# Phases 1–6 Completion Checklist (WP13 sign-off, 2026-10-09)

One line per phase; every claim is backed by a test, a benchmark or a
measured run executed at the WP13 head (`git log` carries the commits).
Ticks mark ONLY what was verified in this work package's runs.

| Phase | Shipped features | Test files (verified green in the WP13 runs) | Benchmark / evidence | Known limits (documented) |
|---|---|---|---|---|
| 1 — Search Core | Tokenizer, inverted index, SQLite storage, BM25 (+filters, phrase, title boost, snippets), FastAPI, query parser, response cache, rate limiting | tests/core/test_api.py, test_bm25.py, **test_golden_keyword.py** (baseline byte-identical through WP13), test_query_cache.py, test_search_dos.py, test_fresh_boot.py, test_pagination_contract.py, test_shared_searcher.py | Golden keyword snapshot: 15 queries / 54 results, unchanged since WP0; junk-query CPU 43.12 s → 5 ms (WP1) | 128-term/128-word query bounds; SQLite single writer (Phase 8 shared store) |
| 2 — Unified Ingestion | files/code/product/web connectors, chunker, dedup, failure ledger, MIME magic-byte routing, markdown reader, OCR opt-in, zip-bomb + PDF-page guards | tests/ingestion/ (17 files incl. **test_wp13_encoding.py** 75-row table + WP11 differential + detector-stub matrix; test_encoding.py P0-4/B6) | Encoding: 75/75 table rows exact round-trip on charset-normalizer 3.5.1 AND 3.4.6; WP11 parity 51/51; reviewer's 14 blocking samples 0 mojibake (WP12: 12) | Genuine cp1257 Baltic relies on detector top-guess (letters all collide with cp1252); very short samples (<64 B) logged at INFO as statistically weak; bulk body size is a proxy-level concern |
| 3 — Web Crawler | frontier (4xx-terminal, WAL), politeness, robots (fail-closed), sitemap, SSRF validation + DNS pinning, render_js (gated, subrequest-validated), blocklist, CLI | tests/crawler/ (fetcher, frontier, pipeline, robots, sitemap, security, url_utils) | **Real public crawl (WP9): 25/25 pages, 0 errors, 137,541 B, 512 real edges, authority converged 7 iters** — docs/REAL_CRAWL_REPORT.md | robots wildcards approximated; render_js residual DNS-rebinding documented (Chromium resolves its own DNS); per-URL frontier commits (deferred ledger) |
| 4 — Hybrid Search | hash + ST embedders, vector store (row-index, lock-outside-fill), hybrid RRF/weighted fusion, min_score, explain, filter pushdown, batched stages | tests/core/test_embedders.py, test_hybrid*.py, test_wp12_pushdown.py, test_wp12_n_plus_one.py, test_wp12_row_index.py; tests/evaluation/test_semantic_benchmark.py | WP10 labeled fixture (336 docs / 77 queries): st beats hash on paraphrase NDCG@10 0.625 vs 0.351; CI floors (keyword 0.55, hash 0.65); Scale bench (WP13): selective hybrid 3.1/9.8/27.0 ms at 1K/5K/10K (2.8% of docs match); match_all worst case labelled (1,384 ms @10K) | Hash embedder is the default (lexical, not semantics); single-process vector matrix (Phase 8 for shared) |
| 5 — Advanced Ranking | 13-signal reranker (+NEXUS_RERANK_WEIGHTS), spell correction (budgeted), suggestions (prefix + related w/ inverted-gram index, WP13), A/B experiment log, facets, diversity, hybrid pagination contract | tests/ranking/ (test_ranker.py, test_spell_budget.py, test_suggestions.py incl. WP13 scan-cost parity, test_ab.py), test_query_parser.py | Hybrid pagination: offset=50 → full page, 120-doc walk no gaps/dups; N+1 per query 936 → 0 (WP12); related_searches fallback O(vocab)/request → O(query grams) (WP13, parity-pinned) | Click signals + LTR training = Phase 7+; A/B analysis needs production traffic; per-worker rate limiter (Phase 8 shared state) |
| 6 — Link Intelligence | link graph v1–v3 (normalized nodes, anti-flood caps, domain-pair cap), PageRank + reciprocal/nofollow/domain-diversity corrections, authority/popularity rerank signals, traversal/components/dead-links/orphans, background recompute, graph cache | tests/links/ (test_graph_normalization.py, test_domain_cap.py, test_graph_analysis.py, test_graph_api.py), tests/evaluation/test_graph_benchmark.py | Graph benchmark: 10K pages / 100K edges recompute 3.2 s < 30 s bound; authority benchmark: NDCG@10 0.541 → 1.000 with signal on; URL-variant authority merge (6 inlinks → one node) | Exact-hostname domain-diversity (~10 subdomains saturate; PSL dep deliberately not added); heuristics only, no ML spam classifier; validated on synthetic + one real sandbox crawl, not web-scale farms |

## Cross-phase verification (executed at the WP13 head, real output)

- Offline suite (Python 3.14.7, Windows 11): **844 passed, 7 skipped, 614 subtests** —
  identical outcomes on **Python 3.12.14** (844/7/614; the only delta is
  warning volume: slowapi's `asyncio.iscoroutinefunction` deprecation is
  3.14-only and upstream).
- Model-gated suite (`NEXUS_RUN_MODEL_TESTS=1`, cached MiniLM):
  **849 passed, 2 skipped**.
- Load smoke (`NEXUS_RUN_LOAD_SMOKE=1`): **1 passed** (mixed 200-request
  load, p95 bounded, zero 5xx).
- Graph bench (`NEXUS_RUN_GRAPH_BENCH=1`): **15 passed, 2 skipped**.
- Golden keyword baseline: **byte-identical** through every WP13 change.
- Docker (local, was "unverified" since WP12): `docker compose build` OK,
  container boots, **`GET /ready` → 200 `{"ready":true}`**, `GET /health` →
  200 `{"status":"ok",...}`.
- Packaging: `scripts/package.ps1` (git archive) — 234 entries, zero
  `.db`/`__pycache__`/`.git`/`.swp` junk; stray `nexus_search_frontier.db`
  and `.git/.COMMIT_EDITMSG.swp` deleted; `.gitignore`/`.gitattributes`
  already cover the classes.
- CI: full suite as a matrix on charset-normalizer **3.5.1 + 3.4.6**
  (Ubuntu / Python 3.12) + docker job — see AUDIT_REMEDIATION WP13 gate
  for the run result.

## Phases 1–6 verdict

**Complete.** Every phase's shipped feature list matches code that is
test-pinned; the known limits above are documented, deliberate and
carried into the phase plans that own them (Phase 7: RAG; Phase 8:
distributed + shared state; Phase 10: ops).
