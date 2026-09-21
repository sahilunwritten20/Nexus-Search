# Phase 4 — Hybrid Search Implementation Plan

## Architecture Overview

The existing system has:
- **Storage**: SQLite with `documents` and `postings` tables
- **Indexer**: Tokenizes documents, manages postings, emits `indexed`/`deleted` events
- **BM25Search**: BM25 retrieval with filters, phrase boosting, title boosting, snippets, chunk grouping
- **API**: FastAPI with `/documents` (POST/DELETE) and `/search` (GET)
- **Ingestion**: Unified pipeline with dedup, quality gate, chunking, indexer integration
- **Crawler**: Uses same ingestion pipeline via `make_crawler_ingest_fn`

Phase 4 adds:
- **Embedding generation** (using sentence-transformers or similar)
- **Vector index** (SQLite-backed, flat or HNSW)
- **Semantic search** (mode=semantic)
- **Hybrid search** (mode=hybrid) with score normalization + weighted merge
- **Evaluation framework** (Precision@K, Recall@K, MRR, NDCG, latency)

---

## 1. Vector / Embedding Layer

### 1.1 Embedding Model Selection
- Use `sentence-transformers` with `all-MiniLM-L6-v2` (384 dims, fast, good quality)
- Fallback: hash-based embeddings for environments without ML deps
- Model configurable via environment variable

### 1.2 Document Embeddings
- Generate embeddings for document/chunk content at index time
- Store in vector index linked by `doc_id`
- Use Indexer's `indexed` event to trigger embedding generation

### 1.3 Query Embeddings
- Generate embedding for search query at query time
- Cache query embeddings (LRU, small)

### 1.4 Embedding Cache
- SQLite table: `embeddings (doc_id PRIMARY KEY, embedding BLOB, model TEXT, content_hash TEXT, updated_at REAL)`
- Cache hit: content_hash matches current document content
- Cache miss: regenerate embedding
- Invalidation: on document update/delete (via Indexer events)

### 1.5 Incremental Embedding
- Indexer's `indexed` event → check if embedding needs update (content_hash changed)
- Indexer's `deleted` event → remove from vector index

### 1.6 Batch Embedding
- Batch encode multiple texts for efficiency
- Used during initial indexing and bulk operations

---

## 2. Vector Index

### 2.1 Storage Schema
```sql
CREATE TABLE IF NOT EXISTS vectors (
    doc_id TEXT PRIMARY KEY,
    embedding BLOB NOT NULL,  -- serialized float32 array
    dim INTEGER NOT NULL,
    model TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vectors_model ON vectors (model);
```

### 2.2 Vector Index Implementation
- **Phase 4a**: Flat index (brute-force cosine similarity) - simple, correct, good for thousands of docs
- **Phase 4b**: HNSW index (optional, for scale) - can be added later
- Interface: `add(doc_id, embedding)`, `remove(doc_id)`, `search(query_embedding, top_k)`

### 2.3 Consistency
- Vector index operations within same transaction as document indexing where possible
- Indexer events ensure sync even if separate connections

---

## 3. Search Layer

### 3.1 Score Normalization
**BM25 scores**: Unbounded positive, typically 0-10+ depending on corpus
**Vector scores**: Cosine similarity [-1, 1], typically 0-1 for relevant docs

**Normalization strategy**: Min-max normalization per result set
```
normalized_score = (score - min_score) / (max_score - min_score)  if max > min else 0.5
```
Applied separately to BM25 and vector results before merging.

### 3.2 Configurable Weights
- `bm25_weight` (default 1.0)
- `vector_weight` (default 1.0)
- Can be passed per-query or configured globally

### 3.3 Candidate Merging
```
1. Get BM25 candidates (top_k * 2)
2. Get Vector candidates (top_k * 2)
3. Normalize both score sets independently
4. For each unique doc_id: final_score = bm25_w * bm25_norm + vector_w * vector_norm
5. Sort by final_score desc
6. Deduplicate (same doc_id from both)
7. Return top_k
```

### 3.4 Search Modes
- `mode=keyword` → BM25 only (existing behavior, backward compatible)
- `mode=semantic` → Vector only
- `mode=hybrid` → BM25 + Vector merged

