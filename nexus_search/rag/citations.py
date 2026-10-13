"""P7-5  citation parsing, verification, support and quote checks.

Every `[n]` marker in an answer must map to a source number of THIS
request's context selection; citations resolve to exact stored rows
(chunk_id -> Storage row) through the EXISTING batched Storage path.
Unverifiable citations are DROPPED and the answer is flagged with the
failure reasons; an answer whose EVERY citation fails is refused
outright (never shipped with invented sources).

Support checks (hallucination reduction, deterministic  NO extra LLM
call): each cited answer sentence gets a lexical-overlap support score
against the text of its cited source(s) AS SENT (post-redaction, so
redacted prompts still verify); sentences below the threshold are
listed in `unsupported_claims`. Quote verification: every quoted span in
the answer must appear in the cited source; verified quotes return
character offsets, failed quotes go to `unsupported_claims`.

HONEST LIMIT: these are LEXICAL checks, not semantic entailment  a
restated-but-true sentence scores lower than a verbatim copy, and a
paraphrased lie can pass. The P7-9 benchmark measures the residual gap.
"""
import re
from dataclasses import dataclass, field

from ..core.tokenizer import tokenize

# Lexical support threshold: fraction of a sentence's content tokens that
# must appear in the cited source text. Calibrated on the Phase 7 stub
# battery (verbatim extractive sentences score ~1.0; fabricated sentences
# score < 0.2; 0.4 sits in the measured gap).
SUPPORT_THRESHOLD = 0.4

_CITATION_RE = re.compile(r"\[(\d{1,3})\]")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_QUOTE_RE = re.compile("[\"]([^\"]{2,400})[\"]")

VERIFIED = "verified"
DROPPED_UNKNOWN_NUMBER = "unknown_source_number"
DROPPED_MISSING_ROW = "source_row_missing"


@dataclass
class Citation:
    n: int
    doc_id: str            # parent_id (the document)
    parent_id: str
    chunk_id: str          # the exact stored row id
    chunk_index: int | None
    title: str
    url: str | None
    snippet: str
    status: str = VERIFIED


@dataclass
class CitationVerification:
    citations: list[Citation] = field(default_factory=list)
    citations_verified: bool = True
    dropped: list[dict] = field(default_factory=list)  # {n, reason}
    all_failed: bool = False


@dataclass
class QuoteCheck:
    quote: str
    cited_numbers: list[int]
    verified: bool
    answer_offset: tuple[int, int] | None = None  # char span in the answer


@dataclass
class SupportReport:
    unsupported_claims: list[dict] = field(default_factory=list)
    quote_checks: list[QuoteCheck] = field(default_factory=list)
    scored_sentences: int = 0
    uncited_sentences: int = 0


def parse_citations(answer_text: str) -> list[int]:
    """Unique [n] markers in order of first appearance."""
    seen: dict[int, None] = {}
    for match in _CITATION_RE.finditer(answer_text or ""):
        seen.setdefault(int(match.group(1)), None)
    return list(seen)


def _snippet_of(text: str, width: int = 200) -> str:
    text = (text or "").strip()
    if len(text) <= width:
        return text
    return text[:width].rstrip() + "..."


def verify_citations(answer_text: str, selection, storage) -> CitationVerification:
    """Parse + verify [n] citations against THIS request's numbered
    sources. Chunk rows are re-resolved through the EXISTING batched
    Storage.get_documents path (fresh read: a row deleted mid-request is
    caught, and the returned snippet/title come from the stored row)."""
    out = CitationVerification()
    numbers = parse_citations(answer_text)
    if not numbers:
        out.all_failed = False
        return out

    by_number = {s.number: s for s in selection.sources}
    rows = {}
    to_resolve = []
    for n in numbers:
        source = by_number.get(n)
        if source is None:
            out.dropped.append({"n": n, "reason": DROPPED_UNKNOWN_NUMBER})
        else:
            to_resolve.append(source.chunk_id)
    # ONE batched fetch for every cited row (the existing path)
    rows = storage.get_documents(to_resolve) if to_resolve else {}

    verified_count = 0
    for n in numbers:
        source = by_number.get(n)
        if source is None:
            continue
        row = rows.get(source.chunk_id)
        if row is None:
            out.dropped.append({"n": n, "reason": DROPPED_MISSING_ROW})
            continue
        out.citations.append(Citation(
            n=n, doc_id=source.parent_id, parent_id=source.parent_id,
            chunk_id=source.chunk_id, chunk_index=source.chunk_index,
            title=row.title or source.title,
            url=source.url,
            snippet=_snippet_of(row.content)))
        verified_count += 1

    out.citations_verified = not out.dropped
    # EVERY citation failed -> the answer is refused outright
    out.all_failed = verified_count == 0 and bool(numbers)
    return out


