"""Tokenization for Nexus Search's BM25 index (Unicode-aware).

Includes a conservative, rule-based English stemmer ("running"→"run",
"stories"→"story"). It is ON by default for English-looking tokens, because
indexing and queries share this one tokenizer — both sides stem identically,
so quoted-phrase matching is unaffected by stemming; stemming only widens
recall to morphological variants. It is NOT a full Porter stemmer and does
not pretend to be one (documented in `_stem_en`). Disable globally with
NEXUS_STEMMING=0 or per call with tokenize(text, stem=False).

Stopword REMOVAL is deliberately OFF by default (NEXUS_STOPWORDS=1 to enable
for latency-sensitive experiments): dropping "the"/"of"/etc. silently changes
phrase semantics, and "Honesty over completeness theater" says don't."""

import os
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


# ---------------------------------------------------------------------------
# Stemming — rule-based English suffix stripper (NOT full Porter:
# honest scope). One entry per language; unknown languages stem to identity.
# ---------------------------------------------------------------------------

_EN_STEM_RULES = (
    ("ational", "ate"), ("tional", "tion"), ("ities", "ity"), ("ies", "y"),
    ("ing", ""), ("edly", ""), ("ed", ""), ("ly", ""), ("es", ""), ("s", ""),
)


def _stem_en(token: str) -> str:
    """Conservative English stem. Guardrails (per its rules):
    - never shorten below 3 chars (prevents "us"→"u" garbage)
    - leave it alone if stripping would collide with a different morpheme
      pattern the rule knows nothing about (vowel checks)
    """
    if len(token) < 4 or not token.isalpha() or not token.isascii():
        return token
    for suffix, repl in _EN_STEM_RULES:
        if token.endswith(suffix) and len(token) - len(suffix) + len(repl) >= 3:
            stem = token[: len(token) - len(suffix)] + repl
            if not stem:
                return token
            if suffix == "s" and stem.endswith(("s", "u", "i")):
                return token  # "class"→"clas" is a false stem, not morphology
            # double-consonant resolution for inflected forms:
            # "running" → "run" (not "runn"), "stopped" → "stop"
            if suffix in ("ing", "ed", "edly") and len(stem) >= 4 and stem[-1] == stem[-2] and stem[-1] not in "aeiou":
                stem = stem[:-1]
            # basic pronounceability guard: a result with no vowel isn't a stem
            if any(c in "aeiouy" for c in stem):
                return stem
    return token


_TECHNIQUE = {"en": _stem_en}


def stem_token(token: str, language: str = "en") -> str:
    """Pluggable per-language stemming. Unknown language -> token unchanged."""
    fn = _TECHNIQUE.get(language)
    return fn(token) if fn else token


def tokenize(text: str, stem: "bool | None" = None) -> list[str]:
    """Lowercase word tokens in any script. Keeps internal apostrophes
    (don't -> "don't") and drops all other punctuation. CJK runs become
    character bigrams; a mixed token like "iPhone15发布" is split by script
    first, so its Latin part stays a normal word ("iphone15", "发布").

    `stem=None` follows the global default (NEXUS_STEMMING, on by default);
    pass True/False to force. Non-English-Latin tokens are never stemmed."""
    if stem is None:
        stem = os.environ.get("NEXUS_STEMMING", "1") != "0"
    text = unicodedata.normalize("NFKC", text).casefold()
    tokens: list[str] = []
    token_re = _get_token_re()
    for token in token_re.findall(text):
        if not _HAS_CJK.search(token):  # fast path: nearly all tokens
            tokens.append(stem_token(token) if stem else token)
            continue
        for run in _SCRIPT_RUN.findall(token):
            tokens.extend(_cjk_bigrams(run) if _HAS_CJK.match(run) else [run])
    return tokens