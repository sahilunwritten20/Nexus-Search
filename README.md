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
| `NEXUS_RATE_LIMIT` | `60/minute`  | Per-client limit on `/search` and write endpoints (`0` disables) |
| `NEXUS_EMBEDDER` | *(auto)*       | `hash:384` (offline) or `st:all-MiniLM-L6-v2` |

For local development: `NEXUS_ENV=dev uvicorn nexus_search.core.api:app`
(open write endpoints, loud warning + Swagger UI at `/docs`, which production
hides). Never run an exposed instance without `NEXUS_API_KEY`.

Endpoints: `GET /search` (pagination, `mode=keyword|semantic|hybrid`, facets,
`diversity=`, sort, highlight), `POST/DELETE /documents`, `GET
/documents/{id}`, `POST /documents/bulk`, `POST /search/explain` (key-gated),
`GET /suggest`, `GET /related`, `GET /health`, `GET /ready`,
`GET /metrics` (key-gated).

Crawler ops: `python -m nexus_search.crawler.cli block <host> --reason ...`
/ `unblock` / `blocklist`. Reindex after tokenizer changes:
`python -m nexus_search.core.reindex --db $NEXUS_DB --shadow`
(replays in-flight writes during the swap; zero-downtime).

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

Test suite: **592 passed, 3 skipped** (595 collected; offline;
sentence-transformers cases self-skip unless `NEXUS_RUN_MODEL_TESTS=1`).
Per phase: core 195 · crawler 108 · ingestion 107 · ranking 91 ·
evaluation 2 · end-to-end/regressions/lifecycle/audit 108.
Run: `python -m pytest -q`.

**Embedder default is lexical, not semantic.** The default
`NEXUS_EMBEDDER=hash:384` (also the docker-compose default) is a deterministic
offline hasher — hybrid mode works and degrades honestly, but it is NOT
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

-   Outgoing Link Extraction
-   Source URL Storage
-   Destination URL Storage
-   Anchor Text Storage
-   Link Metadata
-   Internal Links
-   External Links
-   URL Graph
-   Graph Storage
-   Graph Traversal
-   Connected Components
-   Dead-Link Detection
-   Orphan-Page Detection
-   PageRank
-   Iterative PageRank
-   Page Authority
-   Domain Authority Signals
-   Link Weight Calculation
-   Anchor-Text Relevance
-   Link Quality Signals
-   Spam/Link Manipulation Detection
-   Link Score in Ranking
-   Configurable Authority Weight
-   Ranking Evaluation
-   Before/After PageRank Benchmark
-   Incremental Graph Updates
-   Background PageRank
-   Graph Caching
-   Large Graph Benchmark
-   Failure Recovery

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

