"""BM25 retrieval with filters, title boost, required phrases, snippets,
pagination, and chunk grouping."""
import math
from dataclasses import dataclass, field
from typing import Optional

from .filters import matches_filters
from .query_parser import parse_query
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

    def _idf(self, term: str, n_docs: int) -> float:
        n_t = self.storage.document_frequency(term)
        return math.log((n_docs - n_t + 0.5) / (n_t + 0.5) + 1)

    @staticmethod
    def _has_phrase(doc_tokens: list[str], phrase: list[str]) -> bool:
        n = len(phrase)
        return any(doc_tokens[i:i + n] == phrase for i in range(len(doc_tokens) - n + 1))

    @staticmethod
    def _snippet(doc, terms: list[str], phrases: list[str], width: int = 200,
                 highlight: bool = False, mark: tuple[str, str] = ("<mark>", "</mark>")) -> str:
        """Query-focused snippet. highlight=True wraps matched terms/phrases
        in <mark>..</mark> (configurable via `mark`). The window logic is
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
                   mark: tuple[str, str]) -> str:
        """Wrap exact (case-insensitive) phrase/term matches in the window.
        Longest-first so phrases win over their component words; text inside
        an existing <mark> is not re-wrapped."""
        import re
        targets = sorted({p for p in phrases if p} | {t for t in terms if t},
                         key=len, reverse=True)
        if not targets:
            return snippet
        open_m, close_m = mark
        pattern = re.compile("|".join(re.escape(t) for t in targets), re.IGNORECASE)

        def wrap(match):
            return open_m + match.group(0) + close_m

        out, pos = [], 0
        for m in pattern.finditer(snippet):
            # skip matches inside an already-open mark region
            opens = snippet.count(open_m, 0, m.start()) - snippet.count(close_m, 0, m.start())
            if opens > 0:
                continue
            out.append(snippet[pos:m.start()])
            out.append(wrap(m))
            pos = m.end()
        out.append(snippet[pos:])
        return "".join(out)

    def search_page(
        self, query: str, top_k: int = 10, offset: int = 0, group_chunks: bool = True,
        highlight: bool = False,
    ) -> SearchPage:
        """Ranked page of results plus the total match count.

        group_chunks=True collapses all chunks of one parent into a single
        result (best chunk wins); False returns raw chunk-level hits.
        highlight=False (default) returns plain snippets, byte-identical to
        pre-Phase-5; True wraps matches in <mark> tags.
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
            for term in set(terms):
                idf = self._idf(term, n_docs)
                for doc_id, tf in self.storage.postings_for_term(term):
                    doc = get(doc_id)
                    if doc is None or not matches_filters(doc, parsed.filters):
                        continue
                    norm = 1 - self.b + self.b * (doc.length / avg_len if avg_len else 1)
                    scores[doc_id] = scores.get(doc_id, 0.0) + idf * (tf * (self.k1 + 1)) / (tf + self.k1 * norm)
        elif parsed.filters:  # filter-only query, e.g. "type:pdf"
            for doc_id in self.storage.all_doc_ids():
                doc = get(doc_id)
                if doc and matches_filters(doc, parsed.filters):
                    scores[doc_id] = 0.0
        else:
            return SearchPage(0)

        unique_terms = set(terms)
        ranked = []
        for doc_id, score in scores.items():
            doc = get(doc_id)
            if phrases:  # quoted phrases are REQUIRED, matched as token sequences
                doc_tokens = tokenize(f"{doc.title} {doc.content}")
                if not all(self._has_phrase(doc_tokens, ph) for ph in phrases):
                    continue
                score *= PHRASE_BOOST ** len(phrases)
            if unique_terms:
                title_terms = set(tokenize(doc.title))
                hits = sum(1 for t in unique_terms if t in title_terms)
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