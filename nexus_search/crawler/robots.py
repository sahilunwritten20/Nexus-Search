"""RFC 9309 robots.txt parsing + matching, stdlib-only (review
ROBOTS-WILDCARD).

Replaces urllib.robotparser in PolitenessManager because the stdlib's
wildcard support differs BY PYTHON VERSION: 3.12 treats `*` and `$` as
literal characters (sites using the extremely common `Disallow: /*.pdf$`
syntax are over-crawled), while 3.14 added wildcard semantics. A
crawler's politeness behavior must not depend on the interpreter
version — and this repo's CI runs 3.12 while dev machines run 3.14.

Conformance (RFC 9309, test-pinned in tests/crawler/test_robots_rfc9309.py):
- `*` matches any sequence (including empty); `$` is special only as the
  FINAL character (a mid-pattern $ is literal).
- Rules are prefix-anchored; the most specific matching rule (longest
  pattern text) wins; ties go to the LEAST restrictive rule (Allow).
- No matching rule -> allowed. Empty robots.txt -> allowed. Empty
  `Disallow:` value -> allowed.
- Group selection: exact case-insensitive match of the crawler's
  product token; if (and only if) none matches, the `*` group applies;
  all matching groups merge (rules union; last Crawl-delay wins).
- Matching considers path AND query string (RFC 9309 §2.2.4 note);
  /robots.txt itself is implicitly allowed.
- BOM, CRLF, blank lines, full-line and trailing `#` comments handled.
"""
import re
from urllib.parse import urlsplit


class _Rule:
    __slots__ = ("allow", "pattern", "specificity", "_regex")

    def __init__(self, allow: bool, pattern: str):
        self.allow = allow
        self.pattern = pattern
        # specificity = the pattern's text length (RFC 9309 §2.2.4:
        # "the rule with the longer path" is the more specific one)
        self.specificity = len(pattern)
        self._regex = self._compile(pattern)

    @staticmethod
    def _compile(pattern: str) -> re.Pattern:
        anchor_end = pattern.endswith("$")
        if anchor_end:
            pattern = pattern[:-1]
        parts = ["^"]
        for ch in pattern:
            parts.append(".*" if ch == "*" else re.escape(ch))
        if anchor_end:
            parts.append("$")
        return re.compile("".join(parts))

    def matches(self, path: str) -> bool:
        return self._regex.match(path) is not None


class _Group:
    __slots__ = ("agents", "rules", "crawl_delay")

    def __init__(self):
        self.agents: set[str] = set()
        self.rules: list[_Rule] = []
        self.crawl_delay: float | None = None


_EMPTY_RULES = re.compile(r"^(user-agent|disallow|allow|crawl-delay|sitemap)\s*$",
                          re.IGNORECASE)


def _split_groups(text: str) -> tuple[list[_Group], list[str]]:
    """RFC 9309 §2.3.1: consecutive user-agent lines open a group; the
    group ends at the next user-agent line that follows any rule.
    Returns (groups, sitemaps) — Sitemap lines are file-wide."""
    groups: list[_Group] = []
    sitemaps: list[str] = []
    current: _Group | None = None
    pending_agents: list[str] = []  # user-agents awaiting their rules
    saw_rule_in_current = False

    raw = text.lstrip("﻿")
    for line in re.split(r"\r?\n", raw):
        line = line.strip()
        if not line:
            continue
        # comments: everything from the first '#' is not part of the line
        if "#" in line:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
        if ":" not in line:
            continue  # malformed line: RFC says ignore, keep parsing
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "sitemap":
            if value:
                sitemaps.append(value)
            continue
        if key == "user-agent":
            if not value:
                continue
            if current is None or saw_rule_in_current:
                # a rule line closed the previous group's agent block
                current = _Group()
                groups.append(current)
                saw_rule_in_current = False
            current.agents.add(value.lower())
            continue
        if key not in ("allow", "disallow", "crawl-delay"):
            continue  # unknown extension keys are ignored per RFC §2.3
        if current is None:
            # rules before any user-agent line apply to no group per the
            # RFC; the common forgiving read is to treat them as '*'
            current = _Group()
            current.agents.add("*")
            groups.append(current)
        if key == "crawl-delay":
            try:
                current.crawl_delay = float(value)
            except ValueError:
                pass  # non-numeric crawl-delay: ignore per RFC
            saw_rule_in_current = True
            continue
        if not value:
            # "Disallow:" with no value means "allow everything" (the
            # rule set for that group is empty)
            saw_rule_in_current = True
            continue
        current.rules.append(_Rule(allow=(key == "allow"), pattern=value))
        saw_rule_in_current = True
    return groups, sitemaps


class RobotsRules:
    """Parsed robots.txt; deterministic and version-independent."""

    def __init__(self, text: str):
        self.groups, self.sitemaps = _split_groups(text or "")

    def _matching_groups(self, useragent: str) -> list[_Group]:
        token = (useragent or "").strip().lower()
        exact = [g for g in self.groups if token in g.agents]
        if exact:
            return exact
        return [g for g in self.groups if "*" in g.agents]

    def is_allowed(self, useragent: str, url: str) -> bool:
        parts = urlsplit(url)
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        if target == "/robots.txt":
            return True  # implicitly allowed (RFC 9309 §2.3.3)
        matched = self._matching_groups(useragent)
        if not matched:
            return True
        best: _Rule | None = None
        for group in matched:
            for rule in group.rules:
                if not rule.matches(target):
                    continue
                if (best is None
                        or rule.specificity > best.specificity
                        or (rule.specificity == best.specificity
                            and rule.allow)):
                    best = rule
        return True if best is None else best.allow

    def crawl_delay(self, useragent: str) -> float | None:
        """The matched groups' Crawl-delay (last one wins on merge)."""
        delays = [g.crawl_delay for g in self._matching_groups(useragent)
                  if g.crawl_delay is not None]
        return delays[-1] if delays else None
