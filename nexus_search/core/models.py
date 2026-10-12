"""Pydantic request/response models for the FastAPI layer."""
import json
from typing import Optional
from pydantic import BaseModel, Field, field_validator


MAX_CONTENT_CHARS = 10_000_000  # ~10MB of text: attacker input must be bounded
# metadata is arbitrary client JSON smuggled alongside content — bound it too
MAX_METADATA_CHARS = 100_000


class DocumentIn(BaseModel):
    doc_id: str = Field(min_length=1, max_length=512)
    content: str = Field(max_length=MAX_CONTENT_CHARS)
    title: str = Field(default="", max_length=10_000)
    doc_type: str = Field(default="text", max_length=64)
    metadata: dict = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def _metadata_bounded(cls, v: dict) -> dict:
        if len(json.dumps(v, default=str)) > MAX_METADATA_CHARS:
            raise ValueError(f"metadata exceeds {MAX_METADATA_CHARS} serialized chars")
        return v


class BulkDocumentIn(BaseModel):
    # per-document bound applies through nested DocumentIn validation
    documents: list[DocumentIn] = Field(min_length=1, max_length=500)


class SearchResultOut(BaseModel):
    doc_id: str
    score: float
    title: str
    snippet: str
    doc_type: str
    metadata: dict = Field(default_factory=dict)
    chunk_id: Optional[str] = None
    matched_chunks: int = 1
    bm25_score: Optional[float] = None
    vector_score: Optional[float] = None
    source: Optional[str] = None
    # Only populated when the request asked for debug info: each retriever's
    # contribution to the final fused score (they sum to `score`).
    bm25_normalized: Optional[float] = None
    vector_normalized: Optional[float] = None


class SearchMetadata(BaseModel):
    mode: str = "keyword"
    requested_mode: Optional[str] = None  # what the client asked for
    mode_used: Optional[str] = None       # what actually ran (differs on fallback)
    reranked: bool = False                # Phase 5 re-ranker applied?
    bm25_candidates: int = 0
    vector_candidates: int = 0
    merged_candidates: int = 0
    fallback: bool = False
    fallback_reason: Optional[str] = None
    latency_ms: int = 0
    fusion: Optional[str] = None
    sort: Optional[str] = None
    has_more: bool = False       # more results beyond this page? (Stage 4)
    next_cursor: Optional[str] = None  # opaque cursor for the next page (Stage 4)
    facets_truncated: bool = False  # facet pass sampled a >500-doc population?


class DocumentOut(BaseModel):
    doc_id: str
    title: str
    content: str
    doc_type: str
    length: int = 0
    metadata: dict = Field(default_factory=dict)
    added_at: float = 0.0


class SearchResponse(BaseModel):
    query: str
    total_results: int
    offset: int = 0
    top_k: int = 10
    results: list[SearchResultOut]
    metadata: Optional[SearchMetadata] = None
    facets: Optional[dict] = None  # field -> {value: count}; set when facets=... given


class ExplainRequest(BaseModel):
    query: str
    top_k: int = 10
    mode: str = "hybrid"
    fusion: str = "rrf"
    bm25_weight: float = Field(default=1.0, ge=0)
    vector_weight: float = Field(default=1.0, ge=0)
    rerank: bool = False  # also surface Stage-2 reranker features per result


class ExplainResult(BaseModel):
    doc_id: str
    final_score: float
    bm25_score: Optional[float] = None
    vector_score: Optional[float] = None
    bm25_normalized: Optional[float] = None
    vector_normalized: Optional[float] = None
    source: str
    title: str
    chunk_id: Optional[str] = None
    # when rerank=true: raw Stage-2 signal values + model-blended score
    features: Optional[dict] = None
    reranked_score: Optional[float] = None


class ExplainResponse(BaseModel):
    query: str
    mode: str
    metadata: SearchMetadata
    results: list[ExplainResult]


# ---------------------------------------------------------------------------
# Phase 7 — RAG /ask (P7-6). Bounded fields everywhere: attacker input
# is capped BEFORE any LLM spend, each with a clear 4xx.
# ---------------------------------------------------------------------------

MAX_ASK_QUERY_CHARS = 2000    # same as /search (attacker cost parity)
MAX_ASK_QUERY_WORDS = 128     # same as /search
MAX_ASK_FILTER_KEYS = 8       # a filter dict has exactly 2 legal keys; 8 is the hard bound
MAX_SESSION_ID_CHARS = 64
SESSION_ID_CHARSET = r"^[A-Za-z0-9._:-]{1,64}$"


