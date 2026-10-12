"""P7-3  the LLM provider layer.

`LLMProvider.complete(messages, *, max_tokens, timeout, stream)` is the
ONLY thing the RAG pipeline may call. Tool-calling is DISABLED BY
CONSTRUCTION: the interface has no `tools`/`functions` parameter, the
adapters never send one, and there is no agent loop (PHASE7_PLAN 2).

Implementations:
- StubProvider      deterministic, offline, with selectable ADVERSARIAL
                    behaviors (extractive / no_answer /
                    hallucinated_citation / obey_injection / markdown_exfil
                    / html_payload / prompt_leak / bad_quote / slow /
                    error). The adversarial modes exist to prove the P7-5
                    defenses work, not to imitate a good model.
- OpenAIProvider    any OpenAI-compatible chat-completions endpoint
                    (NEXUS_LLM_BASE_URL covers local servers), `requests`
                    only  no SDK, no new dependency.
- AnthropicProvider the Anthropic Messages API, same rules.

Discipline:
- hard timeout (<= 30s, config-clamped) on every call;
- bounded retry with jitter (<= 2, connect errors / 429 / 5xx ONLY 
  read timeouts are NOT retried: the call may already have landed);
- concurrency semaphore (default 4); saturation raises ProviderBusy
  (the API maps it to 429 + Retry-After);
- the API key travels ONLY in the auth header to the configured
  endpoint; error paths raise type-coded ProviderError with NO response
  bodies, NO paths, NO key fragments (the poisoned-key test enforces it).
"""
import json
import re
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterator, Optional, Union

import requests

from .config import RagConfig


# ---------------------------------------------------------------------------
# Results / errors
# ---------------------------------------------------------------------------

@dataclass
class LLMUsage:
    tokens_in: int = 0
    tokens_out: int = 0


@dataclass
class LLMResult:
    text: str
    usage: LLMUsage = field(default_factory=LLMUsage)
    model: str = ""
    provider: str = ""


class LLMStream:
    """Streaming accumulator: iterate over text deltas, then `.result()`.

    The RAG layer treats streamed text as provisional until the stream is
    exhausted (P7-7); this class is what makes the final text + usage
    available for citation/quote verification at stream end."""

    def __init__(self, model: str, provider: str):
        self._parts: list[str] = []
        self._cursor = 0
        self._lock = threading.Lock()
        self._usage = LLMUsage()
        self._model = model
        self._provider = provider
        self._done = False

    def add(self, delta: str) -> None:
        with self._lock:
            self._parts.append(delta)

    def set_usage(self, usage: LLMUsage) -> None:
        with self._lock:
            self._usage = usage

    def finish(self) -> None:
        with self._lock:
            self._done = True

    @property
    def text_so_far(self) -> str:
        with self._lock:
            return "".join(self._parts)

    @property
    def usage(self) -> LLMUsage:
        with self._lock:
            return LLMUsage(**vars(self._usage))

    def __iter__(self) -> Iterator[str]:
        return self

    def __next__(self) -> str:
        # iteration is a cursor over the accumulated parts  consuming the
        # iterator must NOT destroy the final text (result() reads it after)
        with self._lock:
            if self._cursor < len(self._parts):
                piece = self._parts[self._cursor]
                self._cursor += 1
                return piece
            raise StopIteration

    def result(self) -> LLMResult:
        with self._lock:
            text = "".join(self._parts)
            usage = LLMUsage(**vars(self._usage)) if self._usage else LLMUsage()
        return LLMResult(text=text, usage=usage, model=self._model,
                         provider=self._provider)