### 3.5 BM25 Fallback
- If vector search fails (model unavailable, index error, timeout): log warning, return BM25 results
- Search metadata indicates `fallback: true` and `fallback_reason`
- Never silently hide failures

### 3.6 Duplicate Removal
- By `doc_id` (parent document)
- Chunk-level deduplication via parent_id metadata
- Keep best score among duplicates

---

## 4. API Extensions

### 4.1 Search Endpoint
```
GET /search?q=...&top_k=10&offset=0&mode=hybrid&bm25_weight=1.0&vector_weight=1.0
```

### 4.2 Response Metadata
```json
{
  "query": "...",
  "mode": "hybrid",
  "total_results": 42,
  "bm25_candidates": 20,
  "vector_candidates": 20,
  "merged_candidates": 35,
  "bm25_weight": 1.0,
  "vector_weight": 1.0,
  "fallback": false,
  "fallback_reason": null,
  "latency_ms": 15
}
```

### 4.3 Debug Explanation
```
GET /search?q=...&debug=true
```
Returns per-result breakdown: bm25_score, vector_score, normalized_bm25, normalized_vector, final_score, source (bm25/vector/both)

---

## 5. Evaluation Framework

### 5.1 Benchmark Dataset
- Small deterministic dataset (~50 docs, 20 queries)
- Stored as JSON: `eval_dataset.json`
- Each query has graded relevance judgments (0-3)

### 5.2 Metrics
- **Precision@K**: Relevant retrieved / K
- **Recall@K**: Relevant retrieved / Total relevant
- **MRR**: 1 / rank of first relevant
- **NDCG@K**: Normalized Discounted Cumulative Gain
- **Latency**: Mean/median/p95 over N queries

### 5.3 Comparative Evaluation
- Run each query in keyword/semantic/hybrid modes
- Report metrics for each
- Latency comparison

---

## 6. Testing Strategy

### 6.1 Unit Tests
- Embedding generation (cache hit/miss, incremental)
- Vector index (add, remove, search)
- Score normalization
- Candidate merging
- Search modes

### 6.2 Integration Tests
- Full ingestion → embedding → search flow
- Crawler → ingestion → embedding → search
- Chunk lifecycle with vectors
- Document update → embedding refresh
- Document delete → vector cleanup

### 6.3 API Tests
- All three modes
- Parameter validation
- Metadata/debug endpoints
- Fallback behavior

### 6.4 Regression Tests
- All 196 existing tests must pass
- No behavioral changes to keyword mode

---

## 7. Dependencies

Add to `requirements.txt`:
```
sentence-transformers>=2.2
numpy>=1.24
```

Optional (for HNSW later):
```
hnswlib>=0.7
```

---

## 8. File Structure

```
nexus_search/
├── core/
│   ├── embeddings.py       # Embedding generation + cache
│   ├── vector_index.py     # Vector storage + search
│   ├── hybrid_search.py    # Hybrid search logic
│   ├── api.py              # Extended with mode parameter
│   └── models.py           # Extended response models
├── ingestion/
│   └── pipeline.py         # Minor: ensure embeddings triggered
└── evaluation/
    ├── dataset.py          # Benchmark dataset
    ├── metrics.py          # Precision, Recall, MRR, NDCG
    └── benchmark.py        # Runner + comparison
tests/
├── core/
│   ├── test_embeddings.py
│   ├── test_vector_index.py
│   ├── test_hybrid_search.py
│   └── test_api_phase4.py
├── evaluation/
│   └── test_metrics.py
└── integration/
    └── test_phase4_integration.py
```

---

## 9. Implementation Order

1. **Embeddings module** - model loading, encoding, cache
2. **Vector index** - SQLite storage + flat search
3. **Indexer integration** - subscribe to indexed/deleted events
4. **Hybrid search** - normalization, merging, search modes
5. **API extension** - mode parameter, metadata, debug
6. **Evaluation** - dataset, metrics, benchmark runner
7. **Tests** - unit, integration, API, regression
8. **Validation** - full test suite, benchmarks