class AskRequest(BaseModel):
    """POST /ask body. `session_id` is client-supplied (untrusted, bounded);
    its conversation semantics land with P7-8 — the field is validated
    now so the contract is stable."""
    query: str = Field(min_length=1, max_length=MAX_ASK_QUERY_CHARS)
    filters: Optional[dict] = Field(default=None)
    top_k: int = Field(default=10, ge=1, le=100)
    mode: str = Field(default="hybrid", pattern="^(keyword|semantic|hybrid)$")
    session_id: Optional[str] = Field(default=None,
                                      pattern=SESSION_ID_CHARSET)

    @field_validator("filters")
    @classmethod
    def _filters_bounded(cls, v: Optional[dict]) -> Optional[dict]:
        if v is None:
            return v
        if len(v) > MAX_ASK_FILTER_KEYS:
            raise ValueError(f"filters accepts at most {MAX_ASK_FILTER_KEYS} keys")
        return v

    @field_validator("query")
    @classmethod
    def _query_words_bounded(cls, v: str) -> str:
        if len(v.split()) > MAX_ASK_QUERY_WORDS:
            raise ValueError(f"query has more than {MAX_ASK_QUERY_WORDS} words")
        return v

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"query": "how does bm25 rank documents",
                 "top_k": 5, "mode": "hybrid"},
                {"query": "what powers the crawler politeness",
                 "filters": {"doc_type": "web"}, "top_k": 3},
            ]
        }
    }


class AskSource(BaseModel):
    n: int
    doc_id: str
    parent_id: str
    chunk_id: str
    chunk_index: Optional[int] = None
    title: str = ""
    url: Optional[str] = None
    snippet: str = ""


class AskCitation(AskSource):
    status: str = "verified"


class AskMetadata(BaseModel):
    """Numbers and codes only — prompts, nonces and keys are structurally
    excluded (the service builds it from typed fields)."""
    model_config = {"extra": "allow"}

    latency_ms: int = 0
    prompt_version: str = ""
    retrieval: dict = Field(default_factory=dict)
    tokens_in: int = 0
    tokens_out: int = 0
    estimated_cost: Optional[float] = None
    provider: str = ""
    model: str = ""
    citations_verified: Optional[bool] = None
    citation_drop_reasons: list = Field(default_factory=list)
    unsupported_claims: list = Field(default_factory=list)
    quote_checks: list = Field(default_factory=list)
    uncited_sentences: Optional[int] = None
    redactions: list = Field(default_factory=list)
    sanitizer_removals: Optional[int] = None


class AskResponse(BaseModel):
    request_id: str
    answer: Optional[str] = None
    answerable: bool
    refusal_reason: Optional[str] = None
    degraded_reason: Optional[str] = None
    citations: list[AskCitation] = Field(default_factory=list)
    sources: list[AskSource] = Field(default_factory=list)
    metadata: AskMetadata = Field(default_factory=AskMetadata)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"request_id": "a1b2c3d4",
                 "answer": "BM25 scores lexical matches over an inverted "
                           "index [1].",
                 "answerable": True,
                 "citations": [{"n": 1, "doc_id": "bm25-doc",
                                "parent_id": "bm25-doc",
                                "chunk_id": "bm25-doc",
                                "chunk_index": 0, "title": "BM25",
                                "url": None,
                                "snippet": "BM25 scores lexical matches...",
                                "status": "verified"}],
                 "sources": [{"n": 1, "doc_id": "bm25-doc",
                              "parent_id": "bm25-doc",
                              "chunk_id": "bm25-doc",
                              "chunk_index": 0, "title": "BM25",
                              "url": None,
                              "snippet": "BM25 scores lexical matches..."}],
                 "metadata": {"latency_ms": 12, "prompt_version": "rag-prompt-v1",
                              "tokens_in": 210, "tokens_out": 18,
                              "estimated_cost": None, "provider": "stub",
                              "model": "stub-deterministic-1",
                              "citations_verified": True}},
                {"request_id": "e5f6a7b8",
                 "answer": None, "answerable": False,
                 "refusal_reason": "no_evidence",
                 "citations": [], "sources": []},
            ]
        }
    }