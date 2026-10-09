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
        # WP13-3: inverted character-trigram index (gram -> terms) for the
        # related-searches fallback. Built once per vocabulary change; the
        # WP11-era code rebuilt every term's trigram set on EVERY request —
        # O(vocab) CPU per request once the query log is empty. Prototype
        # scale: tens of thousands of terms -> a few hundred thousand
        # postings, tens of MB — same scale acknowledgment as _terms.
        self._gram_index: dict[str, list[str]] = {}
        self._gram_count_at_load = -1

    def _refresh(self) -> None:
        """Rebuild the sorted vocabulary when the document count changed.
        Doc-count is a cheap proxy: it misses same-count content swaps, and
        suggestions tolerate that (the stale term simply pulls 0 documents)."""
        count = self.storage.document_count()
        if count != self._doc_count_at_load:
            self._terms = self.storage.all_terms()
            self._doc_count_at_load = count

    def _refresh_grams(self) -> None:
        """Rebuild the inverted trigram index when the vocabulary changed
        since it was built (same cheap doc-count proxy as _refresh)."""
        self._refresh()
        if self._gram_count_at_load != self._doc_count_at_load:
            index: dict[str, list[str]] = {}
            for term in self._terms:
                for gram in _trigrams(term):
                    index.setdefault(gram, []).append(term)
            self._gram_index = index
            self._gram_count_at_load = self._doc_count_at_load

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

    def related_searches(self, query: str, experiment_log=None, limit: int = 5,
                         max_log_rows: int = 10_000) -> list[str]:
        """Queries related to `query`.

        Source order:
        1. the query log (other logged queries sharing a term with this one),
           scanned over at most `max_log_rows` most recent rows — the log
           grows unboundedly, the scan must not
        2. trigram-similar index terms never present in the query itself

        Empty log + empty index both return [] without error."""
        from ..core.query_parser import parse_query
        terms = set(parse_query(query).terms)
        related: list[str] = []

        if experiment_log is not None:
            try:
                related = self._from_log(query, experiment_log, limit, max_log_rows)
            except Exception:
                related = []  # a logging table must never break suggestions
        if related:
            return related[:limit]
        # An EMPTY log (or a log with no co-occurrences) must not suppress the
        # trigram-similarity fallback — a present-but-useless log object is not
        # a reason to return nothing. Fall through either way.

        # Fallback: term similarity against the index vocabulary, via the
        # inverted trigram index (WP13-3): for each query gram, bump every
        # term sharing it — the same |term_grams ∩ query_grams| score the
        # naive per-request scan computed, at O(query grams × postings)
        # per request instead of O(whole vocabulary).
        self._refresh_grams()
        scored: Counter[str] = Counter()
        qgrams = set().union(*(_trigrams(t) for t in terms)) if terms else set()
        for gram in qgrams:
            for term in self._gram_index.get(gram, ()):
                if term in terms:
                    continue
                scored[term] += 1
        return [t for t, _ in sorted(scored.items(),
                                     key=lambda kv: (-kv[1], -self.storage.document_frequency(kv[0]), kv[0]))[:limit]]

    def _from_log(self, query: str, log, limit: int, max_log_rows: int = 10_000) -> list[str]:
        from ..core.query_parser import parse_query
        my_terms = set(parse_query(query).terms)
        counts: Counter[str] = Counter()
        rows = log.conn.execute(
            "SELECT DISTINCT query FROM (SELECT query FROM query_experiments "
            "ORDER BY id DESC LIMIT ?)", (max_log_rows,)
        ).fetchall()
        for (other,) in rows:
            other_terms = set(parse_query(other).terms)
            if my_terms & other_terms and other != query:
                counts[other] += len(my_terms & other_terms)
        return [q for q, _ in counts.most_common(limit)]