def _sentence_tokens(sentence: str) -> list[str]:
    return [t for t in tokenize(sentence) if len(t) > 2]


def sentence_support(sentence: str, source_texts: list[str]) -> float:
    """Fraction of the sentence's content tokens present in the union of
    the cited source texts (AS SENT)."""
    tokens = _sentence_tokens(sentence)
    if not tokens or not source_texts:
        return 0.0
    union_tokens: set[str] = set()
    for text in source_texts:
        union_tokens.update(t for t in tokenize(text) if len(t) > 2)
    if not union_tokens:
        return 0.0
    hits = sum(1 for t in tokens if t in union_tokens)
    return hits / len(tokens)


def _split_sentences(answer_text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_RE.split(answer_text or "")
            if s and s.strip()]


def _normalize_ws(text: str) -> str:
    return " ".join((text or "").split())


def check_quotes(answer_text: str, selection,
                 as_sent: dict[str, str] | None = None) -> list[QuoteCheck]:
    """Every quoted span must appear (whitespace-normalized) in the cited
    source(s) of the sentence it sits in — checked against the text AS
    SENT (post-redaction contract); verified quotes carry char offsets
    into the answer text."""
    checks: list[QuoteCheck] = []
    source_by_number = {s.number: s for s in selection.sources}

    def _text_of(n: int) -> str:
        source = source_by_number[n]
        if as_sent is not None and source.chunk_id in as_sent:
            return as_sent[source.chunk_id]
        return source.text

    for match in _QUOTE_RE.finditer(answer_text or ""):
        quote = match.group(1)
        cited = parse_citations(match.group(0))
        if not cited:
            # look for citations in the sentence containing the quote
            start = answer_text.rfind(".", 0, match.start())
            end = answer_text.find(".", match.end())
            window = answer_text[max(start, 0): end + 1 if end != -1 else None]
            cited = parse_citations(window)
        texts = [_text_of(n) for n in cited if n in source_by_number]
        normalized_quote = _normalize_ws(quote)
        verified = any(normalized_quote in _normalize_ws(t) for t in texts)
        checks.append(QuoteCheck(
            quote=quote, cited_numbers=cited, verified=verified,
            answer_offset=(match.start(1), match.end(1)) if verified else None))
    return checks


def check_support(answer_text: str, selection,
                  support_threshold: float = SUPPORT_THRESHOLD,
                  check_quotes_too: bool = True,
                  as_sent: dict[str, str] | None = None) -> SupportReport:
    """Per-cited-sentence lexical support + quote verification, against
    the cited sources' text AS SENT (post-redaction — what the model
    actually saw)."""
    report = SupportReport()
    by_number = {s.number: s for s in selection.sources}

    def _text_of(n: int) -> str:
        source = by_number[n]
        if as_sent is not None and source.chunk_id in as_sent:
            return as_sent[source.chunk_id]
        return source.text

    for sentence in _split_sentences(answer_text or ""):
        if not _sentence_tokens(sentence):
            continue  # bare markers/punctuation carry nothing to assess
        cited = parse_citations(sentence)
        if not cited:
            report.uncited_sentences += 1
            continue
        texts = [_text_of(n) for n in cited if n in by_number]
        score = sentence_support(sentence, texts)
        report.scored_sentences += 1
        if score < support_threshold:
            report.unsupported_claims.append({
                "sentence": sentence,
                "citations": cited,
                "support_score": round(score, 3),
                "reason": "low_lexical_support",
            })
    if check_quotes_too:
        for qc in check_quotes(answer_text, selection, as_sent=as_sent):
            report.quote_checks.append(qc)
            if not qc.verified:
                report.unsupported_claims.append({
                    "sentence": f'"{qc.quote}"',
                    "citations": qc.cited_numbers,
                    "support_score": 0.0,
                    "reason": "quote_not_in_source",
                })
    return report

