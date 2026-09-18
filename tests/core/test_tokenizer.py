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


if __name__ == "__main__":
    unittest.main()
