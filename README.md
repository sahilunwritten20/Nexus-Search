# Nexus Search

A production-oriented search engine built from the ground up, evolving
from traditional keyword retrieval into a hybrid semantic and AI-powered
search platform.

## Project Overview

Nexus Search is a modular search engine project designed to demonstrate
how modern search systems can be built incrementally.

The project covers:

-   Keyword search and BM25 ranking
-   Unified document ingestion
-   Web crawling
-   Semantic and vector search
-   Hybrid retrieval
-   Advanced ranking
-   Link intelligence
-   AI search and RAG
-   Distributed ingestion
-   Distributed search
-   Production infrastructure

The system is developed phase by phase, with automated testing and
benchmarking used throughout the development process.

------------------------------------------------------------------------

## Running the API

``` bash
pip install -r requirements.txt
uvicorn nexus_search.core.api:app
```

Configuration is via environment variables (see `.env.example`):

| Variable         | Default        | Meaning |
| ---------------- | -------------- | ------- |
| `NEXUS_DB`       | `nexus_search.db` | SQLite file for documents/postings/vectors |
| `NEXUS_ENV`      | `production`   | Anything but `dev` **refuses to boot without `NEXUS_API_KEY`** |
| `NEXUS_API_KEY`  | *(unset)*      | Required on `POST/DELETE /documents` (`X-API-Key` header); unset + non-dev = no boot |
| `NEXUS_CACHE_TTL` | `5`           | `/search` response-cache TTL, seconds |
| `NEXUS_REQUIRE_AUTH_FOR_READS` | `0` | `1` gates GET /search, /documents/{id}, /suggest, /related behind X-API-Key too (needs `NEXUS_API_KEY` to engage) |
| `NEXUS_AUTHORITY_WEIGHT` | `0.0` | Phase 6: link-graph authority weight in rerank (0=inert; 0.1–0.3 typical). Rollback = set to 0 + restart |
| `NEXUS_POPULARITY_WEIGHT` | `0.0` | Phase 6: link-graph popularity weight in rerank. Same rollback |
| `NEXUS_RATE_LIMIT` | `60/minute`  | Per-client limit on `/search` and write endpoints (`0` disables) |
| `NEXUS_TRUST_PROXY` | *(off)*     | `N`: rate-limit key on the Nth-from-right `X-Forwarded-For` entry (reverse-proxy deployments); off = spoofed XFF ignored |
| `NEXUS_ANCHOR_WEIGHT` | `0.0`     | Phase 6: inbound-anchor/query-overlap weight in rerank (same rollout/rollback as authority) |
| `NEXUS_AUTHORITY_RECOMPUTE_INTERVAL` | `0` | Seconds between background PageRank passes (`0` = off; CLI recompute unaffected) |
| `NEXUS_DOMAIN_AUTHORITY_FALLBACK` | `0` | `1`: unknown URLs fall back to their domain's aggregate score |
| `NEXUS_RERANK_WEIGHTS` | *(unset)*    | JSON object overriding any subset of rerank signal weights, clamped [0,1] (e.g. `{"title_match":0.3}`) |
| `NEXUS_SPELL_MAX_TERM_LEN` etc. | see `.env.example` | Spell-correction bounds (BUG-01): term 20, dist-2 12, 8 corrections/query, 4000-comparison budget |
| `NEXUS_MAX_QUERY_TERMS` | `128`       | Max positive terms per query (extras dropped deterministically) |
| `NEXUS_MAX_CANDIDATES` | `1000`       | Fused-pool hard bound; `offset+top_k` past it is a clear 400 |
| `NEXUS_EMBEDDER` | `hash:384`  | offline-safe default; set `st:sentence-transformers/all-MiniLM-L6-v2` for real semantics (model pre-baked in the image) |
| `NEXUS_MAX_QUERY_TERMS` | `128`  | Max positive terms per query; extras dropped deterministically (BUG-01) |
| `NEXUS_SPELL_*` | see `.env.example` | Spell-correction bounds: term len 20, dist-2 len 12, 8 corrections, 4000-comparison budget (BUG-01) |
| `NEXUS_MAX_CANDIDATES` | `1000` | Hard bound on a fused candidate pool; `offset+top_k` past it is a clear 400 (BUG-03/04) |

