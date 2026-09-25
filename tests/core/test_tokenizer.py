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