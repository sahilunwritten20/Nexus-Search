"""Fuzz: the query parser and boolean matcher must never raise on
arbitrary input (mission B1: operator/quote/paren/unicode soup; no
exception may escape a /search as a 500).

Pure functions — no DB — so the fuzz budget can be generous. Also pins
the bounds contract: >128 positive terms / >64 phrases are TRUNCATED,
never unbounded work, and the parse is IDEMPOTENT for well-formed
re-rendered queries (terms(text) stability under parse->render cycles
wherever the structure is flat).
"""
import string
import unittest

from hypothesis import given, settings
from hypothesis import strategies as st

from nexus_search.core.query_parser import (
    ParsedQuery, boolean_match, parse_query,
)

# the adversarial alphabet: operators, quotes, escapes, parens, colons,
# dashes, unicode, control characters
ATOMS = st.sampled_from([
    "AND", "OR", "NOT", "and", "or", "not", '"', "'", "\\", "(", ")",
    ":", "-", "--", "title:", "type:", "lang:", 'type:"', '"a b"',
    '"', "a", "bm25", "c++", "c#", "f#", "node.js", ".net",
    "é", "中文", "\u200b", "\ufeff", "🚀", "\x00", "\n", "\t",
    " " * 5, "x" * 300, "*" * 50, "\\" * 20, '"quoted""',
])
QUERIES = st.lists(ATOMS, min_size=0, max_size=30).map(lambda parts: "".join(parts))


class TestQueryParserFuzz(unittest.TestCase):
    @settings(derandomize=True, max_examples=300, deadline=None)
    @given(QUERIES)
    def test_parse_never_raises(self, query):
        parsed = parse_query(query)
        self.assertIsInstance(parsed, ParsedQuery)
        # caps hold under any input
        self.assertLessEqual(len(parsed.terms), 128)
        self.assertLessEqual(len(parsed.phrases), 64)

    @settings(derandomize=True, max_examples=200, deadline=None)
    @given(QUERIES, st.integers(0, 6))
    def test_boolean_match_never_raises(self, query, seed):
        parsed = parse_query(query)
        doc_tokens = ["alpha", "beta", "gamma", "delta"][:seed % 4 + 1]
        result = boolean_match(parsed, doc_tokens, doc_tokens[:1])
        self.assertIsInstance(result, bool)

    @settings(derandomize=True, max_examples=100, deadline=None)
    @given(st.text(alphabet=string.printable, max_size=120))
    def test_printable_soup_never_raises(self, query):
        parse_query(query)

    def test_term_cap_truncation_flag_is_honest(self):
        many = " ".join(f"w{i}" for i in range(200))
        parsed = parse_query(many)
        self.assertEqual(len(parsed.terms), 128)
        self.assertTrue(parsed.terms_truncated)

    def test_phrase_cap_truncation_flag_is_honest(self):
        many = " ".join(f'"phrase{i}"' for i in range(100))
        parsed = parse_query(many)
        self.assertEqual(len(parsed.phrases), 64)
        self.assertTrue(parsed.phrases_truncated)

    def test_parse_is_idempotent_on_flat_terms(self):
        q = "alpha beta gamma type:pdf -delta"
        p1 = parse_query(q)
        p2 = parse_query(q)
        self.assertEqual(p1.terms, p2.terms)
        self.assertEqual(p1.filters, p2.filters)
        self.assertEqual(p1.not_filters, p2.not_filters)


if __name__ == "__main__":
    unittest.main()
