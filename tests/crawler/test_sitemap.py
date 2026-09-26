import gzip
import unittest
from nexus_search.crawler.sitemap import maybe_gzip, parse_sitemap, parse_sitemap_extended


class TestMaybeGzip(unittest.TestCase):
    XML = b'<?xml version="1.0"?><urlset><url><loc>https://a.com/x</loc></url></urlset>'

    def test_plain_passthrough(self):
        self.assertEqual(maybe_gzip(self.XML), self.XML.decode())

    def test_gzip_magic_detected(self):
        self.assertEqual(maybe_gzip(gzip.compress(self.XML)), self.XML.decode())

    def test_gzip_content_type(self):
        self.assertEqual(maybe_gzip(gzip.compress(self.XML), "application/gzip"),
                         self.XML.decode())

    def test_corrupt_gzip_returns_empty(self):
        self.assertEqual(maybe_gzip(b"\x1f\x8b garbage not gzip"), "")

    def test_empty(self):
        self.assertEqual(maybe_gzip(b""), "")


class TestSitemap(unittest.TestCase):
    def test_parse_sitemap(self):
        xml = '''<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <url><loc>https://example.com/a</loc></url>
        <url><loc>https://example.com/a</loc></url>
        <url><loc>https://example.com/b</loc></url></urlset>'''
        self.assertEqual(parse_sitemap(xml), ['https://example.com/a', 'https://example.com/b'])

    def test_invalid_xml_returns_empty(self):
        self.assertEqual(parse_sitemap('<broken>'), [])


class TestSitemapExtensions(unittest.TestCase):
    """Build-out #21: news:news and video:video extension parsing."""

    NEWS_XML = """<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
            xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
      <url>
        <loc>https://news.example/story1</loc>
        <news:news>
          <news:publication><news:name>Example News</news:name></news:publication>
          <news:title>Big Story</news:title>
          <news:publication_date>2025-01-15</news:publication_date>
        </news:news>
      </url>
    </urlset>"""

    VIDEO_XML = """<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
            xmlns:video="http://www.google.com/schemas/sitemap-video/1.1">
      <url>
        <loc>https://video.example/watch/1</loc>
        <video:video>
          <video:title>Demo Video</video:title>
          <video:thumbnail_loc>https://video.example/t1.jpg</video:thumbnail_loc>
          <video:duration>120</video:duration>
        </video:video>
      </url>
    </urlset>"""

    def test_plain_sitemap_still_works(self):
        out = parse_sitemap_extended(
            "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
            "<url><loc>https://x.com/a</loc></url></urlset>")
        self.assertEqual(out, [{"loc": "https://x.com/a"}])

    def test_news_extension_extracted(self):
        out = parse_sitemap_extended(self.NEWS_XML)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["loc"], "https://news.example/story1")
        self.assertEqual(out[0]["news"]["title"], "Big Story")
        self.assertEqual(out[0]["news"]["publication_date"], "2025-01-15")
        self.assertEqual(out[0]["news"]["publication"], "Example News")

    def test_video_extension_extracted(self):
        out = parse_sitemap_extended(self.VIDEO_XML)
        self.assertEqual(out[0]["video"][0]["title"], "Demo Video")
        self.assertEqual(out[0]["video"][0]["duration"], "120")

    def test_malformed_extension_skipped_not_fatal(self):
        # valid XML, extension namespace declared, but empty news block:
        # must skip cleanly and NOT raise
        out = parse_sitemap_extended(
            "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9' "
            "xmlns:news='http://www.google.com/schemas/sitemap-news/0.9'>"
            "<url><loc>https://x.com/a</loc><news:news/></url></urlset>")
        self.assertEqual(out[0]["loc"], "https://x.com/a")
        self.assertNotIn("news", out[0])

    def test_completely_broken_xml_returns_empty(self):
        self.assertEqual(parse_sitemap_extended("<urlset><unclosed>"), [])

    def test_extended_still_dedupes(self):
        xml = ("<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
               "<url><loc>https://x.com/a</loc></url>"
               "<url><loc>https://x.com/a</loc></url></urlset>")
        out = parse_sitemap_extended(xml)
        self.assertEqual(len(out), 1)


if __name__ == '__main__':
    unittest.main()
