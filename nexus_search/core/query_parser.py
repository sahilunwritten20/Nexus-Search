"""Small, dependency-free query parser for the Nexus Search core.

Supported syntax:
- quoted phrases: "machine learning"
- field filters: type:pdf, lang:en
- ordinary terms
"""
from dataclasses import dataclass, field
import re

from .tokenizer import tokenize
_TOKEN_RE = re.compile(r"\"([^\"]+)\"|([^\s]+)")


@dataclass
class ParsedQuery:
    terms: list[str] = field(default_factory=list)
    phrases: list[str] = field(default_factory=list)
    filters: dict[str, str] = field(default_factory=dict)


def parse_query(query: str) -> ParsedQuery:
    parsed = ParsedQuery()
    for match in _TOKEN_RE.finditer(query.strip()):
        phrase, token = match.groups()
        value = (phrase or token or "").strip()
        if not value:
            continue
        if phrase is not None:
            parsed.phrases.append(phrase.lower())
            continue
        if ":" in value:
            key, filter_value = value.split(":", 1)
            key = key.lower().strip()
            filter_value = filter_value.strip()
            if key in {"type", "doc_type", "lang", "language"} and filter_value:
                parsed.filters["doc_type" if key in {"type", "doc_type"} else "language"] = filter_value.lower()
                continue
        parsed.terms.extend(tokenize(value))
    return parsed
