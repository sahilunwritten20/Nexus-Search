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

# SKEP-2 (P2): a site we crawl controls this number; 60s is the maximum
# pacing the crawler will honor (Google ignores Crawl-delay entirely;
# 60s is generous for real politeness while bounding a hostile value).
CRAWL_DELAY_MAX = 60.0


class _Rule:
    __slots__ = ("allow", "pattern", "specificity", "_body", "_anchored")

    def __init__(self, allow: bool, pattern: str):
        self.allow = allow
        self.pattern = pattern
        # specificity = the pattern's text length (RFC 9309 §2.2.4 /
        # Google's documented rule-length precedence, $ counted)
        self.specificity = len(pattern)
        # SKEP-1 (P1): compiling attacker-controlled patterns into regexes
        # allowed catastrophic backtracking ('/a*a*...*b' NEVER returned —
        # measured hang at 8 stars x 5000 chars, freezing the whole
        # process). Matching is now a LINEAR two-pointer glob.
        self._anchored = pattern.endswith("$")
        self._body = pattern[:-1] if self._anchored else pattern

    def matches(self, path: str) -> bool:
        return _glob_match(self._body, path, require_full_text=self._anchored)


def _glob_match(pattern: str, text: str, require_full_text: bool) -> bool:
    """Linear wildcard match (SKEP-1): '*' matches any sequence
    (including empty), everything else is literal; the match is
    left-anchored. With require_full_text (the trailing-$ form), the
    pattern must consume the ENTIRE text; otherwise a prefix match wins
    (classic robots prefix semantics). Worst case O(len(pattern) *
    len(text)) with NO exponential blowup — the classic star-anchored
    backtracking loop, not a regex engine."""
    pi = ti = 0
    star_p = star_t = -1
    n, m = len(pattern), len(text)
    while ti < m:
        if pi == n:
            if require_full_text:
                return False  # anchored form: trailing text breaks the $
            return True       # prefix form: pattern exhausted = match
        c = pattern[pi]
        if c == "*":
            star_p, star_t = pi, ti
            pi += 1
            continue
        if c == text[ti]:
            pi += 1
            ti += 1
            continue
        if star_p >= 0:
            # backtrack to the last star, let it absorb one more char
            pi = star_p + 1
            star_t += 1
            ti = star_t
            continue
        return False
    # text exhausted: trailing stars match the empty remainder
    while pi < n and pattern[pi] == "*":
        pi += 1
    return pi == n


class _Group:
    __slots__ = ("agents", "rules", "crawl_delay")

    def __init__(self):
        self.agents: set[str] = set()
        self.rules: list[_Rule] = []
        self.crawl_delay: float | None = None


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
            # SKEP-2 (P2): attacker-controlled value was consumed
            # unvalidated — 99999999 slept a worker ~3 years, NaN/-5
            # silently DISABLED pacing, 1e999 crashed time.sleep(inf).
            # Clamp finite values to [0, CRAWL_DELAY_MAX]; anything
            # non-numeric/negative/NaN is ignored (absent).
            try:
                delay = float(value)
            except ValueError:
                continue
            if not (delay >= 0) or delay > CRAWL_DELAY_MAX:
                # not(finite and >= 0) or absurdly large
                if delay > CRAWL_DELAY_MAX:
                    current.crawl_delay = CRAWL_DELAY_MAX
                # else: negative/NaN -> leave as absent
                saw_rule_in_current = True
                continue
            current.crawl_delay = delay
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
