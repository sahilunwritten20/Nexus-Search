# Nexus Search — Phase 1-3 Upgrade

## Upgrade completed

This release strengthens the existing Phase 1-3 foundation without replacing the working architecture.

### Search core
- Added a dependency-free query parser.
- Added quoted phrase recognition.
- Added `type:` / `doc_type:` and `lang:` / `language:` filters.
- Added title relevance boosting.
- Added phrase relevance boosting.
- Improved snippets to start near a matched term or phrase.
- Added safe `top_k` bounds at the API boundary.

### Web ingestion
- Added canonical URL extraction.
- Preserved canonical URL in document metadata.

### Crawler
- Added sitemap XML parsing helper.
- Crawled-page metadata now includes URL and canonical URL.

## Verification

`python -m unittest discover -s tests -v` → **113 tests, 113 passed, 0 failed**.

FastAPI remains environment-dependent; the API source is syntax-valid, while the underlying index/search/storage behavior is covered by tests.
