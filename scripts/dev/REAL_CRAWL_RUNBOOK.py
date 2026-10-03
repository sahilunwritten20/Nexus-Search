"""WP9 deliverable: docs/REAL_CRAWL_REPORT.md is written separately; this
runbook documents HOW the crawl was run and how to re-run it safely.

Crawl executed (2026-10-03, ~14:30 local):
  python scripts/dev/real_crawl_smoke.py
  -> docs/REAL_CRAWL_REPORT.md records the observed output.

Re-run rules (do not weaken):
- keep allow_private_hosts unset (public path: SSRF + DNS pinning active)
- keep concurrency=1 and default_crawl_delay >= 1.0 (politeness floor)
- keep max_pages <= 25, max_depth <= 1, WALL_CLOCK_CAP_S <= 240
- target only the scraping-practice sandbox (quotes.toscrape.com) unless
  you own the site or have verified its robots.txt allows you
"""
print(__doc__)