For local development: `NEXUS_ENV=dev uvicorn nexus_search.core.api:app`
(open write endpoints, loud warning + Swagger UI at `/docs`, which production
hides). Never run an exposed instance without `NEXUS_API_KEY`.

Endpoints: `GET /search` (pagination, `mode=keyword|semantic|hybrid`, facets,
`diversity=`, sort, highlight), `POST/DELETE /documents`, `GET
`/documents/{id}`, `POST /documents/bulk`, `POST /search/explain` (key-gated),
`GET /suggest`, `GET /related`, `GET /health`, `GET /ready`,
`GET /metrics` (key-gated). Query bounds on `/search`: max 2,000 chars and
max 128 words (clear 400 past either — the word bound matches
`NEXUS_MAX_QUERY_TERMS`; a query that long is a pasted document, not a query).

Crawler ops: `python -m nexus_search.crawler.cli block <host> --reason ...`
/ `unblock` / `blocklist`. Normalize legacy link-graph rows (databases
written before BUG-02's fix): `python -m nexus_search.crawler.cli
normalize-links --db $NEXUS_DB --recompute` (idempotent). Reindex after tokenizer changes:
`python -m nexus_search.core.reindex --db $NEXUS_DB --shadow`
(replays in-flight writes during the swap; zero-downtime).

Phase 6 link intelligence:
``` bash
# Recompute authority scores (offline, never on the request path)
python -m nexus_search.crawler.cli authority --recompute --db $NEXUS_DB

# Inspect one URL's scores
python -m nexus_search.crawler.cli authority --show https://example.com

# Graph stats
python -m nexus_search.crawler.cli authority
```
Graph ops: `python -m nexus_search.crawler.cli dead-links|orphans --db $NEXUS_DB`
(read-only reports). Backups: `python -m nexus_search.core.backup --db $NEXUS_DB
--out backups/ --with-frontier` (consistent snapshot under WAL; restore = copy
back while the writer is stopped). A/B log retention:
`python -m nexus_search.ranking.ab purge --db $NEXUS_DB --days 30`.

**Rollout:** set `NEXUS_AUTHORITY_WEIGHT=0.2` (and optionally
`NEXUS_POPULARITY_WEIGHT=0.1`) + restart. **Rollback:** set both to 0 +
restart — the signal is additive and disappears immediately (no reindex
needed, no data migration). Weights ship at 0.0 by default; only turn on
after the eval harness shows uplift (see `evaluation/`).

Docker/CI: `Dockerfile` + `docker-compose.yml` (key required), GitHub
Actions runs the full suite + image build + `/health` probe on every push.

Operations notes:
- The A/B query log (`query_experiments`) records raw query text on every
  `/search` (including cache hits — repeated-query volume is A/B signal).
  It never expires on its own; run a retention purge on a schedule:
  ``` python
  ExperimentLog(db).purge_older_than(days=30)
  ```
- Single-process by design: one uvicorn worker / one container per SQLite
  file. A second process' writes are only picked up on the next
  data-version reload — per-worker query caches and the in-memory vector
  matrix can lag by seconds. Horizontal scale-out needs the Phase 8 shared
  store (pgvector/Qdrant), not more workers here.

Test suite: **760 passed, 7 skipped** offline (hash embedder; skips are
model/benchmark-gated: set `NEXUS_RUN_MODEL_TESTS=1` with the cached MiniLM
for **765 passed, 2 skipped**). CI additionally runs the graph-benchmark
smoke, the authority on/off benchmark, the duplicate-test-name lint and the
env-var documentation check. Run: `python -m pytest -q`.

**Embedder default is lexical, not semantic.** `NEXUS_EMBEDDER` now
defaults to `hash:384` in code, compose AND this table (they agree since the
audit): a deterministic offline hasher — hybrid mode works and degrades honestly, but it is NOT
sentence-transformer semantics. For real semantic vectors set
`NEXUS_EMBEDDER=st:sentence-transformers/all-MiniLM-L6-v2`; the Docker image
pre-bakes this model, so no HuggingFace egress is needed on first boot.

**Packaging:** distribute this repo with `git archive`, not `zip -r .` —
local SQLite artifacts (`nexus_search*.db`, `smoke*.db`) are gitignored for a
reason and must not ship inside tarballs handed to anyone.

The `automation/` directory holds sample n8n workflow exports - reference
material only, not wired into the codebase.

------------------------------------------------------------------------

## Architecture Roadmap

``` text
                    Data Sources
                 /       |       \
                /        |        \
             Files      Web       APIs
                \        |        /
                 \       |       /
                  Unified Ingestion
                         |
                         v
                 Document Processing
                /                    \
               v                      v
          BM25 Search          Vector Search
               \                      /
                \                    /
                 \                  /
                  Hybrid Retrieval
                         |
                         v
                  Advanced Ranking
                         |
                ┌────────┴────────┐
                v                 v
           Search API          AI / RAG
                |                 |
                v                 v
            Results        Answers + Sources
