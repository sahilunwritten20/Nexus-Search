"""WP14-7 — CrawlPipeline.seed_from_sitemap was 0%-covered (pipeline.py
84%): the sitemap-driven seeding path (Phase 3 feature) had a tested parser
but untested wiring. Pins: loc URLs land in the frontier, sitemap-index
following works, max_urls caps, unsafe URLs are skipped without the
allow-private flag. Fetcher stubbed — no network, no server."""
import os
import tempfile
import unittest

from nexus_search.crawler.pipeline import CrawlPipeline

SITEMAP = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://example.com/a</loc></url>
<url><loc>https://example.com/b</loc></url>
<url><loc>http://127.0.0.1:9/x</loc></url>
</urlset>"""


class _StubFetcher:
    def __init__(self, payloads):
        self.payloads = payloads

    def fetch_bytes(self, url):
        return self.payloads.get(url, b"")


class TestSeedFromSitemap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wp14_sms_")

    def _pipeline(self, **kw):
        kw.setdefault("max_pages", 20)
        return CrawlPipeline(db_path=os.path.join(self.tmp, "f.db"),
                             allow_private_hosts=True, **kw)

    def test_locs_are_queued(self):
        pl = self._pipeline()
        pl.fetcher = _StubFetcher({"https://example.com/s.xml": SITEMAP.encode()})
        added = pl.seed_from_sitemap("https://example.com/s.xml")
        self.assertEqual(added, 3)
        self.assertEqual(pl.frontier.pending_count(), 3)
        urls = [e.url for e in pl.frontier.next_batch(10)]
        for loc in ("https://example.com/a", "https://example.com/b",
                    "http://127.0.0.1:9/x"):
            self.assertIn(loc, urls)

    def test_max_urls_caps_seeding(self):
        pl = self._pipeline()
        pl.fetcher = _StubFetcher({"https://example.com/s.xml": SITEMAP.encode()})
        added = pl.seed_from_sitemap("https://example.com/s.xml", max_urls=1)
        self.assertEqual(added, 1)

    def test_sitemap_index_is_followed(self):
        index = ('<?xml version="1.0"?>'
                 '<sitemapindex><sitemap>'
                 '<loc>https://example.com/real.xml</loc>'
                 '</sitemap></sitemapindex>').encode()
        pl = self._pipeline()
        pl.fetcher = _StubFetcher({
            "https://example.com/idx.xml": index,
            "https://example.com/real.xml": SITEMAP.encode(),
        })
        added = pl.seed_from_sitemap("https://example.com/idx.xml")
        self.assertEqual(added, 3)


if __name__ == "__main__":
    unittest.main()