class ProviderError(Exception):
    """Type-coded provider failure. Messages carry NO response bodies, NO
    URLs, NO credentials  the RAG service surfaces the code only."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class ProviderTimeout(ProviderError):
    def __init__(self):
        super().__init__("provider_timeout")


class ProviderBusy(ProviderError):
    """All concurrency slots taken  the API maps this to 429 + Retry-After."""

    def __init__(self, retry_after: float):
        super().__init__("provider_busy")
        self.retry_after = max(0.0, retry_after)


# ---------------------------------------------------------------------------
# Retry policy (shared by the real adapters)
# ---------------------------------------------------------------------------

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _sleep_jitter(attempt: int) -> None:
    """Bounded exponential backoff with jitter: [0, 0.5 * 2^attempt)."""
    import random
    time.sleep(random.uniform(0.0, 0.5 * (2 ** attempt)))


def _post_with_retries(url: str, *, headers: dict, body: dict,
                       timeout: float, max_retries: int):
    """POST with the P7-3 retry contract: connect errors / 429 / 5xx are
    retried (<= max_retries, jittered); READ timeouts and other failures
    fail fast (a non-idempotent-capable call may already have landed).
    Raises ProviderError subclasses; returns the raw Response (callers
    parse JSON or iterate SSE lines)."""
    attempt = 0
    while True:
        try:
            resp = requests.post(url, headers=headers,
                                 json=body, timeout=(timeout, timeout))
        except requests.exceptions.ConnectionError:
            if attempt < max_retries:
                _sleep_jitter(attempt)
                attempt += 1
                continue
            raise ProviderError("provider_connect_error") from None
        except requests.exceptions.Timeout:
            raise ProviderTimeout() from None
        except requests.exceptions.RequestException:
            raise ProviderError("provider_request_error") from None
        if resp.status_code in _RETRYABLE_STATUS and attempt < max_retries:
            _sleep_jitter(attempt)
            attempt += 1
            continue
        if resp.status_code == 401:
            raise ProviderError("provider_auth_error")
        if resp.status_code >= 400:
            # body deliberately dropped: never surfaced, never logged
            raise ProviderError(f"provider_http_error_{resp.status_code}")
        return resp


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------

class LLMProvider(ABC):
    """`complete` is the whole surface. NOTE: there is intentionally NO
    `tools` / `functions` / `tool_choice` parameter  tool-calling cannot
    be configured into this layer, so retrieved text can never trigger a
    tool, a fetch or a write (PHASE7_PLAN 2)."""

    name: str = ""

    @property
    @abstractmethod
    def model(self) -> str: ...

    @abstractmethod
    def complete(self, messages: list[dict], *, max_tokens: int,
                 timeout: Optional[float] = None,
                 stream: bool = False) -> Union[LLMResult, LLMStream]:
        """messages: [{"role": "system"|"user"|"assistant", "content": str}].
        stream=False -> LLMResult; stream=True -> LLMStream (iterate deltas,
        then .result()). Temperature is 0 by default (provider-configured
        decoding params are recorded in answer metadata by the service)."""


class _SlotMixin:
    """Bounded-concurrency acquire: hold a semaphore slot for the whole
    call; on saturation wait `queue_wait` then raise ProviderBusy."""

    def _init_slot(self, concurrency: int, queue_wait: float) -> None:
        self._sem = threading.BoundedSemaphore(concurrency)
        self._queue_wait = queue_wait

    def _slot(self):
        class _Acq:
            def __init__(acq, sem, wait):
                acq.sem, acq.wait, acq.ok = sem, wait, False

            def __enter__(acq):
                if not acq.sem.acquire(timeout=max(acq.wait, 0.0)):
                    raise ProviderBusy(max(acq.wait, 1.0))
                acq.ok = True
                return acq

            def __exit__(acq, *exc):
                if acq.ok:
                    acq.sem.release()
                return False
        return _Acq(self._sem, self._queue_wait)


# ---------------------------------------------------------------------------
# Stub provider (default in tests/CI  offline, deterministic, adversarial)
# ---------------------------------------------------------------------------

# The frame contract the RAG prompt builder (P7-5 prompt.py) uses; the
# stub parses sources with it so adversarial tests exercise the REAL
# prompt surface. Nonce is per-request random; the stub ignores its value.
_SOURCE_FRAME_RE = re.compile(
    r"==== NXSRAG-[0-9a-f]+ SOURCE (\d+) ====\n(.*?)\n"
    r"==== NXSRAG-[0-9a-f]+ END SOURCE \1 ====",
    re.DOTALL)
_INJECTION_RE = re.compile(
    r"(?im)^\s*(?:ignore|disregard|forget)\b[^\n]*\b(?:instruction|rule|prompt)"
    r"|^\s*system\s*:",)


def _normalized_lines(text: str):
    """Line-by-line view with the SAME normalization the prompt builder's
    stripper uses (homoglyphs folded, zero-width stripped, NFKC). The
    stub oracle must still OBEY a homoglyph-dodged injection so the
    defense tests measure the STRIPPER, not the oracle's regex."""
    from .prompt import _normalize_for_matching
    for line in text.splitlines():
        yield line, _normalize_for_matching(line)