```

------------------------------------------------------------------------

# Development Roadmap

## Phase 1 --- Search Core

### Search Infrastructure

-   Tokenizer
-   Inverted Index
-   SQLite Storage
-   BM25 Search
-   FastAPI API
-   Query Parser
-   Phrase Search
-   Search Filters
-   Title Boosting
-   Phrase Boosting
-   Search Snippets
-   Pagination
-   Top-K Handling
-   Error Handling
-   Automated Tests

### Test Status

-   Phase 1 as shipped had 48 tests; the consolidated suite today is much larger (see bottom)
-   0 tests failed

------------------------------------------------------------------------

## Phase 2 --- Unified Ingestion

### Core Ingestion

-   Text Ingestion
-   Markdown Ingestion
-   Source Code Ingestion
-   CSV Ingestion
-   JSON Ingestion
-   Product Data Ingestion
-   Web Page Ingestion
-   Deduplication
-   Content Hashing
-   Metadata Extraction
-   Canonical URL
-   Unified Document Model

### Document Processing

-   PDF Ingestion
-   DOCX Ingestion
-   XLSX Ingestion
-   PPTX Ingestion
-   Document Chunking
-   MIME-Type Detection
-   Content Quality Scoring

### Test Status

-   Phase 2 as shipped had 63 tests; the consolidated suite today is much larger (see bottom)
-   0 tests failed

------------------------------------------------------------------------

## Phase 3 --- Web Crawler

### Core Crawler

-   URL Frontier
-   URL Normalization
-   robots.txt Support
-   Crawl Delay / Politeness
-   Retry Mechanism
-   Concurrent Crawling
-   Sitemap Parsing
-   Canonical URL Support
-   Crawler → Ingestion Integration
-   Crawler → Index Integration
-   Crawler Tests

### Advanced Crawler

-   ETag Support
-   Last-Modified Support
-   Incremental Recrawling
-   Crawl Scheduling
-   Domain Limits
-   SSRF Protection
-   Crawler Monitoring

### Additional Reliability

-   Thread-safe crawler state
-   SQLite locking protection
-   Incremental crawl metadata
-   Conditional HTTP requests
-   Crawl metrics
-   End-to-end crawler testing

### Test Status (per test file, measured)

-   Frontier: 11 tests
-   URL utils: 10 tests
-   Politeness (robots.txt / crawl-delay): 8 tests
-   Security/SSRF: 6 tests + DNS pinning: 4 tests
-   Fetcher: 4 tests, Sitemap: 2 tests, Scheduler: 4 tests, Metrics: 5 tests
-   End-to-end (local test server): 5 integration tests (full crawl, depth limit, domain limit, incremental recrawl, private host blocked)
-   Chunk lifecycle: 12 tests (grouping, cascade delete, crawl-path chunking, index hooks)
-   Regressions: 19 tests (fetcher/crawler/politeness/dedup/ingest failure paths)
-   Phase 3 as shipped had 90 tests

------------------------------------------------------------------------

# Phase 4 --- Hybrid Search

## Vector / Embeddings

-   Embedding Generation
-   Document Embeddings
-   Query Embeddings
-   Vector Index
-   Embedding Cache
-   Incremental Embedding
-   Batch Embedding

## Search

-   Semantic Search
-   BM25 + Vector Hybrid Search
-   Score Normalization
-   Configurable BM25 Weight
-   Configurable Vector Weight
-   Candidate Merging
-   Duplicate Removal
-   Top-K Retrieval
-   Keyword-Only Mode
-   Semantic-Only Mode
-   Hybrid Mode
-   BM25 Fallback

## Evaluation

-   Search Benchmark Dataset
-   Precision@K
-   Recall@K
-   MRR
-   NDCG
-   BM25 vs Vector Comparison
-   BM25 vs Hybrid Comparison
-   Vector vs Hybrid Comparison
-   Search Latency Benchmark

## API

-   `mode=keyword`
-   `mode=semantic`
-   `mode=hybrid`
-   Search Metadata
-   Debug Search Explanation
-   Automated Tests

### Test Status (per test file, measured)

-   Embedder layer: 20 tests (hash embedder, config, determinism; sentence-transformers cases env-gated)
-   Phase 4 core (hybrid search, fusion math, evaluation metrics, dataset, integration): 18 tests
-   Audit & validation (vector lifecycle, normalization, weight consistency, dedup merging, fallback, batch embedding, maintenance, concurrency, API wiring): 72 tests
-   Phase 4 total: 110 tests, 0 failed

------------------------------------------------------------------------

# Phase 5 --- Advanced Ranking

## Query Understanding

-   Query Normalization
-   Spell Correction
-   Query Expansion
-   Synonym Handling
-   Language Detection
-   Query Intent Detection
-   Entity Extraction
-   Query Rewriting

## Ranking Signals

-   BM25 Score
-   Semantic Similarity
-   Title Match
-   URL Match
-   Phrase Match
-   Freshness
-   Document Quality
-   Content Quality
-   Language Relevance
-   Source Authority
-   Popularity Signals
-   Click Signals

## Ranking System

-   Feature Extraction
-   Feature Normalization
-   Configurable Ranking Weights
-   Candidate Generation
-   Re-Ranking
-   Learning-to-Rank Framework
-   Ranking Model Evaluation
-   A/B Testing

## Search UX

-   Autocomplete
-   Query Suggestions
-   Related Searches
-   Search Highlighting
-   Faceted Filtering
-   Sorting
-   Pagination Improvements

### Test Status (per test file, measured)

-   Query understanding (`tests/ranking/test_query_understanding.py`): 27 tests
-   Signals (`tests/ranking/test_signals.py`): 24 tests
-   Features + shared normalizers (`tests/ranking/test_features.py`): 7 tests
-   Ranker / LTR framework / A-B instrumentation (`tests/ranking/test_ranker.py`): 18 tests
-   Suggestions & UX internals (`tests/ranking/test_suggestions.py`): 12 tests
-   API surface (`TestApiPhase5Ux` in `tests/core/test_api.py`): 12 tests
-   Phase 5 total: 100 new tests, 0 failed (311 pre-existing Phase 1-4 tests still pass unchanged)

Honest scope notes (per SPEC.md): learning-to-rank ships as framework +
weighted-sum model only (no labeled data exists to train one); A/B ships as
instrumentation (deterministic bucketing + query log) — real analysis needs
production traffic; authority/popularity/click signals are placeholder
interfaces awaiting Phase 6/7 data sources.

------------------------------------------------------------------------

# Phase 6 --- Link Intelligence

Implemented (verified by tests — status markers added by the audit
remediation; every bullet below matches shipped code):

-   [x] Outgoing Link Extraction
-   [x] Source URL Storage
-   [x] Destination URL Storage (normalized: fragments/utm/case/ports
      collapse — one node per page)
-   [x] Anchor Text Storage
-   [x] Link Metadata (rel flags, first/last seen)
-   [x] Internal Links (is_internal: same exact host, migration-backfilled)
-   [x] External Links (per-page internal/external counts)
-   [x] URL Graph
-   [x] Graph Storage (versioned migrations v1-v3, WAL, anti-flood caps)
-   [x] Graph Traversal (neighbors out|in|both, bounded iterative BFS,
      key-gated GET /graph/neighbors)
-   [x] Connected Components (iterative union-find, deterministic ids)
-   [x] Dead-Link Detection (frontier 4xx cross-ref; CLI dead-links; never fetches)
-   [x] Orphan-Page Detection (CLI orphans; seeds excluded)
-   [x] PageRank
-   [x] Iterative PageRank (d=0.85, dangling redistribution, tol 1e-6)
-   [x] Page Authority
-   [x] Domain Authority Signals (PR-weighted aggregate; fallback behind
      NEXUS_DOMAIN_AUTHORITY_FALLBACK, default off)
-   [x] Link Weight Calculation (nofollow zero-pass, reciprocal 0.25)
-   [x] Anchor-Text Relevance (url_anchors aggregation; NEXUS_ANCHOR_WEIGHT,
      default 0.0, zero-weight identity test-pinned)
-   [x] Link Quality Signals (nofollow/sponsored/ugc + reciprocal discount)
-   [x] Spam/Link Manipulation Detection (heuristics: caps, reciprocal
      discount, domain-diversity weighting; NO ML classifier — documented
      limit, distributed farms can still defeat it)
-   [x] Link Score in Ranking
-   [x] Configurable Authority Weight
-   [x] Ranking Evaluation (evaluation/authority_benchmark.py)
-   [x] Before/After PageRank Benchmark (mission shape: NDCG@10
      0.541 -> 1.000 with weights on)
-   [x] Incremental Graph Updates (edge-set version counter; recompute
      skipped when nothing changed; anchor/rel changes count)
-   [x] Background PageRank (NEXUS_AUTHORITY_RECOMPUTE_INTERVAL, default
      off; overlap-guarded worker, clean lifespan shutdown)
-   [x] Graph Caching (in-memory authority read cache, invalidated on
      in-process swap + cross-process data_version polling)
-   [x] Large Graph Benchmark (10k pages / 100k edges: recompute 3.2s
      against the committed 30s bound; smoke mode in CI)
-   [x] Failure Recovery (shadow-swap scores; concurrent-writes test)

------------------------------------------------------------------------

# Phase 7 --- AI Search / RAG

-   Document Embeddings
-   Chunk Embeddings
-   Retrieval Pipeline
-   Context Selection
-   RAG Pipeline
-   LLM Integration
-   Answer Generation
-   Source Citations
-   Citation → Document Mapping
-   Citation Verification
-   Hallucination Reduction
-   Answer + Sources API
-   Streaming Responses
-   Conversation Context
-   RAG Quality Evaluation
-   Answer Quality Evaluation

------------------------------------------------------------------------

# Phase 8 --- Distributed Ingestion

-   Kafka
-   Event-Driven Ingestion
-   Distributed Crawler Workers
-   Distributed Indexing Workers
-   Redis
-   Job Queues
-   Retry Queues
-   Dead-Letter Queues
-   Worker Health Monitoring
-   Backpressure
-   Horizontal Scaling

------------------------------------------------------------------------

# Phase 9 --- Distributed Search

-   Search Shards
-   Document Sharding
-   Vector Sharding
-   Replication
-   Replica Selection
-   Distributed Query Execution
-   Result Merging
-   Fault Tolerance
-   Node Failure Handling
-   Rebalancing
-   Horizontal Scaling
-   Load Balancing
-   Distributed Caching

------------------------------------------------------------------------

# Phase 10 --- Production Infrastructure

## Deployment

-   Docker
-   Docker Compose
-   Kubernetes
-   Helm
-   Cloud Deployment
-   CI/CD
-   Staging Environment
-   Production Environment

## Observability

-   Structured Logging
-   Metrics
-   Prometheus
-   Grafana
-   Distributed Tracing
-   OpenTelemetry
-   Error Tracking
-   Alerting

## Security

-   Authentication
-   Authorization
-   RBAC
-   API Keys
-   Rate Limiting
-   Secrets Management
-   HTTPS/TLS
-   Input Validation
-   SSRF Protection
-   Audit Logging

## Reliability

-   Health Checks
-   Readiness Probes
-   Liveness Probes
-   Autoscaling
-   Database Backups
-   Disaster Recovery
-   Data Retention
-   Graceful Shutdown
-   Zero/Low-Downtime Deployment

------------------------------------------------------------------------

# Testing Strategy

Nexus Search uses automated testing throughout development.

Testing areas include:

-   Unit testing
-   Integration testing
-   Ingestion testing
-   Crawler testing
-   API testing
-   End-to-end testing
-   Search quality evaluation
-   Performance benchmarking

Run the complete test suite:

``` bash
python -m pytest -v
```

Run ingestion tests:

``` bash
python -m pytest tests/ingestion -v
```

Run crawler tests:

``` bash
python -m pytest tests/crawler -v
```

------------------------------------------------------------------------

# Technology Stack

Current and planned technologies include:

-   Python
-   FastAPI
-   SQLite
-   BM25
-   pytest
-   Web crawling
-   Document processing
-   Vector embeddings
-   Vector search
-   Redis
-   Kafka
-   Docker
-   Kubernetes
-   Prometheus
-   Grafana
-   OpenTelemetry

Technologies are introduced as their corresponding architecture phases
are implemented.

------------------------------------------------------------------------

# Project Structure

``` text
nexus-search/
│
├── nexus_search/          # the package
│   ├── core/              # tokenizer, storage, BM25, hybrid/vector search, API
│   ├── ingestion/         # connectors, dedup, chunker, quality, pipeline
│   ├── crawler/           # frontier, fetcher, politeness, security, pipeline
│   └── evaluation/        # benchmark dataset + metrics runner
│
├── tests/                 # test suite (core / ingestion / crawler / e2e)
│   ├── core/
│   ├── ingestion/
│   └── crawler/
│
├── docs/                  # phase plans & changelogs
├── scripts/dev/           # local dev/debug scratch scripts (kept out of the way)
│
├── crawler_config.yaml    # crawler defaults
├── requirements.txt
├── pytest.ini
├── seeds.example.txt      # copy to seeds.txt (git-ignored) to crawl
├── .env.example           # copy to .env (git-ignored) for configuration
├── SPEC.md
└── README.md
```

The repository structure will expand as semantic search, advanced
ranking, AI/RAG, and distributed components are introduced.

------------------------------------------------------------------------

# Project Goal

The long-term goal of Nexus Search is to develop a complete modern
search platform capable of:

1.  Ingesting documents from multiple sources.
2.  Crawling and continuously updating web content.
3.  Performing keyword retrieval using BM25.
4.  Performing semantic vector retrieval.
5.  Combining keyword and semantic retrieval.
6.  Applying advanced ranking signals.
7.  Understanding links and document authority.
8.  Generating AI-powered answers with citations.
9.  Scaling ingestion across distributed workers.
10. Scaling search across distributed nodes.
11. Providing production-grade security, observability, and reliability.

------------------------------------------------------------------------

# Development Philosophy

The project follows an incremental engineering process:

``` text
Build
  ↓
Test
  ↓
Benchmark
  ↓
Improve
  ↓
Integrate
  ↓
Scale
```

Existing working components are preserved while new capabilities are
added on top of the architecture.

------------------------------------------------------------------------


## Roadmap Summary

``` text
Phase 1   Search Core
Phase 2   Unified Ingestion
Phase 3   Web Crawler
Phase 4   Hybrid Search
Phase 5   Advanced Ranking
Phase 6   Link Intelligence
Phase 7   AI Search / RAG
Phase 8   Distributed Ingestion
Phase 9   Distributed Search
Phase 10  Production Infrastructure
```

------------------------------------------------------------------------

## License

This project is currently under development.

