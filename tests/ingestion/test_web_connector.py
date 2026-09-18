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


if __name__ == "__main__":
    unittest.main()
