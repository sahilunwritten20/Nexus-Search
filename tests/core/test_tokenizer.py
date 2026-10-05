import os
import unittest

from nexus_search.core.tokenizer import tokenize


class TestTokenizer(unittest.TestCase):
    def test_lowercases(self):
        self.assertEqual(tokenize("Hello WORLD"), ["hello", "world"])

    def test_strips_punctuation(self):
        self.assertEqual(tokenize("Hello, world!"), ["hello", "world"])

    def test_keeps_internal_apostrophe(self):
        self.assertEqual(tokenize("don't stop"), ["don't", "stop"])

    def test_handles_alphanumeric(self):
        self.assertEqual(tokenize("BM25 is great"), ["bm25", "is", "great"])

    def test_empty_string(self):
        self.assertEqual(tokenize(""), [])

    def test_only_punctuation(self):
        self.assertEqual(tokenize("!!! ??? ..."), [])


class TestTokenizerScripts(unittest.TestCase):
    def test_devanagari_words_stay_whole(self):
        self.assertEqual(tokenize("नमस्ते दुनिया"), ["नमस्ते", "दुनिया"])

    def test_cjk_becomes_bigrams(self):
        self.assertEqual(tokenize("東京都"), ["東京", "京都"])

    def test_mixed_script_token_keeps_latin_intact(self):
        self.assertEqual(
            tokenize("iPhone15发布 review"), ["iphone15", "发布", "review"]
        )
        self.assertEqual(tokenize("Python言語入門")[0], "python")


class TestStemming(unittest.TestCase):
    """Pluggable English stemming (default ON; NEXUS_STEMMING=0 disables).
    Stemmer is conservative rule-based — not full Porter (see tokenizer.py)."""

    def test_default_stems_english_morphology(self):
        self.assertEqual(tokenize("running"), ["run"])
        self.assertEqual(tokenize("runs"), ["run"])
        self.assertEqual(tokenize("stories"), ["story"])

    def test_short_and_nonalpha_tokens_untouched(self):
        self.assertEqual(tokenize("us is go"), ["us", "is", "go"])
        self.assertEqual(tokenize("abc123"), ["abc123"])
        self.assertEqual(tokenize("class"), ["class"])  # false-stem guard


class TestTechAllowlist(unittest.TestCase):
    """P3-10: code-search terms survive as single tokens. Index-affecting:
    deployed DBs must run `python -m nexus_search.core.reindex --shadow`
    after upgrading (see README)."""

    def test_code_terms_stay_whole(self):
        self.assertEqual(tokenize("C++ vs C# node.js"),
                         ["c++", "vs", "c#", "node.js"])

    def test_each_allowlisted_term(self):
        self.assertEqual(tokenize("f#"), ["f#"])
        self.assertEqual(tokenize(".net"), [".net"])
        self.assertEqual(tokenize("Node.JS"), ["node.js"])  # casefolded

    def test_allowlist_never_stems(self):
        self.assertEqual(tokenize("node.js frameworks"), ["node.js", "framework"])

    def test_word_boundaries_respected(self):
        # an identifier that merely CONTAINS the term is not the term
        self.assertNotIn("c++", tokenize("objcplusplus"))       # alnum before
        self.assertNotIn("node.js", tokenize("node.json"))      # alnum after
        self.assertNotIn(".net", tokenize("a.net"))              # alnum before

    def test_surrounding_text_untouched(self):
        self.assertEqual(tokenize("c++ is great"), ["c++", "is", "great"])
        self.assertEqual(tokenize("learning c# today"), ["learn", "c#", "today"])

    def test_cjk_still_bigrams_alongside_allowlist(self):
        self.assertEqual(tokenize("東京 node.js"), ["東京", "node.js"])

    def test_plain_text_fast_path_unchanged(self):
        # no allowlist term -> exactly the pre-P3-10 output
        self.assertEqual(tokenize("running stories"), ["run", "story"])
        self.assertEqual(tokenize("Hello, world!"), ["hello", "world"])

    def test_cjk_never_stemmed(self):
        self.assertEqual(tokenize("发布"), ["发布"])

    def test_explicit_off(self):
        self.assertEqual(tokenize("running", stem=False), ["running"])

    def test_env_flag_disables(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {"NEXUS_STEMMING": "0"}):
            self.assertEqual(tokenize("running"), ["running"])

    def test_query_and_index_share_stems(self):
        idx_tokens = tokenize("the engine is running")
        self.assertIn("run", idx_tokens)
        self.assertIn("run", tokenize("run"))


if __name__ == "__main__":
    unittest.main()