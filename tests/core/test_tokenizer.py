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


if __name__ == "__main__":
    unittest.main()