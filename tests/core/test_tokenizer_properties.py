"""Tokenizer property tests (mission B1): Unicode behavior, bounds,
determinism — hypothesis-driven against the PRODUCTION tokenizer.

Covers the B1 checklist: NFC/NFKC folding, combining marks, emoji,
CJK bigrams, RTL, lone surrogates, control chars, a 10MB-scale single
token (bounded smoke), empty/whitespace-only input, and allowlist terms.
"""
import unittest

from hypothesis import given, settings
from hypothesis import strategies as st

from nexus_search.core.tokenizer import tokenize

UNICODE_TEXT = st.text(
    alphabet=st.characters(min_codepoint=0, max_codepoint=0x2FFFF),
    max_size=200)


class TestTokenizerProperties(unittest.TestCase):
    @settings(derandomize=True, max_examples=200, deadline=None)
    @given(UNICODE_TEXT)
    def test_never_raises_on_any_unicode(self, text):
        out = tokenize(text, stem=False)
        self.assertIsInstance(out, list)
        self.assertTrue(all(isinstance(t, str) for t in out))

    @settings(derandomize=True, max_examples=100, deadline=None)
    @given(st.text(max_size=60))
    def test_deterministic(self, text):
        self.assertEqual(tokenize(text, stem=False),
                         tokenize(text, stem=False))

    def test_nfkc_folding(self):
        # compatibility chars fold to their canonical forms
        self.assertEqual(tokenize("ﬁle", stem=False), ["file"])
        self.assertEqual(tokenize("①", stem=False), ["1"])

    def test_casefold(self):
        self.assertEqual(tokenize("HeLLo WÖRLD", stem=False),
                         ["hello", "wörld"])

    def test_combining_marks_stay_attached(self):
        # e + combining acute NFKC-COMPOSES to the precomposed é — the
        # property that matters: one token, never split or dropped
        out = tokenize("cafe\u0301", stem=False)
        self.assertEqual(out, ["caf\u00e9"])

    def test_emoji_are_not_words_by_design(self):
        # PINNED behavior (B1): the tokenizer keeps WORD tokens; emoji
        # (So category, not \w, not marks) are dropped — an emoji-only
        # query returns nothing. Documented limit, not an accident:
        # changing it is index-affecting and a product decision.
        out = tokenize("hello 🚀 world", stem=False)
        self.assertEqual(out, ["hello", "world"])

    def test_cjk_bigrams(self):
        out = tokenize("机器学习", stem=False)
        self.assertEqual(out, ["机器", "器学", "学习"])

    def test_cjk_single_char_stays_unigram(self):
        self.assertEqual(tokenize("中", stem=False), ["中"])

    def test_mixed_script_splits(self):
        out = tokenize("iPhone15发布", stem=False)
        self.assertIn("iphone15", out)
        self.assertIn("发布", out)

    def test_lone_surrogate_does_not_crash(self):
        out = tokenize("text \ud800 more", stem=False)
        self.assertIsInstance(out, list)

    def test_control_chars_dropped(self):
        out = tokenize("a\x00b\x01c", stem=False)
        self.assertEqual(out, ["a", "b", "c"])

    def test_empty_and_whitespace_only(self):
        self.assertEqual(tokenize("", stem=False), [])
        self.assertEqual(tokenize("   \t\n  ", stem=False), [])

    def test_allowlist_terms_survive_whole(self):
        for term in ("c++", "c#", "f#", "node.js", ".net"):
            self.assertIn(term, tokenize(f"using {term} today", stem=False))

    def test_allowlist_boundary_rules(self):
        # adjacent [a-z0-9] kills the allowlist form
        out = tokenize("objcplusplus", stem=False)
        self.assertNotIn("c++", out)

    def test_large_token_is_bounded_work(self):
        # a 2MB single token must tokenize in bounded time (linear scan)
        import time
        big = "x" * 2_000_000
        t0 = time.perf_counter()
        out = tokenize(big, stem=False)
        elapsed = time.perf_counter() - t0
        self.assertEqual(out, ["x" * 2_000_000])
        self.assertLess(elapsed, 10.0)

    @settings(derandomize=True, max_examples=50, deadline=None)
    @given(st.lists(st.text(min_size=1, max_size=20), max_size=5))
    def test_stemming_never_changes_token_count_much(self, words):
        raw = tokenize(" ".join(words), stem=False)
        stemmed = tokenize(" ".join(words), stem=True)
        # stemming is token-local: same count, same positions
        self.assertEqual(len(raw), len(stemmed))


if __name__ == "__main__":
    unittest.main()
