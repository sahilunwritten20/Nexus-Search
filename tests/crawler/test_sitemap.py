import unittest
from nexus_search.crawler.sitemap import parse_sitemap


class TestSitemap(unittest.TestCase):
    def test_parse_sitemap(self):
        xml = '''<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <url><loc>https://example.com/a</loc></url>
        <url><loc>https://example.com/a</loc></url>
        <url><loc>https://example.com/b</loc></url></urlset>'''
        self.assertEqual(parse_sitemap(xml), ['https://example.com/a', 'https://example.com/b'])

    def test_invalid_xml_returns_empty(self):
        self.assertEqual(parse_sitemap('<broken>'), [])


if __name__ == '__main__':
    unittest.main()
