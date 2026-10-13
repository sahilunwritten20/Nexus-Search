# Architecture — written from CODE by the review/hardening audit

This document describes what the code ACTUALLY does (every claim below was read
from the referenced file during the audit). It is the reference for the
docs-vs-code diff in the claims ledger. Last full source sweep: 2026-10-13.

## Processes / entry points

| Process | Entry | Role |
|---|---|---|
| API server | `uvicorn nexus_search.core.api:app` | FastAPI app; single process per SQLite file BY DESIGN (README:159-163) |
| Crawler CLI | `python -m nexus_search.crawler.cli` | crawl / block / unblock / blocklist / authority / normalize-links / dead-links / orphans |
| Ingest CLI | `python -m nexus_search.ingestion.cli` | files / code / product batch ingest; DLQ list/replay |
| RAG debug CLI | `python -m nexus_search.rag ask\|calibrate` | offline RAG pipeline debugging |
| Vector maintenance | `python -m nexus_search.core.vector_maintenance` | backfill/GC embeddings |
| Reindex | `python -m nexus_search.core.reindex --shadow` | zero-downtime postings rebuild |
| Backup | `python -m nexus_search.core.backup` | online sqlite3 backup API snapshot |
| A/B log retention | `python -m nexus_search.ranking.ab purge\|count` | query-log retention |
| Benchmarks | `python -m nexus_search.evaluation.{benchmark,scale_benchmark,graph_benchmark,authority_benchmark,rerank_benchmark,semantic_benchmark,rag_benchmark}` | eval harnesses |

## Background threads (all in-process)

1. `_AuthorityWorker` (core/api.py:157-194) — daemon thread, opt-in
   `NEXUS_AUTHORITY_RECOMPUTE_INTERVAL>0`; guarded single pass
   (`compute_authority`); stops itself loudly on first failure; joined on
   lifespan shutdown.
2. `EmbeddingSync` (core/embedding_sync.py) — not a thread; a lock-guarded
   queue flushed inline on `add_document` events/batch/`flush()`.
3. Crawler worker pool (crawler/pipeline.py:313) — `ThreadPoolExecutor`
   (default 8) for concurrent fetches; indexing serialized by
   `make_crawler_ingest_fn`'s lock.
4. StubProvider per-call worker thread (rag/provider.py:340) — enforces the
   stub's wall-clock deadline.
5. `/ask` non-stream & `/ask/stream` provider watchdog threads —
   rag/pipeline.py:431-441 (`_call` thread + `worker.join(timeout)`).
6. `_pinned_getaddrinfo` thread-local pins (crawler/security.py) — patched
   `socket.getaddrinfo` after `install_dns_pinning()` (crawler paths only).

## SQLite databases, tables, indexes

### Main DB (`NEXUS_DB`, default `nexus_search.db`) — store "storage" (v4)
- `documents(doc_id PK, title, content, doc_type, length, metadata JSON, added_at)`
- `postings(term, doc_id, term_freq; PK(term,doc_id); idx_postings_term(term), idx_postings_doc(doc_id))`
- idx_documents_added_at(added_at)  [v2]
- `rag_token_usage(bucket, day, tokens; PK(bucket,day))` [v3]
- `rag_sessions(scope, session_id, turn_idx, role, text, updated_at; PK(scope,session_id,turn_idx))` [v4]
- `schema_version(name PK, version)` (core/migrations.py)
- WAL + busy_timeout=5000; one shared connection + RLock (core/storage.py:53-56)

### Main DB — store "doc_vectors" (v1) (core/vector_store.py)
- `doc_vectors(doc_id, model, dim, content_hash, vector BLOB, updated_at; PK(doc_id,model))`
- idx_doc_vectors_model_hash(model,content_hash); idx_doc_vectors_updated(updated_at)
- in-memory `np.float32` matrix (amortized buffers, `_id_to_idx` map,
  `_mem_version` swap guard), reloaded on `PRAGMA data_version` change.

### Main DB — store "content_hashes" (v1) (ingestion/dedup.py)
- `content_hashes(hash PK, doc_id); idx_hashes_doc(doc_id)`

### Main DB — store "failed_ingestions" (v1) (ingestion/failures.py)
- `failed_ingestions(id, doc_id, title, content, doc_type, metadata, error, attempts, next_retry_at, created_at)`
- idx_failed_ingestions_due(next_retry_at)

### Main DB — store "content_history" (v1) (ingestion/history.py)
- `content_history(id, doc_id, prev_hash, prev_content, new_hash, recorded_at)`
- idx_content_history_doc(doc_id)

### Main DB — store "link_graph" (v3) (links/graph.py)
- `link_edges(from_url, to_url, anchor_text, rel_attrs, first_seen, last_seen, from_domain [v2], to_domain [v2], is_internal [v3]; PK(from_url,to_url))`
- idx_link_edges_to(to_url); idx_link_edges_from(from_url);
  idx_link_edges_domains(from_domain,to_domain) [v2]
- `authority_scores(url PK, pagerank, inlink_count, inlink_domains, authority, popularity, computed_at)` (rebuilt via shadow-table swap)
- `url_anchors(url PK, anchor_text)` [v3]
- `domain_authority(domain PK, authority, popularity, pages, computed_at)` [v3]
- `graph_meta(key PK, value)` [v3] — `edges_version`, `computed_version`
- `query_experiments(id, query, variant, ranking_mode, created_at)` — Phase 5 A/B log (ranking/ab.py; created via executescript, not the migration runner)

