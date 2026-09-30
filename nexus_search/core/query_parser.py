"""Small, dependency-free query parser for the Nexus Search core.

Supported syntax:
- quoted phrases: "machine learning"  (with \" escaping inside)
- field filters: type:pdf, lang:en
- field-scoped terms: title:foo, title:"foo bar"
- boolean operators: AND, OR, NOT (explicit, uppercase) and -prefix (negation)
- precedence: NOT > AND > OR. Bare adjacency ("a b") keeps the historic
  "should" (OR) semantics, so existing behavior is unchanged.

Model: the query compiles to a list of OR-groups; each group has
required / optional ("should") / excluded atoms. A document matches if ANY
group matches, and within a group matches iff it contains every required
term, zero excluded terms, and (when the group has no required terms) at
least one optional term. That maps exactly onto NOT>AND>OR precedence.

`terms` / `phrases` / `filters` / `text` are kept for backward compatibility
(all positive terms + phrases in flat form); `groups` carries the boolean
structure that BM25/hybrid use for the gate.

Bounds (BUG-01 hardening): a query contributes at most
NEXUS_MAX_QUERY_TERMS positive terms (default 128) and 64 phrases; beyond
that the extras are dropped and the `terms_truncated` / `phrases_truncated`
flags are set. Real queries never approach these; adversarial 2,000-char
junk must not turn into unbounded per-request work. parse_query is also
memoized per query string (it is a pure function) with defensive copies on
every hit, because one /search request parses the same query up to four
times (understanding, retrieval, facet pass).
"""
import os
from dataclasses import dataclass, field
import re

from .tokenizer import tokenize

_PHRASE_RE = r'"((?:[^"\\]|\\.)*)"'       # quoted, with \" escapes
# atom = [field:"phrase"] | ["phrase"] | [word]
_ATOM_RE = re.compile(rf'([A-Za-z_]+):{_PHRASE_RE}|{_PHRASE_RE}|(\S+)')

_OPERATORS = {"AND", "OR", "NOT"}

FILTER_KEYS = {"type", "doc_type", "lang", "language"}
FILTER_KEY_MAP = {"type": "doc_type", "doc_type": "doc_type",
                  "lang": "language", "language": "language"}
FIELD_KEYS = {"title"}


def _unescape(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text)


@dataclass
class Group:
    """One OR-branch of the boolean query."""
    required: list[str] = field(default_factory=list)
    optional: list[str] = field(default_factory=list)
    required_phrases: list[str] = field(default_factory=list)
    optional_phrases: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    excluded_phrases: list[str] = field(default_factory=list)
    title_terms: list[str] = field(default_factory=list)
    title_phrases: list[str] = field(default_factory=list)

    def positive_terms(self) -> list[str]:
        out = list(self.required) + list(self.optional) + self.title_terms
        for ph in self.required_phrases + self.optional_phrases + self.title_phrases:
            out.extend(tokenize(ph))
        return out

    @staticmethod
    def any_term() -> str:
        return "any"


@dataclass
class ParsedQuery:
    terms: list[str] = field(default_factory=list)       # flat, positive terms
    phrases: list[str] = field(default_factory=list)     # required phrases
    filters: dict[str, str] = field(default_factory=dict)
    not_filters: dict[str, str] = field(default_factory=dict)  # -type:pdf / NOT type:pdf
    text: str = ""
    groups: list[Group] = field(default_factory=list)    # boolean structure
    # set when the term/phrase caps below dropped part of the query
    terms_truncated: bool = False
    phrases_truncated: bool = False

    @property
    def has_boolean(self) -> bool:
        """True iff any non-trivial boolean structure is in play."""
        changed = (len(self.groups) > 1
                   or (self.groups and (self.groups[0].required or self.groups[0].excluded
                                         or self.groups[0].excluded_phrases
                                         or self.groups[0].title_terms
                                         or self.groups[0].title_phrases)))
        return bool(changed)


def _split_tokens(query: str) -> list[tuple[str, bool, str]]:
    """Tokenize a query into (value, is_phrase, field) atoms, honoring quotes,
    escapes and a `key:"phrase"` field prefix on phrases."""
    atoms: list[tuple[str, bool, str]] = []
    for match in _ATOM_RE.finditer(query.strip()):
        field_key_, field_phrase, phrase, word = match.groups()
        if field_phrase is not None:
            atoms.append((field_phrase, True, field_key_))
        elif phrase is not None:
            atoms.append((phrase, True, ""))
        elif word:
            atoms.append((word, False, ""))
    return atoms


