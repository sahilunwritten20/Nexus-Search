"""BM25 retrieval with filters, title boost, required phrases, snippets,
pagination, and chunk grouping."""
import math
import threading
from dataclasses import dataclass, field
from typing import Optional

from .filters import matches_filters
from .query_parser import boolean_match, parse_query
from .storage import Storage
from .tokenizer import tokenize

K1 = 1.5
B = 0.75
TITLE_BOOST = 1.8
PHRASE_BOOST = 1.25


@dataclass
class SearchResult:
    doc_id: str  # the PARENT doc's id when the hit is a chunk
    score: float
    title: str
    snippet: str
    doc_type: str
    metadata: dict = field(default_factory=dict)
    chunk_id: Optional[str] = None  # best-matching chunk, if the doc was chunked
    matched_chunks: int = 1


@dataclass
class SearchPage:
    total: int  # all matches (distinct docs when grouped), before offset/top_k
    results: list[SearchResult] = field(default_factory=list)


class BM25Search:
    def __init__(self, storage: Storage, k1: float = K1, b: float = B):
        self.storage = storage
        self.k1 = k1
        self.b = b
        # Doc-token memo: phrase/boolean/title gating used to re-tokenize
        # every candidate's title+content on EVERY query. keyed by
        # (doc_id, added_at): every write path refreshes added_at, so the
        # cache can never serve a doc's STALE tokens. Bounded (2048 docs).
        self._token_cache: dict[tuple, tuple[tuple[str, ...], tuple[str, ...]]] = {}
        self._token_cache_order: list = []
        self._token_cache_lock = threading.Lock()

    def _tokens_for(self, doc) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """(title_tokens, doc_tokens) for a stored row, memoized by
        (doc_id, added_at)."""
        key = (doc.doc_id, doc.added_at)
        with self._token_cache_lock:
            hit = self._token_cache.get(key)
            if hit is not None:
                return hit
        value = (tuple(tokenize(doc.title)), tuple(tokenize(f"{doc.title} {doc.content}")))
        with self._token_cache_lock:
            if key not in self._token_cache:
                self._token_cache_order.append(key)
                self._token_cache[key] = value
                while len(self._token_cache_order) > 2048:
                    old = self._token_cache_order.pop(0)
                    self._token_cache.pop(old, None)
        return value

    def _idf(self, term: str, n_docs: int) -> float:
        n_t = self.storage.document_frequency(term)
        return math.log((n_docs - n_t + 0.5) / (n_t + 0.5) + 1)

    @staticmethod
    def _has_phrase(doc_tokens: list[str], phrase: list[str]) -> bool:
        """Token-sequence match. Sequence-typed both ways (list vs tuple)
        so the doc-token memo (tuples) and parser output (lists) agree."""
        phrase_t = tuple(phrase)
        n = len(phrase_t)
        return any(tuple(doc_tokens[i:i + n]) == phrase_t for i in range(len(doc_tokens) - n + 1))

    @staticmethod
    def _snippet(doc, terms: list[str], phrases: list[str], width: int = 200,
                 highlight: bool = False, mark: tuple[str, str] = ("<mark>", "</mark>")) -> str:
        """Query-focused snippet. highlight=True wraps matched terms/phrases
        in <mark>..</mark> (configurable via `mark`) inside HTML-escaped
        text — safe to render, untrusted content cannot inject markup
        (P1-6). highlight=False returns the snippet as PLAIN TEXT,
        byte-identical to the pre-highlight behavior. The window logic is
        unchanged; highlighting is applied to the chosen window, so the two
        modes can never disagree about WHERE the snippet comes from."""
        text = doc.content
        lowered = text.lower()
        positions = [lowered.find(p.lower()) for p in phrases if p]
        positions += [lowered.find(t.lower()) for t in terms if t]
        positions = [p for p in positions if p >= 0]
        if not positions:
            snippet = text[:width] + ("..." if len(text) > width else "")
        else:
            start = max(0, min(positions) - width // 4)
            snippet = text[start:start + width]
            if start > 0:
                snippet = "..." + snippet
            if start + width < len(text):
                snippet += "..."
        if highlight:
            snippet = BM25Search._highlight(snippet, terms, phrases, mark)
        return snippet

    @staticmethod
    def _highlight(snippet: str, terms: list[str], phrases: list[str],
                   mark: tuple[str, str] = ("<mark>", "</mark>")) -> str:
        """Wrap exact (case-insensitive) phrase/term matches in the window
        and return HTML: every TEXT segment is html.escape()d, the mark
        tags and the wrapped match text are the only live markup (P1-6 —
        document content is untrusted and must not ride along as HTML).
        Longest-first so phrases win over their component words.

        Matching happens on the RAW snippet (no offset shifting); escaping
        is applied per segment AFTER segmentation, so the two modes agree
        exactly about WHERE the snippet and its matches are.

        A match that lands inside an EXPLICIT mark-tag pair in the
        SOURCE text is not wrapped (that would emit broken nested tags) —
        tracked via real tag spans, not counting, so document content
        cannot suppress highlighting (counting opens before the match
        could be thrown off by an unbalanced literal tag string)."""
        import html
        import re
        targets = sorted({p for p in phrases if p} | {t for t in terms if t},
                          key=len, reverse=True)
        open_m, close_m = mark
        if not targets:
            # still HTML output: escape even when nothing matches
            return html.escape(snippet)
        # protected regions: paired open/close tags in the SOURCE snippet
        protected: list[tuple[int, int]] = []
        for tag_m in re.finditer(re.escape(open_m) + "|" + re.escape(close_m), snippet):
            if tag_m.group(0) == open_m:
                protected.append([tag_m.start(), None])
            elif protected and protected[-1][1] is None:
                protected[-1][1] = tag_m.end()
        protected = [(s, e) for s, e in protected if e is not None]
        pattern = re.compile("|".join(re.escape(t) for t in targets), re.IGNORECASE)

        out, pos = [], 0
        for m in pattern.finditer(snippet):
            if any(m.start() < end and m.end() > start for start, end in protected):
                continue
            out.append(html.escape(snippet[pos:m.start()]))
            out.append(open_m + html.escape(m.group(0)) + close_m)
            pos = m.end()
        out.append(html.escape(snippet[pos:]))
        return "".join(out)

    def search_page(
        self, query: str, top_k: int = 10, offset: int = 0, group_chunks: bool = True,
        highlight: bool = False,
    ) -> SearchPage:
        """Ranked page of results plus the total match count.

        group_chunks=True collapses all chunks of one parent into a single
        result (best chunk wins); False returns raw chunk-level hits.
        highlight=False (default) returns plain-text snippets, byte-identical
        to pre-Phase-5; True returns HTML-escaped snippets with matches
        wrapped in <mark> tags (render-safe: content markup is escaped).
        """
        if top_k <= 0:
            return SearchPage(0)
        offset = max(offset, 0)
        parsed = parse_query(query)
        phrases = [t for t in (tokenize(p) for p in parsed.phrases) if t]
        terms = parsed.terms + [t for ph in phrases for t in ph]

        cache: dict = {}

        def get(doc_id):
            if doc_id not in cache:
                cache[doc_id] = self.storage.get_document(doc_id)
            return cache[doc_id]

        scores: dict[str, float] = {}
        if terms:
            n_docs = self.storage.document_count()
            if n_docs == 0:
                return SearchPage(0)
            avg_len = self.storage.average_length()
            term_set = set(terms)
            # One batched fetch for every term's postings + document
            # frequencies instead of 2 queries per term (BUG-01: long-query
            # cost was dominated by per-term round trips). Accumulation
            # order below is IDENTICAL to the pre-batch code — same
            # set(terms) object, same per-term row order — so scores are
            # byte-identical (pinned by tests/golden).
            postings_by_term = self.storage.postings_for_terms(list(term_set))
            df_by_term = self.storage.document_frequencies(list(term_set))
            # One batched document fetch for every candidate id (the scale
            # benchmark showed the per-doc get_document loop was the BM25
            # cost at 10K docs). The per-request dict cache stays for the
            # phrase/title/token gates below.
            candidate_ids = {doc_id
                             for rows in postings_by_term.values()
                             for doc_id, _tf in rows}
            docs_by_id = self.storage.get_documents(list(candidate_ids))
            # WP12-B4: seed the per-request cache from the batch — the
            # ranked/grouping loops below read through `get()` and were
            # re-fetching every doc one-by-one (the batch result was
            # never reused; audit R3).
            cache.update(docs_by_id)
            for term in term_set:
                n_t = df_by_term.get(term, 0)
                idf = math.log((n_docs - n_t + 0.5) / (n_t + 0.5) + 1)
                for doc_id, tf in postings_by_term.get(term, ()):
                    doc = docs_by_id.get(doc_id)
                    if doc is None or not matches_filters(doc, parsed.filters, parsed.not_filters):
                        continue
                    norm = 1 - self.b + self.b * (doc.length / avg_len if avg_len else 1)
                    scores[doc_id] = scores.get(doc_id, 0.0) + idf * (tf * (self.k1 + 1)) / (tf + self.k1 * norm)
        elif parsed.filters or parsed.not_filters:  # filter-only query, e.g. "type:pdf" / "-type:pdf"
            # WP12-B4: ONE metadata-only scan for the filter + ONE batched
            # full read of just the passing docs (was a get_document per doc
            # in the corpus).
            meta = self.storage.get_documents_meta()
            passing = [doc_id for doc_id, doc in meta.items()
                       if matches_filters(doc, parsed.filters, parsed.not_filters)]
            cache.update(self.storage.get_documents(passing))
            for doc_id in passing:
                scores[doc_id] = 0.0
        else:
            return SearchPage(0)

        unique_terms = set(terms)
        ranked = []
        for doc_id, score in scores.items():
            doc = get(doc_id)
            title_tokens, doc_tokens = ((), ())
            if phrases or parsed.has_boolean or unique_terms:
                title_tokens, doc_tokens = self._tokens_for(doc)  # memoized per (doc_id, added_at)
            if phrases:  # quoted phrases are REQUIRED, matched as token sequences
                if not all(self._has_phrase(doc_tokens, ph) for ph in phrases):
                    continue
                score *= PHRASE_BOOST ** len(phrases)
            if parsed.has_boolean:
                if not boolean_match(parsed, doc_tokens, title_tokens):
                    continue
            if unique_terms:
                title_term_set = set(title_tokens)
                hits = sum(1 for t in unique_terms if t in title_term_set)
                score *= 1 + (TITLE_BOOST - 1) * min(hits / len(unique_terms), 1.0)
            ranked.append((doc_id, score))

        ranked.sort(key=lambda kv: (-kv[1], kv[0]))

        # Group chunk hits under their parent (dict keeps best-first order).
        groups: dict[str, list[tuple[str, float]]] = {}
        for doc_id, score in ranked:
            doc = get(doc_id)
            if doc is None:
                continue
            parent = doc.metadata.get("parent_id", doc_id) if group_chunks else doc_id
            groups.setdefault(parent, []).append((doc_id, score))

        results = []
        for parent, members in list(groups.items())[offset:offset + top_k]:
            best_id, best_score = members[0]
            best = get(best_id)
            if best is None:
                continue
            results.append(
                SearchResult(
                    doc_id=parent,
                    score=best_score,
                    title=best.title,
                    snippet=self._snippet(best, parsed.terms, parsed.phrases,
                                          highlight=highlight),
                    doc_type=best.doc_type,
                    metadata=best.metadata,
                    chunk_id=best_id if best_id != parent else None,
                    matched_chunks=len(members),
                )
            )
        return SearchPage(total=len(groups), results=results)

    def search(self, query: str, top_k: int = 10) -> list[SearchResult]:
        return self.search_page(query, top_k=top_k).results