### Frontier DB (`<db>_frontier.db`) — store "frontier" (v2)
- `frontier(url PK, depth, priority, status, added_at, retry_count [v2], next_retry_at [v2])`
- idx_frontier_status_priority(status, priority DESC, depth ASC)
- `visited(url PK, last_crawled_at, content_hash, etag, last_modified)`
- idx_visited_last_crawled(last_crawled_at)
- `crawl_errors(url, error, timestamp)`
- blocklist store (v1): `blocklist(host PK, reason, created_at)`

### Migrations runner (core/migrations.py)
Per-store versions in `schema_version`; each migration runs in one
BEGIN IMMEDIATE txn; idempotent SQL required; failures raise (no half-boot).

## Data flow

Ingestion (CLI/crawler) → `IngestDoc` → dedup (content-hash + canonical) →
quality gate → chunker (doc_id#chunkN, metadata.parent_id) →
`Indexer.add_document` → postings+document row (atomic) → `EmbeddingSync`
event → hash/ST embedding → doc_vectors (content-hash-gated) → in-memory
matrix.

/search: query → parse_query (memoized) → [understanding: spell-correct +
synonyms, opt-in] → BM25 postings batch + vector matmul (allowed_ids
pushdown) → phrase/boolean gates → fusion (RRF k=60 or weighted min-max) →
sort/diversity → optional Phase 5 rerank (weighted-sum model; retrieval
contributions preserved) → page slice → facets (2nd pass, 500-doc cap) →
QueryCache (TTL 5s, cleared on writes).

/ask: bounds → budget precheck (shared SQLite, LOCAL-TIME day key — see
finding STORAGE-2) → retrieval (same shared HybridSearch, group_chunks=False)
→ refuse-gate (raw bm25/vector floors per embedder, frozen in
rag/gate_floors.json) → context selection (llm_allowed filter, per-parent
cap, Jaccard dedup, hard budgets, ±1 neighbor stitching) → nonce-fenced
prompt (injection strip, homoglyph normalization, optional PII redaction)
→ provider (stub/openai/anthropic; retries: connect/429/5xx only; semaphore)
→ citation verification (Storage re-fetch) → support/quote checks vs AS-SENT
→ sanitize (HTML/md-link/script strip; prompt-leak scrub) → settle budget.

Crawler: frontier (priority/depth) → blocklist → SSRF validate_url →
robots (fail-closed when unreachable) → per-domain slot (crawl-delay) →
fetch (manual redirects, every hop validated, DNS pinning installed,
content-type must be text/html, 5MB cap) → extract_page (ONE canonical HTML
parser, shared with ingestion) → ingest (serialized) → mark_done →
links → graph.record_edge (only after successful fetch+ingest).

## Network calls (complete list)

1. Crawler `requests.Session.get` — external http(s), robots/sitemaps/pages.
2. `requests.post` — LLM provider (OpenAI/Anthropic adapters), auth header only.
3. Optional Playwright Chromium render (`NEXUS_RENDER_JS=1`) — every
   subrequest route-validated; SW blocked; residual: browser-resolved DNS.
4. `socket.getaddrinfo` — DNS (patched with pin-aware resolver on crawler).
5. ST model load (local HF cache; `HF_HUB_OFFLINE=1` in Docker).

Everything else is offline by construction (hash embedder default).

## Trust boundaries

- HTTP clients (crawler fetches, provider calls) — untrusted responses.
- Ingested document CONTENT — untrusted; reaches: BM25 index, vector store,
  RAG prompts (injection-stripped, nonce-fenced), snippets (HTML-escaped),
  link graph (normalized).
- API inputs: query strings (bounded 2000 chars/128 words), doc writes
  (10MB content cap, 100KB metadata), session ids (charset/length regex),
  filters (2 known keys), cursors (base64 offset).
- API key / LLM key: env only; compared constant-time; never logged
  (poisoned-key tests); verified keys get separate rate/budget buckets.
- `X-Forwarded-For` trusted only behind explicit `NEXUS_TRUST_PROXY` hops.
- LLM output — untrusted: sanitized, citation-verified, leak-scrubbed.

## Files read/written

- SQLite DBs (above). Backups write `<out>/<label>-<stamp>.db`.
- Ingest CLI reads arbitrary files under `--path` (size-capped 64MB,
  content-sniffed). Crawler_config.yaml, seeds.txt.
- `.env.example` is documentation; no .env is read by the code itself
  (compose injects).
- `rag/gate_floors.json` — read at runtime (packaged).
- Logs: stdout/stderr only (no log files).

## Single-process contract

One uvicorn worker per SQLite file; cross-process writes are picked up by
`PRAGMA data_version` polling (VectorStore, LinkGraph cache). RAM RAG
sessions need sticky routing; shared budget state is SQLite. Crawler
politeness is in-process only (documented).

## Known discrepancies vs docs (from the claims ledger work)

- README test counts stale (real: 1168 passed / 13 skipped offline; 1176/5
  with NEXUS_RUN_MODEL_TESTS=1).
- README env table `NEXUS_MAX_DECOMPRESSED_BYTES 536870912` — code and
  .env.example both say 67108864 (64 MiB).
- budget.py/api.py comments say budgets are per **UTC** day;
  core/storage.py `_day_key` uses **localtime** — behavior is local-time.
