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
from .session import SessionCapExceeded, heuristic_condense

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
                 neighbor_fetcher=None,
                 session_store=None):
        self.config = config
        self.retriever = retriever
        self.provider = provider
        self.storage = storage
        self.metrics = metrics or global_metrics()
        self.moderator = moderator or NoopModerator()
        self._neighbor_fetcher = neighbor_fetcher
        self.sessions = session_store

    # ------------------------------------------------------------ sessions

    def _prepare_session(self, query: str, session_id, scope):
        """Load session history + condense the follow-up. Returns
        (standalone_query, history_text, session_or_None, extra_spend).
        Raises SessionExpired (clear 4xx upstream)."""
        extra = {"tokens_in": 0, "tokens_out": 0}
        if not session_id or self.sessions is None:
            return query, None, None, extra
        history = self.sessions.get(scope, session_id)
        history_text = (history.history_text(self.config.session_max_turns)
                        if history else None)
        if not history_text:
            return query, None, history, extra
        mode = self.config.followup_condense
        if mode == "off":
            return query, history_text, history, extra
        if mode == "llm":
            try:
                result = self.provider.complete(
                    _condense_messages(history_text, query),
                    max_tokens=min(self.config.max_tokens, 256),
                    timeout=self.config.timeout)
                rewritten = (result.text or "").strip()
                extra["tokens_in"] += result.usage.tokens_in
                extra["tokens_out"] += result.usage.tokens_out
                if rewritten and len(rewritten) < 1000:
                    return rewritten, history_text, history, extra
            except ProviderError:
                pass  # deterministic fallback — never fail over condensing
        return (heuristic_condense(query, history_text), history_text,
                history, extra)

    def _session_append(self, session_id, scope, role: str, text: str,
                        pre_llm: bool) -> None:
        """Record a turn. Pre-LLM appends enforce the caps (clear 4xx);
        post-LLM appends only log (a cap hit after spend is logged, never
        surfaced as an error the user already paid for)."""
        if not session_id or self.sessions is None:
            return
        try:
            self.sessions.append(scope, session_id, role, text)
        except SessionCapExceeded as exc:
            if pre_llm:
                raise
            logger.warning("session %s post-answer turn not stored (%s)",
                           session_id, exc)

    # ---------------------------------------------------------------- ask

    def ask(self, query: str, *, filters: dict | None = None,
            top_k: int = 10, mode: str = "hybrid",
            history: str | None = None,
            session_id: str | None = None, scope: str | None = None,
            request_id: str | None = None) -> RagResponse:
        started = time.perf_counter()
        self.metrics.record_request()
        if self.config.log_queries:
            logger.info("rag request %s query_hash=%s",
                        request_id or "-", query_hash(query))

        # 0. sessions (P7-8): condense the follow-up, enforce caps BEFORE
        # any spend, then run retrieval on the standalone query
        standalone, history_text, session_obj, condense_spend = \
            self._prepare_session(query, session_id, scope)
        if session_id and self.sessions is not None:
            turns = len(session_obj.turns) if session_obj else 0
            if turns + 2 > self.config.session_max_turns:
                raise SessionCapExceeded(f"turns:{turns + 2}")
            self._session_append(session_id, scope, "user", query,
                                 pre_llm=True)

        # 1. retrieval (shared HybridSearch; error -> clean 503 upstream)
        retrieval = self.retriever.retrieve(standalone, filters=filters,
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
            self._session_append(session_id, scope, "assistant",
                                 f"(no answer: {gate.refusal_reason})",
                                 pre_llm=False)
            return RagResponse(
                answer=None, answerable=False,
                refusal_reason=gate.refusal_reason,
                sources=[_source_out(s) for s in
                         _sources_for_refusal(retrieval.results)],
                metadata=self._meta(started, retrieval_meta,
                                    spend={"tokens_in": condense_spend["tokens_in"],
                                           "tokens_out": condense_spend["tokens_out"],
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
        prompt = build_prompt(query, selection, history=history_text,
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
            self._session_append(session_id, scope, "assistant",
                                 f"(no answer: {DEGRADED_PROVIDER_TIMEOUT})",
                                 pre_llm=False)
            return self._degraded(DEGRADED_PROVIDER_TIMEOUT, retrieval,
                                  selection, retrieval_meta, started,
                                  condense_spend=condense_spend)
        except ProviderError as exc:
            self.metrics.record_provider_error(exc.code)
            self._session_append(session_id, scope, "assistant",
                                 f"(no answer: {DEGRADED_PROVIDER_ERROR})",
                                 pre_llm=False)
            return self._degraded(DEGRADED_PROVIDER_ERROR, retrieval,
                                  selection, retrieval_meta, started,
                                  condense_spend=condense_spend)

        # 6. parse + verify citations (existing Storage path)
        raw_answer = result.text or ""
        verification = verify_citations(raw_answer, selection, self.storage)
        if verification.all_failed:
            # EVERY citation failed -> refuse outright, never ship it
            self.metrics.record_refusal(REFUSAL_UNVERIFIABLE_CITATIONS)
            self.metrics.record_citation_failure(
                len(verification.dropped))
            self._session_append(session_id, scope, "assistant",
                                 f"(no answer: {REFUSAL_UNVERIFIABLE_CITATIONS})",
                                 pre_llm=False)
            return RagResponse(
                answer=None, answerable=False,
                refusal_reason=REFUSAL_UNVERIFIABLE_CITATIONS,
                sources=[_source_out(s) for s in selection.sources],
                metadata=self._meta(
                    started, retrieval_meta,
                    spend={**self._spend(result, condense_spend),
                           "condense_tokens_in": condense_spend["tokens_in"],
                           "condense_tokens_out": condense_spend["tokens_out"]},
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
            started, retrieval_meta,
            spend={**self._spend(result, condense_spend),
                   "condense_tokens_in": condense_spend["tokens_in"],
                   "condense_tokens_out": condense_spend["tokens_out"]},
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

        self._session_append(session_id, scope, "assistant", answer,
                             pre_llm=False)
        return RagResponse(
            answer=answer, answerable=True,
            citations=verification.citations,
            sources=[_source_out(s) for s in selection.sources],
            metadata=metadata)

    # ------------------------------------------------------------ helpers

    # ---------------------------------------------------------- streaming

    def ask_streaming(self, query: str, *, filters: dict | None = None,
                      top_k: int = 10, mode: str = "hybrid",
                      history: str | None = None,
                      session_id: str | None = None, scope: str | None = None,
                      request_id: str | None = None):
        """P7-7: yield typed SSE events for /ask/stream.

        Event order: meta (retrieval + gate, BEFORE any token; a refusal
        streams no tokens) -> token* -> citations (authoritative, at
        stream end) -> done. Failures mid-stream: truncated (LLM
        timeout, never a silent hang) or error (provider failure ->
        sources still attached), each followed by done.

        The yielded `token` payloads are SANITIZED with a StreamSanitizer
        (split-token payloads caught); citation/quote verification runs
        at stream end on the final text. Budget settling is the CALLER's
        duty (it owns the bucket): the last event's `spend` field carries
        the usage to settle — on disconnect the caller settles nothing
        (the pre-check reserved no spend; documented)."""
        import threading
        from .sanitize import StreamSanitizer

        started = time.perf_counter()
        self.metrics.record_request()
        if self.config.log_queries:
            logger.info("rag stream request %s query_hash=%s",
                        request_id or "-", query_hash(query))

        standalone, history_text, session_obj, condense_spend = \
            self._prepare_session(query, session_id, scope)
        if session_id and self.sessions is not None:
            turns = len(session_obj.turns) if session_obj else 0
            if turns + 2 > self.config.session_max_turns:
                raise SessionCapExceeded(f"turns:{turns + 2}")
            self._session_append(session_id, scope, "user", query,
                                 pre_llm=True)

        retrieval = self.retriever.retrieve(standalone, filters=filters,
                                             k=top_k, mode=mode)
        if retrieval.retrieval_error is not None:
            self.metrics.record_degraded()
            raise RagServiceUnavailable()

        gate = evaluate_gate(retrieval.results, self.retriever.embedder_name)
        retrieval_meta = {
            "results": len(retrieval.results), "total": retrieval.total,
            "mode": retrieval.mode, "reranked": retrieval.reranked,
            "language": retrieval.language,
            "retrieval_degraded": retrieval.degraded_reason,
            "gate_signals": gate.signals,
        }
        if retrieval.degraded_reason:
            self.metrics.record_degraded()

        base_meta = {
            "request_id": request_id,
            "retrieval": retrieval_meta,
            "prompt_version": PROMPT_VERSION,
            "provider": self.provider.name,
            "model": self.provider.model,
        }

        if not gate.answerable:
            self.metrics.record_refusal(gate.refusal_reason)
            yield {"event": "meta", "data": {**base_meta,
                                             "answerable": False,
                                             "refusal_reason": gate.refusal_reason}}
            yield {"event": "done", "data": {
                "spend": {"tokens_in": 0, "tokens_out": 0,
                          "estimated_cost": None},
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "sources": [_source_out(s) for s in
                            _sources_for_refusal(retrieval.results)],
                "refusal_reason": gate.refusal_reason, "answerable": False}}
            return

        selection = select_context(
            retrieval.results,
            max_context_tokens=self.config.max_context_tokens,
            max_context_bytes=self.config.max_context_bytes,
            chars_per_token=self.config.chars_per_token,
            per_parent_cap=self.config.per_parent_cap,
            dedup_threshold=self.config.dedup_threshold,
            stitch_neighbors=self.config.stitch_neighbors,
            fetch_neighbors=self._neighbor_fetcher)
        prompt = build_prompt(standalone, selection, history=history_text,
                              redact_pii_enabled=self.config.redact_pii)
        if prompt.redaction_total():
            self.metrics.record_redactions(prompt.redaction_total())

        # The hard timeout contract: connection establishment must finish
        # within config.timeout (requests enforces it per-socket-read on
        # real adapters; the stub's `slow` behavior sleeps timeout+1 to
        # prove this path fires). After that, tokens must keep arriving:
        # a config.timeout-long GAP between tokens also means truncated.
        box: dict = {}

        def _call():
            try:
                box["stream"] = self.provider.complete(
                    prompt.messages, max_tokens=self.config.max_tokens,
                    timeout=self.config.timeout, stream=True)
            except BaseException as exc:  # noqa: BLE001 - re-raised below
                box["error"] = exc

        worker = threading.Thread(target=_call, daemon=True)
        worker.start()
        worker.join(max(self.config.timeout, 0.0))
        if "stream" not in box:
            self.metrics.record_provider_error("provider_timeout")
            yield {"event": "meta", "data": {**base_meta, "answerable": True}}
            yield {"event": "truncated", "data": {"reason": "provider_timeout"}}
            yield {"event": "done", "data": {
                "spend": {"tokens_in": 0, "tokens_out": 0,
                          "estimated_cost": None},
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "degraded_reason": DEGRADED_PROVIDER_TIMEOUT,
                "sources": [_source_out(s) for s in selection.sources]}}
            return
        if "error" in box:
            exc = box["error"]
            self.metrics.record_provider_error(
                getattr(exc, "code", "provider_error"))
            yield {"event": "meta", "data": {**base_meta, "answerable": True}}
            yield {"event": "error", "data": {
                "reason": DEGRADED_PROVIDER_ERROR if isinstance(exc, ProviderError)
                else "provider_error"}}
            yield {"event": "done", "data": {
                "spend": {"tokens_in": 0, "tokens_out": 0,
                          "estimated_cost": None},
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "degraded_reason": DEGRADED_PROVIDER_ERROR,
                "sources": [_source_out(s) for s in selection.sources]}}
            return

        yield {"event": "meta", "data": {**base_meta, "answerable": True}}

        stream = box["stream"]
        sanitizer = StreamSanitizer()
        final_text_parts: list[str] = []
        last_token_at = time.monotonic()
        try:
            for delta in stream:
                now = time.monotonic()
                if now - last_token_at > self.config.timeout:
                    raise TimeoutError()
                last_token_at = now
                clean = sanitizer.push(delta)
                if clean:
                    final_text_parts.append(clean)
                    yield {"event": "token", "data": {"text": clean}}
            tail = sanitizer.flush()
            if tail:
                final_text_parts.append(tail)
                yield {"event": "token", "data": {"text": tail}}
        except TimeoutError:
            self.metrics.record_provider_error("provider_timeout")
            yield {"event": "truncated", "data": {"reason": "provider_timeout"}}
            yield {"event": "done", "data": {
                "spend": {"tokens_in": 0, "tokens_out": 0,
                          "estimated_cost": None},
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "degraded_reason": DEGRADED_PROVIDER_TIMEOUT,
                "sources": [_source_out(s) for s in selection.sources]}}
            return
        except ProviderError as exc:
            self.metrics.record_provider_error(exc.code)
            yield {"event": "error", "data": {"reason": DEGRADED_PROVIDER_ERROR}}
            yield {"event": "done", "data": {
                "spend": {"tokens_in": 0, "tokens_out": 0,
                          "estimated_cost": None},
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "degraded_reason": DEGRADED_PROVIDER_ERROR,
                "sources": [_source_out(s) for s in selection.sources]}}
            return

        # stream end: the final text is authoritative for verification
        final = stream.result() if hasattr(stream, "result") else None
        raw_text = final.text if final is not None else "".join(final_text_parts)
        answer, removed = sanitize_text(raw_text)
        answer, leaked = scrub_leaks(answer, SYSTEM_PROMPT, prompt.nonce)
        verification = verify_citations(answer, selection, self.storage)
        support = check_support(answer, selection)
        if support.unsupported_claims:
            self.metrics.record_unsupported_claim(len(support.unsupported_claims))
        if final is not None:
            spend = self._spend(final)
        else:
            spend = {"tokens_in": 0, "tokens_out": 0,
                     "estimated_cost": None,
                     "provider": self.provider.name,
                     "model": self.provider.model}

        if verification.all_failed:
            self.metrics.record_refusal(REFUSAL_UNVERIFIABLE_CITATIONS)
            self.metrics.record_citation_failure(len(verification.dropped))
            yield {"event": "citations", "data": {
                "citations": [], "citations_verified": False,
                "citation_drop_reasons": verification.dropped}}
            yield {"event": "done", "data": {
                "spend": spend,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "refusal_reason": REFUSAL_UNVERIFIABLE_CITATIONS,
                "answerable": False,
                "sources": [_source_out(s) for s in selection.sources]}}
            return

        yield {"event": "citations", "data": {
            "citations": [vars(c) for c in verification.citations],
            "citations_verified": verification.citations_verified,
            "citation_drop_reasons": verification.dropped,
            "unsupported_claims": [
                {"citations": u["citations"], "reason": u["reason"],
                 "support_score": u["support_score"]}
                for u in support.unsupported_claims],
            "quote_checks": [
                {"verified": q.verified, "cited": q.cited_numbers,
                 "offsets": q.answer_offset} for q in support.quote_checks],
            "sanitizer_removals": removed + leaked,
            "redactions": prompt.redactions,
        }}
        self._session_append(session_id, scope, "assistant", answer,
                             pre_llm=False)
        yield {"event": "done", "data": {
            "spend": {**spend,
                      "condense_tokens_in": condense_spend["tokens_in"],
                      "condense_tokens_out": condense_spend["tokens_out"]},
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "answer": answer,
            "answerable": True,
            "sources": [_source_out(s) for s in selection.sources]}}

    def _spend(self, result: LLMResult, extra: dict | None = None) -> dict:
        tokens_in = result.usage.tokens_in + (extra or {}).get("tokens_in", 0)
        tokens_out = result.usage.tokens_out + (extra or {}).get("tokens_out", 0)
        cost = self.config.estimate_cost(tokens_in, tokens_out)
        self.metrics.record_usage(tokens_in, tokens_out, cost)
        return {"tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "estimated_cost": cost,
                "provider": result.provider,
                "model": result.model}

    def _meta(self, started: float, retrieval_meta: dict, *,
              spend: dict, extra: dict | None = None) -> dict:
        meta = {
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "prompt_version": PROMPT_VERSION,
            "retrieval": retrieval_meta,
            "condense_tokens_in": spend.get("condense_tokens_in", 0),
            "condense_tokens_out": spend.get("condense_tokens_out", 0),
            **{k: v for k, v in spend.items()
               if not k.startswith("condense_")},
        }
        if extra:
            meta.update(extra)
        return meta

    def _degraded(self, reason, retrieval, selection, retrieval_meta,
                  started, condense_spend: dict | None = None) -> RagResponse:
        c = condense_spend or {}
        return RagResponse(
            answer=None, answerable=True,  # the evidence exists; the LLM
            degraded_reason=reason,        # answer does not
            sources=[_source_out(s) for s in
                     (selection.sources if selection.sources
                      else _sources_for_refusal(retrieval.results))],
            metadata=self._meta(started, retrieval_meta,
                                spend={"tokens_in": c.get("tokens_in", 0),
                                       "tokens_out": c.get("tokens_out", 0),
                                       "estimated_cost": None,
                                       "condense_tokens_in": c.get("tokens_in", 0),
                                       "condense_tokens_out": c.get("tokens_out", 0),
                                       "provider": self.provider.name,
                                       "model": self.provider.model}))


def _condense_messages(history_text: str, query: str) -> list[dict]:
    """Prompt for the LLM follow-up condense step: the transcript and the
    follow-up are DATA in a fenced region; the instruction is fixed."""
    return [
        {"role": "system", "content":
            "Rewrite the user's follow-up question as ONE standalone "
            "question that can be understood without the conversation. "
            "Output ONLY the rewritten question — no preamble, no answer."},
        {"role": "user", "content":
            f"==== NXSRAG-CONDENSE HISTORY ====\n{history_text}\n"
            f"==== NXSRAG-CONDENSE END HISTORY ====\n"
            f"==== NXSRAG-CONDENSE QUESTION ====\n{query}\n"
            f"==== NXSRAG-CONDENSE END QUESTION ===="},
    ]


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
