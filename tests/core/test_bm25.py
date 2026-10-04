import os
import tempfile
import unittest

from nexus_search.core.bm25 import BM25Search
from nexus_search.core.indexer import Indexer
from nexus_search.core.storage import Storage


class TestBM25(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.storage = Storage(self.path)
        self.indexer = Indexer(self.storage)
        self.search = BM25Search(self.storage)

    def tearDown(self):
        self.storage.close()
        os.remove(self.path)

    def test_empty_corpus_returns_no_results(self):
        self.assertEqual(self.search.search("anything"), [])

    def test_empty_query_returns_no_results(self):
        self.indexer.add_document("d1", "some content")
        self.assertEqual(self.search.search(""), [])

    def test_highlight_not_spoofed_by_mark_literal(self):
        # a doc whose content contains a literal "<mark>" string must not
        # suppress highlighting of the actual match later in the window
        self.indexer.add_document("s", '<mark> decoy </mark> the real alpha match is here')
        results = self.search.search_page("alpha", highlight=True).results
        self.assertIn("<mark>alpha</mark>", results[0].snippet)

    def test_highlight_false_stays_plain_and_byte_identical(self):
        # highlight=False output is PLAIN TEXT and must never change: no
        # escaping, no mark tags — byte-identical to pre-highlight behavior
        self.indexer.add_document("s", 'hello <img src=x onerror=alert(1)> world')
        results = self.search.search_page("hello", highlight=False).results
        self.assertNotIn("<mark>", results[0].snippet)
        self.assertIn("<img src=x onerror=alert(1)>", results[0].snippet)


class TestHighlightEscaping(unittest.TestCase):
    """P1-6: highlight=True output is HTML — content must be escaped per
    segment so stored XSS payloads in document content can't ride along.
    Pre-fix: content 'hello <img src=x onerror=alert(1)> world' returned
    '<mark>hello</mark> <img src=x onerror=alert(1)> world' — payload intact."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.storage = Storage(self.path)
        self.indexer = Indexer(self.storage)
        self.search = BM25Search(self.storage)

    def tearDown(self):
        self.storage.close()
        os.remove(self.path)

    def _snippet_for(self, content, query):
        self.indexer.add_document("d", content)
        results = self.search.search_page(query, highlight=True).results
        return results[0].snippet

    def test_script_payload_escaped_and_match_marked(self):
        import html as _html
        snippet = self._snippet_for("hello <script>alert(1)</script> world", "hello")
        self.assertIn("<mark>hello</mark>", snippet)
        self.assertNotIn("<script>", snippet)
        self.assertIn(_html.escape("<script>"), snippet)

    def test_img_onerror_payload_escaped(self):
        import html as _html
        snippet = self._snippet_for("hello <img src=x onerror=alert(1)> world", "hello")
        self.assertIn("<mark>hello</mark>", snippet)
        self.assertNotIn("<img", snippet)
        self.assertIn(_html.escape("<img"), snippet)

    def test_angle_brackets_quotes_and_ampersand_escaped(self):
        import html as _html
        snippet = self._snippet_for(
            'alpha "quoted" <b>bold</b> and <i>it</i>', "alpha")
        self.assertIn("<mark>alpha</mark>", snippet)
        self.assertNotIn("<b>", snippet)
        self.assertNotIn('"quoted"', snippet)
        self.assertIn(_html.escape('"quoted"'), snippet)
        self.assertIn(_html.escape("<b>bold</b>"), snippet)
        self.assertIn(_html.escape("and"), snippet)

    def test_no_match_highlighted_output_still_escaped(self):
        # filter-only queries carry no highlight targets: the result still
        # exists, and its snippet is HTML output — escape it too
        import html as _html
        self.indexer.add_document("d", "plain <em>text</em> only", doc_type="code")
        results = self.search.search_page("type:code", highlight=True).results
        snippet = results[0].snippet
        self.assertNotIn("<mark>", snippet)
        self.assertNotIn("<em>", snippet)
        self.assertIn(_html.escape("<em>"), snippet)

    def test_term_inside_source_entity_is_matched_on_raw_text(self):
        # the source contains the literal ampersand entity and the query
        # hits the "amp" inside it: matching happens on RAW content (no
        # offset shifting), escaping happens per segment afterwards
        import html as _html
        entity = "&" + "amp;"  # assembled so no tooling rewrites the entity
        snippet = self._snippet_for(f"values: {entity} more alpha here", "amp")
        self.assertIn("<mark>amp</mark>", snippet)
        self.assertIn(_html.escape("&"), snippet)

    def test_mark_literal_decoy_escaped_but_real_match_marked(self):
        # the pinned anti-suppression case, now with escaping: the decoy
        # literal mark-tag text is escaped, the REAL match still gets the tag
        import html as _html
        self.indexer.add_document("s", '<mark> decoy </mark> real alpha match here')
        results = self.search.search_page("alpha", highlight=True).results
        snippet = results[0].snippet
        self.assertIn("<mark>alpha</mark>", snippet)
        self.assertIn(_html.escape("<mark>"), snippet)
        # exactly ONE live (unescaped) mark pair: the real match
        self.assertEqual(snippet.count("<mark>"), 1)
        self.assertEqual(snippet.count("</mark>"), 1)

    def test_cjk_highlight_escaped_consistently(self):
        snippet = self._snippet_for("東京都の説明文です alpha", "東京")
        self.assertIn("<mark>東京</mark>", snippet)

    def test_match_inside_protected_source_mark_not_double_wrapped(self):
        # a query term INSIDE the source's literal mark-tag region is not
        # wrapped (that would nest tags) — behavior preserved, and the
        # region is escaped in the output
        import html as _html
        self.indexer.add_document("s", "<mark>alpha inside decoy</mark> beta tail")
        results = self.search.search_page("alpha", highlight=True).results
        snippet = results[0].snippet
        self.assertEqual(snippet.count("<mark>"), 0,
                         f"no live mark tags expected: {snippet!r}")
        self.assertIn(_html.escape("<mark>"), snippet)


    def test_token_cache_reflects_reindex(self):
        # memoized doc tokens must refresh when the doc is rewritten
        self.indexer.add_document("x", "alpha content here")
        self.assertTrue(self.search.search('"alpha content"'))
        self.indexer.add_document("x", "totally different words")
        self.assertTrue(self.search.search("totally different"))
        self.assertFalse(self.search.search('"alpha content"'))

    def test_finds_matching_document(self):
        self.indexer.add_document("d1", "the quick brown fox jumps")
        self.indexer.add_document("d2", "a completely different sentence")
        results = self.search.search("fox")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].doc_id, "d1")

    def test_no_matching_term_returns_empty(self):
        self.indexer.add_document("d1", "hello world")
        self.assertEqual(self.search.search("nonexistentterm"), [])

    def test_term_frequency_increases_score(self):
        self.indexer.add_document("d1", "python python python programming")
        self.indexer.add_document("d2", "python programming language basics")
        results = {r.doc_id: r.score for r in self.search.search("python")}
        self.assertGreater(results["d1"], results["d2"])

    def test_rare_term_gets_positive_score(self):
        self.indexer.add_document("d1", "python is a language")
        self.indexer.add_document("d2", "python and zygote appear here")
        self.indexer.add_document("d3", "python is popular for scripting")
        results = self.search.search("zygote")
        self.assertEqual(len(results), 1)
        self.assertGreater(results[0].score, 0)

    def test_top_k_limits_results(self):
        for i in range(5):
            self.indexer.add_document(f"d{i}", "shared term appears here")
        results = self.search.search("shared", top_k=2)
        self.assertEqual(len(results), 2)

    def test_results_sorted_descending_by_score(self):
        self.indexer.add_document("d1", "cat cat cat dog")
        self.indexer.add_document("d2", "cat dog dog dog")
        self.indexer.add_document("d3", "cat dog cat dog")
        scores = [r.score for r in self.search.search("cat dog")]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_multi_term_query_scores_both(self):
        self.indexer.add_document("d1", "apple banana")
        self.indexer.add_document("d2", "apple only here")
        results = {r.doc_id: r.score for r in self.search.search("apple banana")}
        self.assertGreater(results["d1"], results["d2"])

    def test_snippet_is_truncated(self):
        self.indexer.add_document("d1", "word " * 100)
        results = self.search.search("word")
        self.assertLessEqual(len(results[0].snippet), 203)  # 200 chars + "..."

    def test_reindexed_document_uses_new_content_for_scoring(self):
        self.indexer.add_document("d1", "original content about cars")
        self.indexer.add_document("d1", "updated content about bicycles")
        self.assertEqual(self.search.search("cars"), [])
        self.assertEqual(len(self.search.search("bicycles")), 1)


if __name__ == "__main__":
    unittest.main()
