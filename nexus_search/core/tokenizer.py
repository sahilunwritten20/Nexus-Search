"""Tokenization for Nexus Search's BM25 index (Unicode-aware)."""
import re
import unicodedata

# \w minus underscore, PLUS combining marks (Devanagari matras, accents, Thai...)
# which \w drops and which would otherwise chop words apart.
_MARKS = "".join(
    chr(c) for c in range(0x10000) if unicodedata.category(chr(c)) in ("Mn", "Mc")
)
_W = rf"(?:[^\W_]|[{re.escape(_MARKS)}])"
_TOKEN_RE = re.compile(rf"{_W}+(?:'{_W}+)?")

# CJK ranges that have no spaces between words: Han ideographs (Chinese,
# and the kanji shared by Japanese/Korean text), Hiragana, Katakana, and
# Hangul syllables. Without a real segmenter, a run of these characters
# would otherwise become one giant token (a whole sentence), so partial
# queries could never match. Character bigrams are the standard
# space-free fallback: they let a 2+ character CJK query substring match
# without needing real word segmentation.
_CJK_RANGES = (
    (0x3040, 0x30FF),  # Hiragana, Katakana
    (0x3400, 0x4DBF),  # CJK Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
    (0xAC00, 0xD7A3),  # Hangul syllables
)


def _is_cjk_char(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def _cjk_bigrams(token: str) -> list[str]:
    """Split a run of CJK characters into overlapping character bigrams
    (e.g. "東京都" -> ["東京", "京都"]). A lone leftover character (either
    because the whole token is one character, or a trailing character with
    no following pair) is kept as a unigram so it's still searchable.
    """
    if len(token) == 1:
        return [token]
    return [token[i:i + 2] for i in range(len(token) - 1)]


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens in any script. Keeps internal apostrophes
    (don't -> "don't") and drops all other punctuation. CJK runs (which
    have no spaces to mark word boundaries) are further split into
    character bigrams so partial CJK queries can match.
    """
    text = unicodedata.normalize("NFKC", text).casefold()
    tokens = []
    for token in _TOKEN_RE.findall(text):
        if any(_is_cjk_char(ch) for ch in token):
            tokens.extend(_cjk_bigrams(token))
        else:
            tokens.append(token)
    return tokens