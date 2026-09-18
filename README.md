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

-   113 tests passed
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

-   Full ingestion test suite completed
-   59 tests passed

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

### Test Status

-   Frontier tests completed
-   10 Frontier tests passed
-   End-to-end coverage includes crawling, depth limits, domain limits,
    incremental recrawling, and SSRF protection

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
├── nexus_search/
│   ├── ingestion/
│   ├── crawler/
│   ├── search/
│   └── ...
│
├── tests/
│   ├── ingestion/
│   ├── crawler/
│   └── ...
│
├── crawler_config.yaml
├── README.md
└── ...
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

