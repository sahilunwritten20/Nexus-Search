import unittest
from nexus_search.ingestion.connectors.web import extract_page, parse_html


class TestWebMetadataUpgrade(unittest.TestCase):
    def test_canonical_url_is_extracted(self):
        html = '''<html><head><title>Test</title><link rel="canonical" href="/canonical"></head>
        <body><article>Long enough content for the extractor to return article text reliably.</article></body></html>'''
        page = extract_page(html, 'https://example.com/page')
        self.assertEqual(page.canonical_url, 'https://example.com/canonical')
        doc = parse_html(html, 'https://example.com/page')
        self.assertEqual(doc.metadata['canonical_url'], 'https://example.com/canonical')


if __name__ == '__main__':
    unittest.main()
