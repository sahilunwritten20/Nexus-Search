"""BM25 retrieval plus lightweight query understanding and field/phrase boosts."""
import math
from dataclasses import dataclass

from .storage import Storage
from .tokenizer import tokenize
from .query_parser import parse_query

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
    def _snippet(doc, terms: list[str], phrases: list[str], width: int = 200) -> str:
        text = doc.content
        lowered = text.lower()
        positions = [lowered.find(p.lower()) for p in phrases if p]
        positions += [lowered.find(t.lower()) for t in terms if t]
        positions = [p for p in positions if p >= 0]
        if not positions:
            return text[:width] + ("..." if len(text) > width else "")
        pos = min(positions)
        start = max(0, pos - width // 4)
        snippet = text[start:start + width]
        if start > 0:
            snippet = "..." + snippet
        if start + width < len(text):
            snippet += "..."
        return snippet

    def search(self, query: str, top_k: int = 10) -> list[SearchResult]:
        if top_k <= 0:
            return []
        parsed = parse_query(query)
        terms = parsed.terms + [t for phrase in parsed.phrases for t in tokenize(phrase)]
        if not terms:
            return []

        n_docs = self.storage.document_count()
        if n_docs == 0:
            return []
        avg_len = self.storage.average_length()
        scores: dict[str, float] = {}
        doc_lengths: dict[str, int] = {}

        for term in set(terms):
            idf = self._idf(term, n_docs)
            for doc_id, tf in self.storage.postings_for_term(term):
                if doc_id not in doc_lengths:
                    doc = self.storage.get_document(doc_id)
                    if doc is None or not self._matches_filters(doc, parsed.filters):
                        continue
                    doc_lengths[doc_id] = doc.length if doc else avg_len
                dl = doc_lengths[doc_id]
                norm = 1 - self.b + self.b * (dl / avg_len if avg_len else 1)
                denom = tf + self.k1 * norm
                scores[doc_id] = scores.get(doc_id, 0.0) + idf * (tf * (self.k1 + 1)) / denom

        ranked = []
        for doc_id, score in scores.items():
            doc = self.storage.get_document(doc_id)
            if doc is None or not self._matches_filters(doc, parsed.filters):
                continue
            title_terms = set(tokenize(doc.title))
            title_hits = sum(1 for term in set(terms) if term in title_terms)
            score *= 1 + (TITLE_BOOST - 1) * min(title_hits / max(len(set(terms)), 1), 1.0)
            if parsed.phrases:
                content_lower = doc.content.lower()
                title_lower = doc.title.lower()
                phrase_hits = sum(1 for phrase in parsed.phrases if phrase.lower() in content_lower or phrase.lower() in title_lower)
                if phrase_hits:
                    score *= PHRASE_BOOST ** phrase_hits
            ranked.append((doc_id, score))

        ranked.sort(key=lambda kv: (-kv[1], kv[0]))
        results = []
        for doc_id, score in ranked[:top_k]:
            doc = self.storage.get_document(doc_id)
            if doc is None:
                continue
            results.append(SearchResult(
                doc_id=doc_id,
                score=score,
                title=doc.title,
                snippet=self._snippet(doc, parsed.terms, parsed.phrases),
                doc_type=doc.doc_type,
            ))
        return results
