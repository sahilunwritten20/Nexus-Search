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
"""
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
    text: str = ""
    groups: list[Group] = field(default_factory=list)    # boolean structure

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
        # key:phrase with a FILTER key is not a phrase — treat as filter
        if is_phrase and field.lower() in FILTER_KEYS:
            parsed.filters[FILTER_KEY_MAP[field.lower()]] = value.strip().lower() or value.strip()
            pending_and = pending_not = False
            return
        if is_phrase and field and not field_title:
            # unknown field on a phrase: keep as plain phrase terms
            field = ""
        if not is_phrase and ":" in value and not value.startswith("\\"):
            key, _, rest = value.partition(":")
            key_l = key.lower().strip()
            rest = rest.strip()
            if key_l in FILTER_KEYS and rest:
                parsed.filters[FILTER_KEY_MAP[key_l]] = rest.lower()
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
        # -prefix negation (binds like NOT)
        if not is_phrase and raw.startswith("-") and len(raw) > 1 and not raw.startswith("--"):
            pending_not = True
            raw = raw[1:]
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


def group_match(doc_tokens: list[str], title_tokens: list[str], group: Group) -> bool:
    """Boolean gate for one document against one OR-group."""
    doc_set = set(doc_tokens)
    title_set = set(title_tokens)
    for term in group.excluded:
        if term in doc_set:
            return False
    for ph in group.excluded_phrases:
        toks = tokenize(ph)
        n = len(toks)
        if toks and any(doc_tokens[i:i + n] == toks for i in range(len(doc_tokens) - n + 1)):
            return False
    for term in group.required:
        if term not in doc_set:
            return False
    for ph in group.required_phrases:
        toks = tokenize(ph)
        n = len(toks)
        if toks and not any(doc_tokens[i:i + n] == toks for i in range(len(doc_tokens) - n + 1)):
            return False
    for term in group.title_terms:
        if term not in title_set:
            return False
    for ph in group.title_phrases:
        toks = tokenize(ph)
        n = len(toks)
        if toks and not any(title_tokens[i:i + n] == toks for i in range(len(title_tokens) - n + 1)):
            return False
    if group.required or group.title_terms or group.required_phrases or group.title_phrases:
        return True
    if group.optional or group.optional_phrases:
        return any(t in doc_set for t in group.optional) or any(
            (lambda toks: toks and any(doc_tokens[i:i + len(toks)] == toks
                                       for i in range(len(doc_tokens) - len(toks) + 1)))(tokenize(ph))
            for ph in group.optional_phrases)
    return False


def boolean_match(parsed: ParsedQuery, doc_tokens: list[str], title_tokens: list[str]) -> bool:
    """Document satisfies the query's boolean structure (any group matches).
    No groups -> True (the caller's existing term/phrases logic governs)."""
    if not parsed.groups:
        return True
    return any(group_match(doc_tokens, title_tokens, g) for g in parsed.groups)
