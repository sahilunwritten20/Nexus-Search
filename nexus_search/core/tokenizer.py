"""Tokenization for Nexus Search's BM25 index (Unicode-aware)."""

import re
import unicodedata
from functools import lru_cache

# CJK scripts have no spaces between words. Without a real segmenter a whole
# sentence would become ONE token, so runs of these characters are split into
# overlapping character bigrams (the standard space-free fallback):
# Hiragana/Katakana, CJK Ext-A, CJK Unified Ideographs, Compat Ideographs, Hangul.
_CJK = "\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7a3"
_HAS_CJK = re.compile(f"[{_CJK}]")
_SCRIPT_RUN = re.compile(f"[{_CJK}]+|[^{_CJK}]+")


@lru_cache(maxsize=1)
def _get_token_re() -> re.Pattern:
    """Lazily build the token regex with Unicode combining marks.
    Avoids 50-100ms import-time penalty from iterating all 65K codepoints.
    """
    marks = "".join(
        chr(c) for c in range(0x10000) if unicodedata.category(chr(c)) in ("Mn", "Mc")
    )
    w = rf"(?:[^\W_]|[{re.escape(marks)}])"
    return re.compile(rf"{w}+(?:'{w}+)?")


def _cjk_bigrams(run: str) -> list[str]:
    """Split a CJK run into overlapping bigrams. A lone character stays a unigram."""
    if len(run) == 1:
        return [run]
    return [run[i : i + 2] for i in range(len(run) - 1)]


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens in any script. Keeps internal apostrophes
    (don't -> "don't") and drops all other punctuation. CJK runs become
    character bigrams; a mixed token like "iPhone15发布" is split by script
    first, so its Latin part stays a normal word ("iphone15", "发布").
    """
    text = unicodedata.normalize("NFKC", text).casefold()
    tokens: list[str] = []
    token_re = _get_token_re()
    for token in token_re.findall(text):
        if not _HAS_CJK.search(token):  # fast path: nearly all tokens
            tokens.append(token)
            continue
        for run in _SCRIPT_RUN.findall(token):
            tokens.extend(_cjk_bigrams(run) if _HAS_CJK.match(run) else [run])
    return tokens