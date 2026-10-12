"""P7-5 — the RAG pipeline: retrieval → egress filter → gate → context
selection → prompt → provider → parse → verify → sanitize → respond.

Failure discipline:
- retrieval error        -> RagServiceUnavailable (the API maps to a clean
                           503; internals never reach a client);
- provider timeout/error -> DEGRADE to a retrieval-only response
                           (answer: null, sources attached, degraded_reason)
                           — never a 500 with internals;
- gate refusal           -> first-class response BEFORE any LLM spend
                           (reason code, best sources found, raw signals);
- all-citations-failed   -> the answer is refused outright
                           (unverifiable_citations) — invented sources
                           never ship.

Every response field below is safe to serialize: raw gate SIGNALS and
counts only — never prompts, nonces, keys or provider bodies.
"""
import logging
import time
from dataclasses import dataclass, field

from .config import RagConfig
from .citations import check_support, verify_citations
from .context import select_context
from .egress import query_hash
from .gate import evaluate_gate
from .metrics import RagMetrics, global_metrics
from .prompt import PROMPT_VERSION, SYSTEM_PROMPT, build_prompt
from .provider import LLMProvider, LLMResult, ProviderError, ProviderTimeout
from .retrieval import RagRetriever
from .sanitize import NoopModerator, sanitize_text, scrub_leaks

logger = logging.getLogger("nexus_search.rag.pipeline")

REFUSAL_RETRIEVAL_UNAVAILABLE = "retrieval_unavailable"
REFUSAL_UNVERIFIABLE_CITATIONS = "unverifiable_citations"
DEGRADED_PROVIDER_TIMEOUT = "provider_timeout"
DEGRADED_PROVIDER_ERROR = "provider_error"


class RagServiceUnavailable(Exception):
    """Retrieval failed — mapped to a clean 503 by the API (no internals)."""


@dataclass
class RagResponse:
    answer: str | None
    answerable: bool
    refusal_reason: str | None = None
    degraded_reason: str | None = None
    citations: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "answer": self.answer,
            "answerable": self.answerable,
            "refusal_reason": self.refusal_reason,
            "degraded_reason": self.degraded_reason,
            "citations": [vars(c) for c in self.citations],
            "sources": self.sources,
            "metadata": self.metadata,
        }


def _source_out(source, storage=None) -> dict:
    """The serializable source row for a selected context source."""
    return {
        "n": source.number,
        "doc_id": source.parent_id,
        "parent_id": source.parent_id,
        "chunk_id": source.chunk_id,
        "chunk_index": source.chunk_index,
        "title": source.title,
        "url": source.url,
        "snippet": (source.text or "")[:200],
    }


