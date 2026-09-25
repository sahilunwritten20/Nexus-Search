import unittest

from nexus_search.ingestion.connectors.web import extract_page, parse_html

SAMPLE_HTML = """
<html lang="en">
<head>
  <title>Test Page</title>
  <meta name="description" content="A page for testing extraction.">
</head>
<body>
  <nav>Home | About | Contact</nav>
  <header>Site Header</header>
  <article>
    <h1>Main Heading</h1>
    <p>This is the real content of the page that should be extracted, and
    it should be long enough to clear the extractor's short-content check
    so the main-content branch of the heuristic actually wins.</p>
  </article>
  <a href="/relative-link">Relative</a>
  <a href="https://other.com/page">Absolute</a>
  <a href="javascript:void(0)">JS link</a>
  <a href="#top">Fragment only</a>
  <footer>Copyright 2026</footer>
</body>
</html>
"""

NO_ARTICLE_TAG_HTML = """
<html><head><title>Plain Page</title></head><body>
  <nav>Nav links here</nav>
  <div>Just a plain div with the actual page content in it, no article
  or main tag wrapping it at all.</div>
</body></html>
"""


class TestExtractPage(unittest.TestCase):
    def test_extracts_title(self):
        page = extract_page(SAMPLE_HTML, "https://example.com/test")
        self.assertEqual(page.title, "Test Page")

    def test_extracts_meta_description(self):
        page = extract_page(SAMPLE_HTML, "https://example.com/test")
        self.assertEqual(page.meta_description, "A page for testing extraction.")

    def test_extracts_language(self):
        page = extract_page(SAMPLE_HTML, "https://example.com/test")
        self.assertEqual(page.language, "en")

    def test_main_text_excludes_nav_and_footer(self):
        page = extract_page(SAMPLE_HTML, "https://example.com/test")
        self.assertNotIn("Home | About", page.text)
        self.assertNotIn("Copyright 2026", page.text)

    def test_main_text_includes_article_content(self):
        page = extract_page(SAMPLE_HTML, "https://example.com/test")
        self.assertIn("real content of the page", page.text)

    def test_resolves_relative_links(self):
        page = extract_page(SAMPLE_HTML, "https://example.com/test")
        self.assertIn("https://example.com/relative-link", page.links)

    def test_skips_javascript_and_fragment_links(self):
        page = extract_page(SAMPLE_HTML, "https://example.com/test")
        self.assertFalse(any(link.startswith("javascript:") for link in page.links))
        self.assertNotIn("https://example.com/test#top", page.links)

    def test_falls_back_to_body_without_article_tag(self):
        page = extract_page(NO_ARTICLE_TAG_HTML, "https://example.com/plain")
        self.assertIn("actual page content", page.text)
        self.assertNotIn("Nav links here", page.text)


class TestParseHtml(unittest.TestCase):
    def test_returns_ingest_doc_with_web_type(self):
        doc = parse_html(SAMPLE_HTML, "https://example.com/test")
        self.assertEqual(doc.doc_type, "web")
        self.assertEqual(doc.doc_id, "web:https://example.com/test")

    def test_content_excludes_boilerplate(self):
        doc = parse_html(SAMPLE_HTML, "https://example.com/test")
        self.assertIn("real content", doc.content)
        self.assertNotIn("Copyright 2026", doc.content)

    def test_metadata_includes_url(self):
        doc = parse_html(SAMPLE_HTML, "https://example.com/test")
        self.assertEqual(doc.metadata["url"], "https://example.com/test")


class TestStructuredData(unittest.TestCase):
    """JSON-LD + OpenGraph extraction into metadata ("structured" key)."""

    HTML = """<html><head>
        <title>Test Page</title>
        <meta property="og:title" content="OG Title" />
        <meta property="og:image" content="https://x/img.png" />
        <meta name="author" content="Jane Doe" />
        <script type="application/ld+json">
        {"@type": "Product", "name": "Widget", "offers": {"price": "9.99", "priceCurrency": "USD"},
         "datePublished": "2024-01-01"}
        </script>
        </head><body><article><p>Real body content here, long enough to pass.</p></article></body></html>"""

    def test_json_ld_fields_extracted(self):
        page = parse_html(self.HTML, "https://example.com/p")
        s = page.metadata["structured"]
        self.assertEqual(s["price"], "9.99")
        self.assertEqual(s["currency"], "USD")
        self.assertEqual(s["title"], "Widget")  # name from JSON-LD
        self.assertEqual(s["published"], "2024-01-01")
        self.assertEqual(s["json_ld"][0]["@type"], "Product")

    def test_opengraph_and_meta_author_extracted(self):
        page = parse_html(self.HTML, "https://example.com/p")
        s = page.metadata["structured"]
        self.assertEqual(s["og"]["title"], "OG Title")
        self.assertEqual(s["author"], "Jane Doe")

    def test_no_structured_data_no_key(self):
        page = parse_html("<html><body><p>plain text, long enough to be indexed fifty chars.</p></body></html>",
                          "https://example.com/plain")
        self.assertNotIn("structured", page.metadata)

    def test_malformed_json_ld_skipped(self):
        html = ('<html><head><script type="application/ld+json">{broken json</script>'
                '</head><body><article><p>body content over fifty characters long for sure.</p></article></body></html>')
        page = parse_html(html, "https://example.com/badld")
        self.assertIsInstance(page.metadata.get("structured", {}), dict)
        self.assertNotIn("json_ld", page.metadata.get("structured", {}))

    def test_malformed_json_ld(self):
        html = ('<html><head><script type="application/ld+json">{oops</script></head>'
                '<body><article><p>content text that is definitely long enough.</p></article></body></html>')
        doc = parse_html(html, "https://example.com/x")
        self.assertEqual(doc.metadata.get("structured", {}), {})


if __name__ == "__main__":
    unittest.main()
