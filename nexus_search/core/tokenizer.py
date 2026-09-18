"""Tokenization for Nexus Search's BM25 index."""
import re

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:'[A-Za-z]+)?")


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens. Keeps internal apostrophes
    (don't -> "don't", not "don" + "t") and drops all other punctuation.
    """
    return [t.lower() for t in _TOKEN_RE.findall(text)]
