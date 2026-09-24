"""Autocomplete / query suggestions / related searches for Nexus Search Phase 5.

Prefix index: a sorted list + bisect scan over the postings vocabulary the
index already holds (Storage.all_terms()). Deliberately NOT a trie library:
at prototype scale (thousands of docs → tens of thousands of terms) the
sorted list is ~1MB and a binary-search prefix scan is microseconds; a trie
would be more code and a new dependency for zero measurable win.

Related searches: co-occurrence over the query_experiments log (Stage 3 A/B
instrumentation) when it has rows; falls back to character-trigram term
similarity against the index vocabulary when the log is empty. An empty log
table must NEVER raise.
"""
import bisect
from collections import Counter
from typing import Optional


def _trigrams(term: str) -> set[str]:
    return {term[i:i + 3] for i in range(len(term) - 2)} if len(term) > 2 else {term}


class Suggester:
    """Prefix autocomplete over the live index vocabulary."""

    def __init__(self, storage):
        self.storage = storage
        self._terms: list[str] = []
        self._doc_count_at_load = -1  # cheap change detector for the cache

    def _refresh(self) -> None:
        """Rebuild the sorted vocabulary when the document count changed.
        Doc-count is a cheap proxy: it misses same-count content swaps, and
        suggestions tolerate that (the stale term simply pulls 0 documents)."""
        count = self.storage.document_count()
        if count != self._doc_count_at_load:
            self._terms = self.storage.all_terms()
            self._doc_count_at_load = count

    def suggest(self, prefix: str, limit: int = 10) -> list[str]:
        """Up to `limit` index terms starting with `prefix` (case-folded),
        most frequent first (document frequency) — a suggestion that leads to
        zero results would be a bad suggestion."""
        prefix = (prefix or "").strip().lower()
        if not prefix:
            return []
        self._refresh()
        lo = bisect.bisect_left(self._terms, prefix)
        matches = []
        i = lo
        while i < len(self._terms) and self._terms[i].startswith(prefix) and len(matches) < limit * 4:
            matches.append(self._terms[i])
            i += 1
        # over-collect 4x then rank by document frequency for real usefulness
        matches.sort(key=lambda t: (-self.storage.document_frequency(t), t))
        return matches[:limit]

    def related_searches(self, query: str, experiment_log=None, limit: int = 5) -> list[str]:
        """Queries related to `query`.

        Source order:
        1. the query log (other logged queries sharing a term with this one)
        2. trigram-similar index terms never present in the query itself

        Empty log + empty index both return [] without error."""
        from ..core.query_parser import parse_query
        terms = set(parse_query(query).terms)
        related: list[str] = []

        if experiment_log is not None:
            try:
                related = self._from_log(query, experiment_log, limit)
            except Exception:
                related = []  # a logging table must never break suggestions
        if related or experiment_log is not None:
            return related[:limit]

        # Fallback: term similarity against the index vocabulary.
        self._refresh()
        scored: Counter[str] = Counter()
        qgrams = set().union(*(_trigrams(t) for t in terms)) if terms else set()
        for term in self._terms:
            if term in terms:
                continue
            shared = len(_trigrams(term) & qgrams)
            if shared:
                scored[term] = shared
        return [t for t, _ in sorted(scored.items(),
                                     key=lambda kv: (-kv[1], -self.storage.document_frequency(kv[0]), kv[0]))[:limit]]

    def _from_log(self, query: str, log, limit: int) -> list[str]:
        from ..core.query_parser import parse_query
        my_terms = set(parse_query(query).terms)
        counts: Counter[str] = Counter()
        rows = log.conn.execute(
            "SELECT DISTINCT query FROM query_experiments"
        ).fetchall()
        for (other,) in rows:
            other_terms = set(parse_query(other).terms)
            if my_terms & other_terms and other != query:
                counts[other] += len(my_terms & other_terms)
        return [q for q, _ in counts.most_common(limit)]
