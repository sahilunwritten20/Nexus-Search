# Nexus Search — Spec (Phase 0)

## What this is

A unified search engine spanning **web pages, documents/files, products,
and code** — one search layer over everything, not a single-domain tool.

## Goals

- Learn how a real search engine works end to end (tokenization, inverted
  indexes, ranking, crawling, retrieval)
- A portfolio piece that demonstrates genuine systems engineering, not a
  thin wrapper around an API
- A real product with startup potential
- Something embeddable into an existing app

## Scale target

Prototype scale — thousands of items, not web-scale. SQLite is the store
through Phase 7; Phase 8-10 is what changes that if/when it's needed.

## Non-negotiables

- Every phase ships with real tests, not just code that looks plausible
- Integration points between phases are real function calls, not stubs
  left for "later"
- Honesty over completeness theater: if something can't be verified in a
  given environment (e.g., no network access to install a package, no
  live cluster to deploy to), that gets said plainly rather than papered
  over

## Full roadmap

| Phase | Name | Status |
|---|---|---|
| 0 | Spec | This file |
| 1 | Search core | **Done + upgraded** |
| 2 | Unified ingestion | **Done + upgraded** |
| 3 | Crawler | **Done + upgraded** |
| 4 | Hybrid BM25 + vector search | Next |
| 5 | Ranking | Planned |
| 6 | Link intelligence | Planned |
| 7 | AI-cited answers | Planned |
| 8-10 | Production infra (Kafka/Redis/K8s/sharding/monitoring) | Planned — needs real cloud infra to run |

See the main README for what's actually in Phases 1-3 as built.


## Phase 1-3 upgrade contract

Before Phase 4 begins, the foundation supports:

### Phase 1
- BM25 lexical retrieval
- query parsing for phrases and `type:` / `lang:` filters
- title-aware relevance boosting
- phrase relevance boosting
- query-focused snippets
- bounded `top_k` API input

### Phase 2
- unified `IngestDoc` abstraction
- content-hash deduplication
- files, code, products, and web connectors
- canonical URL and language metadata for web documents
- document metadata preserved for downstream ranking/filtering

### Phase 3
- persistent URL frontier
- URL normalization
- robots.txt and crawl-delay handling
- retries and concurrent fetching
- sitemap XML parsing helper
- canonical URL extraction
- real crawler → ingestion → index → BM25 integration

The next major architectural jump is Phase 4: hybrid lexical + semantic retrieval.
