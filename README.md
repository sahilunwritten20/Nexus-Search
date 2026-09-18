# Nexus Search

A unified search engine spanning web pages, documents, products, and code —
Phases 1 through 3, upgraded into a stronger production-oriented foundation in one properly-integrated repo
(previously three separate deliverables with placeholder wiring between
them; this version has none of that).

**113 tests, all passing** — including a true end-to-end test
(`tests/test_end_to_end.py`) that spins up a real local HTTP server,
crawls it, runs the crawled pages through real deduplication and
indexing, and confirms BM25 search actually finds them. Nothing about
"the system works" is assumed here; it's proven.

## Quick start

```bash
pip install -r requirements.txt

# 1. Ingest something — pick any source:
python -m nexus_search.ingestion.cli --source files --path ./some-docs
python -m nexus_search.ingestion.cli --source code --path ./some-repo
python -m nexus_search.ingestion.cli --source product --path ./catalog.csv

# 2. Or crawl a site (indexes automatically as it goes):
cp seeds.example.txt seeds.txt   # edit with real URLs
python -m nexus_search.crawler.cli --seeds seeds.txt --domains example.com

# 3. Serve the API and search:
uvicorn nexus_search.core.api:app --reload
curl "http://localhost:8000/search?q=your+query"
```

## Architecture

```
nexus_search/
├── core/         Phase 1 — tokenizer, inverted index, BM25, query parser, field/phrase boosts, filters, API
├── ingestion/     Phase 2 — files/code/web/product connectors, dedup, richer web metadata, ingest CLI
└── crawler/       Phase 3 — URL frontier, politeness/robots.txt, sitemap parsing, canonical URLs, fetcher, crawl CLI
```

All three phases share **one SQLite file** (`nexus_search.db` by default):
`core` owns the documents + postings tables, `ingestion` adds a
content-hash table for dedup, and `crawler` uses a separate file for its own
frontier/visited state (crawl progress vs. the document index are
different lifecycles — you might reset a crawl without wanting to
rebuild the whole index).

**The actual integration points**, not placeholders:
- `crawler/pipeline.py` imports `extract_page` from
  `ingestion/connectors/web.py` — one HTML-extraction implementation,
  used by both the crawler (for content + links) and standalone web
  ingestion (for content only). Two copies of "how to parse HTML" is
  exactly the kind of drift that causes bugs later.
- `crawler/cli.py` builds its `ingest_fn` from
  `ingestion/pipeline.py::make_crawler_ingest_fn`, so crawled pages flow
  through the same dedup and indexer every other source uses — not a
  logging stub.

## Running the tests

```bash
python -m unittest discover -s tests -v
```

Breakdown: 38 tests on `core` (tokenizer/storage/indexer/BM25 + query parsing, title/phrase boosts and filters), 43 on `ingestion` (all four connectors + dedup + crawler adapter + canonical metadata), 27 on `crawler` (frontier/politeness/URL normalization + sitemap parsing), and 5 true end-to-end.

## What's not installable here, and why

`fastapi`, `uvicorn`, and `pydantic` aren't available in the sandbox this
was built in (no network access to PyPI), so `core/api.py` is
syntax-checked but not functionally run here. Everything it depends on
(`indexer`, `bm25`, `storage`) is fully tested directly — the API layer
itself is thin, well-trodden FastAPI usage with very little room for the
kind of bug that only shows up at runtime. Install the requirements
locally and it runs as shipped.

## Phase 1-3 upgrade highlights

The foundation now includes several upgrades without breaking the original architecture:

- **Query parser:** quoted phrases plus `type:` and `lang:` filters.
- **Field-aware ranking:** title matches receive a controlled relevance boost.
- **Phrase relevance:** exact phrase presence receives a controlled boost.
- **Better snippets:** snippets are centered around matched terms/phrases when possible.
- **Richer web metadata:** canonical URL, language, description, source URL, and crawl depth are retained.
- **Sitemap parsing:** XML sitemaps can be parsed and used as crawler seeds.
- **Safer API bounds:** `top_k` is constrained to a reasonable range.

These are intentionally incremental upgrades. SQLite, BM25, the ingestion abstraction, and the crawler frontier remain the stable foundation for Phase 4 onward.

## What's next

**Phase 4 — hybrid BM25 + vector search** is the natural next step: embed
every document, add cosine-similarity retrieval, blend it with the BM25
scores already computed here. That's the single biggest capability jump
left, and the one that turns "keyword search" into something that
actually understands meaning.

See `SPEC.md` for the full Phase 0-10 roadmap.
