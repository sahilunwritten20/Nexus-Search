"""WP14-7 — query_parser.py was 84%-covered; these pin the uncovered
branches: invalid NEXUS_MAX_QUERY_TERMS values, the 64-phrase cap,
filter-valued quoted phrases, unknown field scopes, NOT-phrases,
AND-promoted phrases, phrase-only OR-groups, TermGroup helpers, and the
boolean_match group paths (excluded/required/title phrases, no-groups)."""
import os
import unittest

from nexus_search.core.tokenizer import tokenize
from nexus_search.core.query_parser import (
    Group, ParsedQuery, boolean_match, group_match, parse_query,
)


class TestGroupHelpers(unittest.TestCase):
    def test_positive_terms_includes_phrase_tokens(self):
        g = Group(required=["a"], optional=["b"],
                  required_phrases=["big bad"])
        self.assertEqual(g.positive_terms(), ["a", "b", "big", "bad"])

    def test_any_term_marker(self):
        self.assertEqual(Group.any_term(), "any")


class TestParseCaps(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("NEXUS_MAX_QUERY_TERMS", None)

    def test_invalid_terms_cap_falls_back_to_128(self):
        for bad in ("0", "-5", "not-a-number", ""):
            with self.subTest(bad=bad):
                os.environ["NEXUS_MAX_QUERY_TERMS"] = bad
                parsed = parse_query(" ".join(f"t{i}" for i in range(200)))
                self.assertEqual(len(parsed.terms), 128)
                self.assertTrue(parsed.terms_truncated)

    def test_phrase_cap_truncates_at_64(self):
        query = " ".join(f'"phrase {i}"' for i in range(70))
        parsed = parse_query(query)
        self.assertEqual(len(parsed.phrases), 64)
        self.assertTrue(parsed.phrases_truncated)


class TestParseEdges(unittest.TestCase):
    def test_quoted_filter_value_is_a_filter_not_a_phrase(self):
        parsed = parse_query('type:"pdf"')
        self.assertEqual(parsed.filters, {"doc_type": "pdf"})
        self.assertEqual(parsed.phrases, [])

    def test_negated_quoted_filter_lands_in_not_filters(self):
        # NOT + field-scoped quoted value is the documented negated-filter
        # shape (a leading-dash + quoted value lexes differently and keeps
        # the quote chars in the value — fail-closed under-matching, not a
        # security hole; the operator form is the contract)
        parsed = parse_query('NOT type:"pdf"')
        self.assertEqual(parsed.not_filters, {"doc_type": "pdf"})

    def test_unknown_field_phrase_keeps_the_phrase(self):
        parsed = parse_query('bogusfield:"some phrase"')
        self.assertIn("some phrase", parsed.phrases)

    def test_not_phrase_creates_excluded_phrase(self):
        parsed = parse_query('NOT "forbidden words"')
        g = parsed.groups[0]
        self.assertEqual(g.excluded_phrases, ["forbidden words"])

    def test_and_promotes_phrase_to_required(self):
        parsed = parse_query('alpha AND "exact thing"')
        g = parsed.groups[0]
        self.assertIn("exact thing", g.required_phrases)

    def test_and_across_two_phrases_promotes_left(self):
        parsed = parse_query('"left phrase" AND "right phrase"')
        g = parsed.groups[0]
        self.assertIn("left phrase", g.required_phrases)
        self.assertIn("right phrase", g.required_phrases)

    def test_title_phrase_scopes_to_title(self):
        parsed = parse_query('title:"the title phrase"')
        g = parsed.groups[0]
        self.assertEqual(g.title_phrases, ["the title phrase"])
        self.assertIn("the title phrase", parsed.phrases)


class TestBooleanMatch(unittest.TestCase):
    def test_excluded_phrase_blocks_doc(self):
        parsed = parse_query('alpha NOT "big bad wolf"')
        doc = tokenize("alpha the big bad wolf lives here")
        self.assertFalse(boolean_match(parsed, doc, []))
        doc2 = tokenize("alpha only harmless text")
        self.assertTrue(boolean_match(parsed, doc2, []))

    def test_required_phrase_gates_doc(self):
        parsed = parse_query('alpha AND "big bad"')
        self.assertTrue(boolean_match(parsed, tokenize("alpha big bad"), []))
        self.assertFalse(boolean_match(parsed, tokenize("alpha big"), []))

    def test_title_phrase_gates_title_only(self):
        parsed = parse_query('title:"exact title"')
        doc = tokenize("body words exact title elsewhere")
        # phrase present in BODY but absent from the TITLE: no match
        self.assertFalse(boolean_match(parsed, doc, tokenize("unrelated")))
        # phrase present in the TITLE: match (body placement is optional)
        self.assertTrue(boolean_match(parsed, doc, tokenize("the exact title")))

    def test_optional_phrase_or_semantics(self):
        parsed = parse_query('"alpha beta" OR "gamma delta"')
        self.assertTrue(boolean_match(parsed, tokenize("gamma delta here"), []))
        self.assertTrue(boolean_match(parsed, tokenize("nothing alpha beta"), []))
        self.assertFalse(boolean_match(parsed, tokenize("none of these"), []))

    def test_empty_groups_match_everything(self):
        parsed = ParsedQuery(terms=[], groups=[])
        self.assertTrue(boolean_match(parsed, tokenize("anything"), []))

    def test_group_match_empty_group_is_false(self):
        # a Group with nothing positive and nothing optional cannot match
        self.assertFalse(group_match(tokenize("x"), [], Group()))


if __name__ == "__main__":
    unittest.main()