class RagService:
    """Wires the shared retriever, the configured provider, the calibrated
    gate and the verification/sanitization layers into one ask() call."""

    def __init__(self, config: RagConfig, retriever: RagRetriever,
                 provider: LLMProvider, storage, *,
                 metrics: RagMetrics = None,
                 moderator=None,
                 neighbor_fetcher=None):
        self.config = config
        self.retriever = retriever
        self.provider = provider
        self.storage = storage
        self.metrics = metrics or global_metrics()
        self.moderator = moderator or NoopModerator()
        self._neighbor_fetcher = neighbor_fetcher

    # ---------------------------------------------------------------- ask

    def ask(self, query: str, *, filters: dict | None = None,
            top_k: int = 10, mode: str = "hybrid",
            history: str | None = None,
            request_id: str | None = None) -> RagResponse:
        started = time.perf_counter()
        self.metrics.record_request()
        if self.config.log_queries:
            logger.info("rag request %s query_hash=%s",
                        request_id or "-", query_hash(query))

        # 1. retrieval (shared HybridSearch; error -> clean 503 upstream)
        retrieval = self.retriever.retrieve(query, filters=filters,
                                             k=top_k, mode=mode)
        if retrieval.retrieval_error is not None:
            self.metrics.record_degraded()
            raise RagServiceUnavailable()

        # 2. gate — decide BEFORE any LLM spend
        gate = evaluate_gate(retrieval.results, self.retriever.embedder_name)
        retrieval_meta = {
            "results": len(retrieval.results),
            "total": retrieval.total,
            "mode": retrieval.mode,
            "reranked": retrieval.reranked,
            "language": retrieval.language,
            "retrieval_degraded": retrieval.degraded_reason,
            "gate_signals": gate.signals,
        }

        if retrieval.degraded_reason:
            self.metrics.record_degraded()

        if not gate.answerable:
            self.metrics.record_refusal(gate.refusal_reason)
            return RagResponse(
                answer=None, answerable=False,
                refusal_reason=gate.refusal_reason,
                sources=[_source_out(s) for s in
                         _sources_for_refusal(retrieval.results)],
                metadata=self._meta(started, retrieval_meta,
                                    spend={"tokens_in": 0, "tokens_out": 0,
                                           "estimated_cost": None}))

        # 3. context selection (budgets; llm_allowed egress filter)
        selection = select_context(
            retrieval.results,
            max_context_tokens=self.config.max_context_tokens,
            max_context_bytes=self.config.max_context_bytes,
            chars_per_token=self.config.chars_per_token,
            per_parent_cap=self.config.per_parent_cap,
            dedup_threshold=self.config.dedup_threshold,
            stitch_neighbors=self.config.stitch_neighbors,
            fetch_neighbors=self._neighbor_fetcher,
        )

        # 4. prompt (fencing + injection stripping + optional redaction)
        prompt = build_prompt(query, selection, history=history,
                              redact_pii_enabled=self.config.redact_pii)
        redaction_total = prompt.redaction_total()
        if redaction_total:
            self.metrics.record_redactions(redaction_total)

        # 5. provider
        try:
            result = self.provider.complete(
                prompt.messages, max_tokens=self.config.max_tokens)
        except ProviderTimeout:
            self.metrics.record_provider_error("provider_timeout")
            return self._degraded(DEGRADED_PROVIDER_TIMEOUT, retrieval,
                                  selection, retrieval_meta, started)
        except ProviderError as exc:
            self.metrics.record_provider_error(exc.code)
            return self._degraded(DEGRADED_PROVIDER_ERROR, retrieval,
                                  selection, retrieval_meta, started)

        # 6. parse + verify citations (existing Storage path)
        raw_answer = result.text or ""
        verification = verify_citations(raw_answer, selection, self.storage)
        if verification.all_failed:
            # EVERY citation failed -> refuse outright, never ship it
            self.metrics.record_refusal(REFUSAL_UNVERIFIABLE_CITATIONS)
            self.metrics.record_citation_failure(
                len(verification.dropped))
            return RagResponse(
                answer=None, answerable=False,
                refusal_reason=REFUSAL_UNVERIFIABLE_CITATIONS,
                sources=[_source_out(s) for s in selection.sources],
                metadata=self._meta(
                    started, retrieval_meta,
                    spend=self._spend(result),
                    extra={"citations_verified": False,
                           "citation_drop_reasons": verification.dropped,
                           "redactions": prompt.redactions,
                           "prompt_version": prompt.prompt_version}))

        # 7. support + quote checks (deterministic, no extra LLM call)
        support = check_support(raw_answer, selection)
        if support.unsupported_claims:
            self.metrics.record_unsupported_claim(len(support.unsupported_claims))
        if any(not qc.verified for qc in support.quote_checks):
            self.metrics.record_citation_failure(
                sum(1 for qc in support.quote_checks if not qc.verified))

        # 8. sanitize (plain text; exfil/markup channels removed; echoed
        #    system prompt / frame markers / nonce scrubbed — a prompt
        #    leak must never reach a response field)
        answer, removed = sanitize_text(raw_answer)
        answer, leaked = scrub_leaks(answer, SYSTEM_PROMPT, prompt.nonce)
        removed += leaked
        moderated = self.moderator.moderate(answer)
        if moderated is not None:
            answer, removed_more = sanitize_text(moderated)
            answer, leaked_more = scrub_leaks(answer, SYSTEM_PROMPT,
                                              prompt.nonce)
            removed += removed_more + leaked_more

        # moderation hook may rewrite; citations were verified against the
        # pre-sanitized text and [n] markers survive sanitization untouched
        metadata = self._meta(
            started, retrieval_meta, spend=self._spend(result),
            extra={
                "citations_verified": verification.citations_verified,
                "citation_drop_reasons": verification.dropped,
                "unsupported_claims": [
                    {"citations": u["citations"], "reason": u["reason"],
                     "support_score": u["support_score"]}
                    for u in support.unsupported_claims],
                "quote_checks": [
                    {"verified": qc.verified, "cited": qc.cited_numbers,
                     "offsets": qc.answer_offset} for qc in support.quote_checks],
                "uncited_sentences": support.uncited_sentences,
                "redactions": prompt.redactions,
                "sanitizer_removals": removed,
                "prompt_version": prompt.prompt_version,
            })

        return RagResponse(
            answer=answer, answerable=True,
            citations=verification.citations,
            sources=[_source_out(s) for s in selection.sources],
            metadata=metadata)

    # ------------------------------------------------------------ helpers

    def _spend(self, result: LLMResult) -> dict:
        cost = self.config.estimate_cost(result.usage.tokens_in,
                                         result.usage.tokens_out)
        self.metrics.record_usage(result.usage.tokens_in,
                                   result.usage.tokens_out, cost)
        return {"tokens_in": result.usage.tokens_in,
                "tokens_out": result.usage.tokens_out,
                "estimated_cost": cost,
                "provider": result.provider,
                "model": result.model}

    def _meta(self, started: float, retrieval_meta: dict, *,
              spend: dict, extra: dict | None = None) -> dict:
        meta = {
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "prompt_version": PROMPT_VERSION,
            "retrieval": retrieval_meta,
            **spend,
        }
        if extra:
            meta.update(extra)
        return meta

    def _degraded(self, reason, retrieval, selection, retrieval_meta,
                  started) -> RagResponse:
        return RagResponse(
            answer=None, answerable=True,  # the evidence exists; the LLM
            degraded_reason=reason,        # answer does not
            sources=[_source_out(s) for s in
                     (selection.sources if selection.sources
                      else _sources_for_refusal(retrieval.results))],
            metadata=self._meta(started, retrieval_meta,
                                spend={"tokens_in": 0, "tokens_out": 0,
                                       "estimated_cost": None,
                                       "provider": self.provider.name,
                                       "model": self.provider.model}))


def _sources_for_refusal(results, limit: int = 5):
    """The best sources found, for refusal responses (the user can inspect
    them). Pairs with the context dataclass: light adapter so refusal
    responses use the same row shape as selection.sources."""

    class _S:
        def __init__(self, r, n):
            self.number = n
            self.chunk_id = r.chunk_id
            self.parent_id = r.parent_id
            self.chunk_index = r.chunk_index
            self.title = r.title
            self.url = r.url
            self.text = r.text

    return [_S(r, i + 1) for i, r in enumerate(results[:limit])]
