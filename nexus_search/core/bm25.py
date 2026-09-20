"""BM25 retrieval with filters, title boost, required phrases, snippets, pagination."""
import math
from dataclasses import dataclass, field

from .query_parser import parse_query
from .storage import Storage
from .tokenizer import tokenize

K1 = 1.5
B = 0.75
TITLE_BOOST = 1.8
PHRASE_BOOST = 1.25


@dataclass
class SearchResult:
    doc_id: str
    score: float
    title: str
    snippet: str
    doc_type: str


@dataclass
class SearchPage:
    total: int  # all matches, before offset/top_k
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
    def _matches_filters(doc, filters: dict[str, str]) -> bool:
        if "doc_type" in filters and doc.doc_type.lower() != filters["doc_type"]:
            return False
        if "language" in filters and str(doc.metadata.get("language", "")).lower() != filters["language"]:
            return False
        return True

    @staticmethod
    def _has_phrase(doc_tokens: list[str], phrase: list[str]) -> bool:
        n = len(phrase)
        return any(doc_tokens[i:i + n] == phrase for i in range(len(doc_tokens) - n + 1))

    @staticmethod
    def _snippet(doc, terms: list[str], phrases: list[str], width: int = 200) -> str:
        text = doc.content
        lowered = text.lower()
        positions = [lowered.find(p.lower()) for p in phrases if p]
        positions += [lowered.find(t.lower()) for t in terms if t]
        positions = [p for p in positions if p >= 0]
        if not positions:
            return text[:width] + ("..." if len(text) > width else "")
        start = max(0, min(positions) - width // 4)
        snippet = text[start:start + width]
        if start > 0:
            snippet = "..." + snippet
        if start + width < len(text):
            snippet += "..."
        return snippet

    def search_page(self, query: str, top_k: int = 10, offset: int = 0) -> SearchPage:
        """Ranked page of results plus the total match count."""
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
                    if doc is None or not self._matches_filters(doc, parsed.filters):
                        continue
                    norm = 1 - self.b + self.b * (doc.length / avg_len if avg_len else 1)
                    scores[doc_id] = scores.get(doc_id, 0.0) + idf * (tf * (self.k1 + 1)) / (tf + self.k1 * norm)
        elif parsed.filters:  # filter-only query, e.g. "type:pdf"
            for doc_id in self.storage.all_doc_ids():
                doc = get(doc_id)
                if doc and self._matches_filters(doc, parsed.filters):
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
        results = [
            SearchResult(
                doc_id=doc_id,
                score=score,
                title=get(doc_id).title,
                snippet=self._snippet(get(doc_id), parsed.terms, parsed.phrases),
                doc_type=get(doc_id).doc_type,
            )
            for doc_id, score in ranked[offset:offset + top_k]
        ]
        return SearchPage(total=len(ranked), results=results)

    def search(self, query: str, top_k: int = 10) -> list[SearchResult]:
        return self.search_page(query, top_k=top_k).results