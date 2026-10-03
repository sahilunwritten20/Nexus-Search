# Real Crawl Report (WP9)

**Run:** 2026-10-03, ~14:30 local, from this repo on the audit machine
(Windows 11, Python 3.14.7, .venv). Command:
`python scripts/dev/real_crawl_smoke.py` (script committed with the run).

## Configuration

| Setting | Value |
|---|---|
| Target | `https://quotes.toscrape.com/` (public scraping-practice sandbox; the repo's own seeds.txt default) |
| Path | PRODUCTION crawl path — SSRF validation + DNS pinning + placeholder-UA refusal all ACTIVE (`allow_private_hosts` unset) |
| UA | `NexusSearchBot/0.1 (+nexus-search audit validation crawl)` — honest identity, no fabricated contact (sandbox target) |
| Robots | fetched per-domain; fail-closed if unreachable |
| Politeness | concurrency=1, crawl-delay 1.0s (floor honored: 60.8s for 25 pages) |
| Limits | max_pages=25, max_depth=1, wall-clock watchdog 240s |
| Dedup/chunking | production pipeline (content-hash dedup, 1000-char chunks, failure queue, history) |

## What happened

1. **A real bug was found and fixed during WP9** (recorded as BUG-14):
   the DNS-pinning resolver dropped `type=0` entries, which is exactly what
   Windows `getaddrinfo(host, service=None)` returns — every real external
   fetch through the pinned resolver failed with "no pinned address", the
   robots fetch failed-closed, and the crawl refused to start (correct
   failure behavior, wrong cause). The unit tests had hand-built
   `SOCK_STREAM` pins and never caught the Windows shape. Fixed in
   `crawler/security.py` (type-0 pins serve any requested type);
   regression test `test_type_zero_pinned_entry_serves_any_requested_type`
   pins the Windows shape forever.
2. After the fix, the crawl completed cleanly:
   - **25/25 pages crawled, 0 errors, 0 skipped, 137,541 bytes, 60.8s**
     (0.41 pages/s — politeness-delayed by design)
   - 44 documents indexed (long quote pages chunked at 1000 chars)
   - robots.txt: HTTP 404 → treated as absent → crawling allowed (fail-open
     per convention; the fail-closed path was proven live by the BUG-14 run)
   - ETag/Last-Modified support present on the sandbox; a re-crawl uses
     conditional GETs (validated by the local live-server e2e suite)

## Link intelligence on real URLs (the BUG-02 validation the audit wanted)

- **512 edges** recorded from real pages; normalization collapsed fragment
  variants (`/tag/love#...`, trailing slashes) into single nodes.
- `compute_authority`: **119 pages scored**, 512/512 endorsing edges,
  converged in 7 iterations, 0.09s.
- Top-authority URLs are exactly right for this site's structure:
  `www.goodreads.com/quotes` and `www.zyte.com/` (footer links on EVERY
  page → highest in-link mass), then `/login`, `/tag/books`, `/tag/friends`
  (nav/sidebar links on most pages).
- Reports: **0 dead links, 94 linked-but-unfetched targets** (the depth-1
  cap prevented fetching them — correct behavior, honestly reported),
  **0 orphans**.

## Search over the crawled corpus

keyword vs hybrid totals for sample queries ("love" 20 vs 25, "life" 24
vs 25, "books" 22 vs 25) — hybrid adds the semantically-adjacent pages the
keyword side misses. Titles resolve to "Quotes to Scrape" (the sandbox's
page title; content differentiates pages).

## Honesty notes

- The target is a static sandbox built FOR scraping practice — a hostile
  environment this is not. Redirect-following across real hosts, TLS to
  arbitrary servers, and robots wildcards remain validated only by the
  local-server suite + unit tests.
- `linked-but-unfetched: 94` is a depth-cap artifact, not a defect.
- No live 304 revalidation was re-run against the sandbox (its ETags were
  observed; conditional-GET behavior is pinned by the e2e suite).

Re-run rules live in `scripts/dev/REAL_CRAWL_RUNBOOK.py` (do not weaken
the safety settings).