def parse_query(query: str) -> ParsedQuery:
    """Parse `query` (memoized per string; returns a fresh copy each call).

    Bounds: at most NEXUS_MAX_QUERY_TERMS positive terms (default 128) and
    at most 64 phrases are kept; anything beyond is dropped and the
    corresponding *_truncated flag is set. Deterministic: the kept part is
    always the query's own leading terms/phrases in order."""
    return _copy_parsed(_parse_query_cached(query, _max_query_terms()))


def _max_query_terms() -> int:
    raw = (os.environ.get("NEXUS_MAX_QUERY_TERMS") or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return 128
    return value if value > 0 else 128


# 64 phrases is generous (each surviving phrase gates every candidate doc);
# the cap exists so a quoted-junk flood cannot make phrase matching the hot
# path. Not env-configurable on purpose: fewer knobs, same honesty.
_MAX_QUERY_PHRASES = 64


from functools import lru_cache  # noqa: E402


@lru_cache(maxsize=512)
def _parse_query_cached(query: str, max_terms: int) -> ParsedQuery:
    """Bounded memo of the PURE parse (never handed out directly).
    parse_query is a pure function of the string, and one /search request
    parses the same query up to four times (understanding, retrieval,
    facet pass). 512 unique strings covers realistic working sets; the
    term cap participates in the key so env changes are always honored."""
    parsed = _parse_query_uncapped(query)
    _apply_caps(parsed, max_terms)
    return parsed


def _copy_parsed(parsed: ParsedQuery) -> ParsedQuery:
    """Defensive copy: callers get their own lists/dicts even on memo hits,
    so no consumer can contaminate the cached structure."""
    return ParsedQuery(
        terms=list(parsed.terms),
        phrases=list(parsed.phrases),
        filters=dict(parsed.filters),
        not_filters=dict(parsed.not_filters),
        text=parsed.text,
        groups=[
            Group(required=list(g.required), optional=list(g.optional),
                  required_phrases=list(g.required_phrases),
                  optional_phrases=list(g.optional_phrases),
                  excluded=list(g.excluded),
                  excluded_phrases=list(g.excluded_phrases),
                  title_terms=list(g.title_terms),
                  title_phrases=list(g.title_phrases))
            for g in parsed.groups
        ],
        terms_truncated=parsed.terms_truncated,
        phrases_truncated=parsed.phrases_truncated,
    )


def _apply_caps(parsed: ParsedQuery, max_terms: int) -> None:
    if len(parsed.terms) > max_terms:
        parsed.terms = parsed.terms[:max_terms]
        parsed.terms_truncated = True
    if len(parsed.phrases) > _MAX_QUERY_PHRASES:
        parsed.phrases = parsed.phrases[:_MAX_QUERY_PHRASES]
        parsed.phrases_truncated = True


def _parse_query_uncapped(query: str) -> ParsedQuery:
    parsed = ParsedQuery()
    atoms = _split_tokens(query)
    groups: list[Group] = [Group()]

    # pending operator state
    pending_and = False
    pending_not = False

    words_for_text: list[str] = []

    def current() -> Group:
        return groups[-1]

    def add_term(raw: str, is_phrase: bool, field: str = ""):
        nonlocal pending_and, pending_not
        value = _unescape(raw) if is_phrase else raw

        # field-scoped: title:x / title:"x y" (phrase prefix arrives via `field`)
        field_title = field.lower() in FIELD_KEYS
        # key:phrase with a FILTER key is not a phrase — treat as filter.
        # NOT/- negation is honored (not_filters), not silently discarded.
        if is_phrase and field.lower() in FILTER_KEYS:
            target = parsed.not_filters if pending_not else parsed.filters
            target[FILTER_KEY_MAP[field.lower()]] = value.strip().lower() or value.strip()
            pending_and = pending_not = False
            return
        if is_phrase and field and not field_title:
            # unknown field on a phrase: keep as plain phrase terms
            field = ""
        if not is_phrase and ":" in value and not value.startswith("\\"):
            key, _, rest = value.partition(":")
            key_l = key.lower().strip()
            rest = rest.strip()
            if key_l in FILTER_KEYS:
                # an empty value ("type:") is a no-op, not the term "type"
                if rest:
                    target = parsed.not_filters if pending_not else parsed.filters
                    target[FILTER_KEY_MAP[key_l]] = rest.lower()
                pending_and = pending_not = False
                return
            if key_l in FIELD_KEYS and rest:
                field_title = True
                value = rest

        negated = pending_not
        pending_and_pending = pending_and
        pending_and = pending_not = False

        if field_title:
            target = current().title_phrases if is_phrase else current().title_terms
            toks = tokenize(value)
            if is_phrase:
                if toks:
                    target.append(value.lower())
                    parsed.phrases.append(value.lower())
            else:
                target.extend(toks)
                parsed.terms.extend(toks)   # still a BM25 candidate term;
            words_for_text.append(value)    # the gate enforces title placement
            return

        if negated:
            if is_phrase:
                current().excluded_phrases.append(value.lower())
            else:
                current().excluded.extend(tokenize(value))
            return

        toks = tokenize(value)
        if is_phrase:
            if toks:
                parsed.phrases.append(value.lower())
                words_for_text.append(value)
                if pending_and_pending:
                    current().required_phrases.append(value.lower())
                else:
                    current().optional_phrases.append(value.lower())
            return
        if not toks:
            return
        parsed.terms.extend(toks)
        words_for_text.append(value)
        (current().required if pending_and_pending else current().optional).extend(toks)

    def _promote_last_optional(g: Group) -> None:
        """AND joins its LEFT operand into required-land, not just its right."""
        if g.optional:
            g.required.append(g.optional.pop())
        elif g.optional_phrases:
            g.required_phrases.append(g.optional_phrases.pop())

    for raw, is_phrase, field_key in atoms:
        if not is_phrase and raw in _OPERATORS:
            if raw == "OR":
                groups.append(Group())
                pending_and = pending_not = False
            elif raw == "AND":
                _promote_last_optional(current())
                pending_and = True
            else:  # NOT
                pending_not = True
            continue
        # -prefix negation (binds like NOT). Repeated leading dashes fold to
        # one: "--foo" is a typo for "-foo", not a positive term "foo".
        if not is_phrase and raw.startswith("-") and raw.lstrip("-"):
            pending_not = True
            raw = raw.lstrip("-")
        add_term(raw, is_phrase, field_key)

    # A NOT that swallowed nothing is dropped; empty groups from a trailing
    # OR are dropped too — malformed tails must not change results.
    groups = [g for g in groups if (g.required or g.optional or g.excluded
                                    or g.required_phrases or g.optional_phrases
                                    or g.excluded_phrases or g.title_terms
                                    or g.title_phrases)]
    if not groups and (parsed.terms or parsed.phrases):
        groups = [Group(optional=[], optional_phrases=list(parsed.phrases))]
    parsed.groups = groups
    parsed.text = " ".join(words_for_text)
    return parsed


def _phrase_in(tokens, phrase: list[str]) -> bool:
    """Token-sequence match, sequence-type agnostic (tuples vs lists)."""
    ph = tuple(phrase)
    n = len(ph)
    return n > 0 and any(tuple(tokens[i:i + n]) == ph for i in range(len(tokens) - n + 1))


def group_match(doc_tokens: list[str], title_tokens: list[str], group: Group) -> bool:
    """Boolean gate for one document against one OR-group."""
    doc_set = set(doc_tokens)
    title_set = set(title_tokens)
    for term in group.excluded:
        if term in doc_set:
            return False
    for ph in group.excluded_phrases:
        if _phrase_in(doc_tokens, tokenize(ph)):
            return False
    for term in group.required:
        if term not in doc_set:
            return False
    for ph in group.required_phrases:
        if not _phrase_in(doc_tokens, tokenize(ph)):
            return False
    for term in group.title_terms:
        if term not in title_set:
            return False
    for ph in group.title_phrases:
        if not _phrase_in(title_tokens, tokenize(ph)):
            return False
    if group.required or group.title_terms or group.required_phrases or group.title_phrases:
        return True
    if group.optional or group.optional_phrases:
        return any(t in doc_set for t in group.optional) or any(
            _phrase_in(doc_tokens, tokenize(ph)) for ph in group.optional_phrases)
    return False


def boolean_match(parsed: ParsedQuery, doc_tokens: list[str], title_tokens: list[str]) -> bool:
    """Document satisfies the query's boolean structure (any group matches).
    No groups -> True (the caller's existing term/phrases logic governs)."""
    if not parsed.groups:
        return True
    return any(group_match(doc_tokens, title_tokens, g) for g in parsed.groups)