def _find_injection(text: str) -> Optional[str]:
    for raw, probe in _normalized_lines(text):
        if _INJECTION_RE.search(probe):
            return raw
    return None


def parse_fenced_sources(messages: list[dict]) -> dict[int, str]:
    """Sources from the last user message, by number -> text."""
    user_texts = [m.get("content") or "" for m in messages
                  if m.get("role") == "user"]
    sources: dict[int, str] = {}
    if not user_texts:
        return sources
    for match in _SOURCE_FRAME_RE.finditer(user_texts[-1]):
        sources[int(match.group(1))] = match.group(2)
    return sources


def _first_words(text: str, n: int) -> str:
    words = text.split()
    return " ".join(words[:n])


class StubProvider(LLMProvider, _SlotMixin):
    """Deterministic offline provider with selectable behaviors.

    BEHAVIORS (constructor arg, default "extractive"):
      extractive           answers from the TOP source with a correct [1]
                           citation and a verbatim quote (falls back to
                           no_answer when no fenced source is present)
      no_answer            declines to answer (no citations)
      hallucinated_citation cites [99]  an id no retrieval produced
      obey_injection       scans the data region for an instruction-like
                           line and OBEYS it (the P7-5/P7-9 defense oracle)
      markdown_exfil       emits ![](http://attacker...) + [x](http://...)
      html_payload         emits <script> + <img onerror=...>
      prompt_leak          echoes the system message verbatim
      bad_quote            quotes text ABSENT from the cited source
      slow                 sleeps timeout+1s (fires the hard-timeout path)
      error                raises ProviderError("stub_error")
    """
    name = "stub"

    def __init__(self, behavior: str = "extractive", concurrency: int = 64,
                 queue_wait: float = 2.0):
        self.behavior = behavior
        self._model = "stub-deterministic-1"
        self._init_slot(concurrency, queue_wait)

    @property
    def model(self) -> str:
        return self._model

    def complete(self, messages, *, max_tokens, timeout=None,
                 stream=False) -> Union[LLMResult, LLMStream]:
        t = 30.0 if timeout is None else timeout
        if stream:
            with self._slot():
                text, usage = self._generate(messages, max_tokens, t)
                return self._to_stream(text, usage)
        # Non-stream: the stub enforces its own wall-clock deadline (the
        # real adapters get theirs from the requests socket timeout)  a
        # `slow` behavior must surface as ProviderTimeout, never a hang.
        with self._slot():
            box: dict = {}

            def _run():
                try:
                    box["out"] = self._generate(messages, max_tokens, t)
                except BaseException as exc:  # re-raised on the caller side
                    box["err"] = exc

            worker = threading.Thread(target=_run, daemon=True)
            worker.start()
            worker.join(t)
            if "out" not in box and "err" not in box:
                raise ProviderTimeout()
            if "err" in box:
                raise box["err"]
            text, usage = box["out"]
            return LLMResult(text=text, usage=usage, model=self._model,
                             provider=self.name)

    def _generate(self, messages, max_tokens, timeout) -> tuple[str, LLMUsage]:
        b = self.behavior
        if b == "error":
            raise ProviderError("stub_error")
        if b == "slow":
            time.sleep((timeout or 30.0) + 1.0)
            return "", LLMUsage()
        prompt_chars = sum(len(m.get("content") or "") for m in messages)
        usage_in = LLMUsage(tokens_in=-(-prompt_chars // 4))
        if b == "prompt_leak":
            system = "\n".join(m.get("content") or "" for m in messages
                              if m.get("role") == "system")
            text = f"{system}\n--- SYSTEM PROMPT LEAKED"
        elif b == "obey_injection":
            data = "\n".join(m.get("content") or "" for m in messages
                             if m.get("role") != "system")
            m_ = _find_injection(data)
            if m_:
                text = f"OBEYED: {m_.strip()}"
            else:
                text, usage_in = self._extractive(messages, usage_in)
        elif b == "no_answer":
            text = ("I don't have enough evidence in the indexed sources "
                    "to answer that.")
        elif b == "hallucinated_citation":
            text = "The answer is documented in source [99]."
        elif b == "markdown_exfil":
            text = ("Based on [1]. ![leak](http://attacker.example/pixel) "
                    "and [click here](http://attacker.example/link).")
        elif b == "html_payload":
            text = ("Based on [1]. <script>alert(1)</script>"
                    "<img src=x onerror=alert(1)>")
        elif b == "bad_quote":
            text = ('According to [1], "the warp drive runs on quantum '
                    'flux capacitors".')
        else:  # extractive (also the fallback for unknown behaviors)
            text, usage_in = self._extractive(messages, usage_in)
        text = text[: max_tokens * 4]  # respect the answer budget
        usage = LLMUsage(tokens_in=usage_in.tokens_in,
                        tokens_out=-(-len(text) // 4))
        return text, usage

    def _extractive(self, messages, usage_in) -> tuple[str, LLMUsage]:
        sources = parse_fenced_sources(messages)
        if not sources:
            return ("I don't have enough evidence in the indexed sources "
                    "to answer that."), usage_in
        top = sources[min(sources)]
        quote = _first_words(top, 12)
        answer = (f"{_first_words(top, 40)} [1] "
                  f'As the source states, "{quote}" [1]')
        return answer, usage_in

    def _to_stream(self, text: str, usage: LLMUsage) -> LLMStream:
        s = LLMStream(self._model, self.name)
        words = text.split(" ")
        for i in range(0, len(words), 3):
            s.add(" ".join(words[i:i + 3]) + " ")
        s.set_usage(usage)
        s.finish()
        return s


# ---------------------------------------------------------------------------
# OpenAI-compatible adapter
# ---------------------------------------------------------------------------

class OpenAIProvider(LLMProvider, _SlotMixin):
    """Chat-completions over `requests`. NEXUS_LLM_BASE_URL retargets to any
    OpenAI-compatible server (local ones included)."""

    name = "openai"
    _DEFAULT_URL = "https://api.openai.com/v1"

    def __init__(self, config: RagConfig):
        self.config = config
        self._model = config.resolved_model()
        self._base = (config.base_url or self._DEFAULT_URL).rstrip("/")
        self._init_slot(config.concurrency, config.queue_wait)

    @property
    def model(self) -> str:
        return self._model

    def complete(self, messages, *, max_tokens, timeout=None,
                 stream=False) -> Union[LLMResult, LLMStream]:
        t = self.config.timeout if timeout is None else timeout
        with self._slot():
            body = {
                "model": self._model,
                "messages": messages,
                "temperature": self.config.temperature,
                "max_tokens": max_tokens,
                "stream": bool(stream),
            }
            headers = {"Content-Type": "application/json",
                       "Authorization": f"Bearer {self.config.api_key}"}
            data = _post_with_retries(f"{self._base}/chat/completions",
                                      headers=headers, body=body,
                                      timeout=t,
                                      max_retries=self.config.max_retries)
            if stream:
                return self._parse_stream(data)
            try:
                data = data.json()
                text = data["choices"][0]["message"].get("content") or ""
                usage = data.get("usage") or {}
                return LLMResult(
                    text=text,
                    usage=LLMUsage(tokens_in=int(usage.get("prompt_tokens", 0)),
                                   tokens_out=int(usage.get("completion_tokens", 0))),
                    model=self._model, provider=self.name)
            except (KeyError, TypeError, ValueError):
                raise ProviderError("provider_bad_response") from None

    def _parse_stream(self, resp) -> LLMStream:
        """Consume the SSE body into an LLMStream. Usage: estimated from
        text length when the endpoint does not include it (documented)."""
        stream = LLMStream(self._model, self.name)
        text_parts: list[str] = []
        try:
            for raw_line in resp.iter_lines(decode_unicode=True):
                if not raw_line or not raw_line.startswith("data:"):
                    continue
                payload = raw_line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    continue
                delta = (chunk.get("choices") or [{}])[0].get("delta", {})
                piece = delta.get("content")
                if piece:
                    text_parts.append(piece)
                    stream.add(piece)
                usage = chunk.get("usage")
                if usage:
                    stream.set_usage(LLMUsage(
                        tokens_in=int(usage.get("prompt_tokens", 0)),
                        tokens_out=int(usage.get("completion_tokens", 0))))
        except requests.exceptions.RequestException:
            raise ProviderTimeout() from None
        if not stream.usage.tokens_in and not stream.usage.tokens_out:
            joined = "".join(text_parts)
            stream.set_usage(LLMUsage(tokens_in=-(-len(joined) // 4),
                                      tokens_out=-(-len(joined) // 4)))
        stream.finish()
        return stream


# ---------------------------------------------------------------------------
# Anthropic Messages adapter
# ---------------------------------------------------------------------------

class AnthropicProvider(LLMProvider, _SlotMixin):
    name = "anthropic"
    _DEFAULT_URL = "https://api.anthropic.com"

    def __init__(self, config: RagConfig):
        self.config = config
        self._model = config.resolved_model()
        self._base = (config.base_url or self._DEFAULT_URL).rstrip("/")
        self._init_slot(config.concurrency, config.queue_wait)

    @property
    def model(self) -> str:
        return self._model

    def complete(self, messages, *, max_tokens, timeout=None,
                 stream=False) -> Union[LLMResult, LLMStream]:
        t = self.config.timeout if timeout is None else timeout
        with self._slot():
            system = "\n".join(m.get("content") or "" for m in messages
                               if m.get("role") == "system")
            rest = [m for m in messages if m.get("role") != "system"]
            body = {
                "model": self._model,
                "max_tokens": max_tokens,
                "temperature": self.config.temperature,
                "messages": rest,
                "stream": bool(stream),
            }
            if system:
                body["system"] = system
            headers = {
                "Content-Type": "application/json",
                "x-api-key": self.config.api_key,
                "anthropic-version": "2023-06-01",
            }
            data = _post_with_retries(f"{self._base}/v1/messages",
                                      headers=headers, body=body,
                                      timeout=t,
                                      max_retries=self.config.max_retries)
            if stream:
                return self._parse_stream(data)
            try:
                data = data.json()
                text = "".join(
                    block.get("text", "")
                    for block in data.get("content", [])
                    if isinstance(block, dict)
                    and block.get("type") == "text")
                usage = data.get("usage") or {}
                return LLMResult(
                    text=text,
                    usage=LLMUsage(tokens_in=int(usage.get("input_tokens", 0)),
                                   tokens_out=int(usage.get("output_tokens", 0))),
                    model=self._model, provider=self.name)
            except (KeyError, TypeError, ValueError):
                raise ProviderError("provider_bad_response") from None

    def _parse_stream(self, data) -> LLMStream:
        stream = LLMStream(self._model, self.name)
        text_parts: list[str] = []
        try:
            for raw_line in data.iter_lines(decode_unicode=True):
                if not raw_line or not raw_line.startswith("data:"):
                    continue
                try:
                    event = json.loads(raw_line[5:].strip())
                except ValueError:
                    continue
                etype = event.get("type")
                if etype == "content_block_delta":
                    piece = (event.get("delta") or {}).get("text")
                    if piece:
                        text_parts.append(piece)
                        stream.add(piece)
                elif etype == "message_delta":
                    usage = event.get("usage") or {}
                    if usage:
                        stream.set_usage(LLMUsage(
                            tokens_in=int(usage.get("input_tokens", 0)),
                            tokens_out=int(usage.get("output_tokens", 0))))
                elif etype == "message_stop":
                    break
        except requests.exceptions.RequestException:
            raise ProviderTimeout() from None
        if not stream.usage.tokens_out and text_parts:
            joined = "".join(text_parts)
            stream.set_usage(LLMUsage(tokens_out=-(-len(joined) // 4)))
        stream.finish()
        return stream


# The interface is the export surface  adapters are chosen via config.
__all__ = [
    "LLMProvider", "LLMResult", "LLMUsage", "LLMStream",
    "ProviderError", "ProviderTimeout", "ProviderBusy",
    "StubProvider", "OpenAIProvider", "AnthropicProvider",
    "parse_fenced_sources",
]


