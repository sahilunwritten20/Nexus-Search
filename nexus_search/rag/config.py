"""P7-3 â€” RAG configuration: every NEXUS_LLM_* / NEXUS_RAG_* knob, fail-closed.

The key is read ONCE from NEXUS_LLM_API_KEY here and is never logged,
serialized or exposed (the poisoned-key test enforces it). A real provider
configured without a key leaves the RAG subsystem ABSENT (search keeps
serving); NEXUS_RAG_ENABLED=0 is the kill switch (disabled); the stub
provider outside dev requires NEXUS_RAG_ALLOW_STUB=1.
"""
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("nexus_search.rag.config")

HARD_TIMEOUT_CEILING = 30.0     # mission contract: default <= 30s, hard cap
MAX_RETRIES_CEILING = 2
DEFAULT_PROVIDER = "stub"
_KNOWN_PROVIDERS = ("stub", "openai", "anthropic")


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    raw = _env(name)
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(lo, min(hi, value))


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = _env(name)
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(lo, min(hi, value))


@dataclass(frozen=True)
class RagConfig:
    # provider
    provider: str = DEFAULT_PROVIDER
    api_key: str = ""               # NEVER logged / serialized (poison test)
    base_url: str = ""              # "" = provider default endpoint
    model: str = ""
    temperature: float = 0.0
    max_tokens: int = 1024          # answer token budget per call
    timeout: float = 30.0           # hard per-call timeout, <= 30s
    max_retries: int = 2            # bounded retry, <= 2
    concurrency: int = 4            # provider call semaphore
    queue_wait: float = 2.0         # semaphore patience before ProviderBusy
    price_table: dict = field(default_factory=dict)  # model -> {"in","out"} $/1M

    # boot policy
    rag_enabled: bool = True
    allow_stub_outside_dev: bool = False
    env: str = "production"         # NEXUS_ENV snapshot (stub policy)

    # retrieval / context knobs (P7-1/P7-2)
    use_rerank: bool = True
    max_context_tokens: int = 6000
    max_context_bytes: int = 24_576
    chars_per_token: int = 4
    per_parent_cap: int = 3
    dedup_threshold: float = 0.85
    stitch_neighbors: bool = True

    # egress controls
    log_queries: bool = False       # logs query HASHES only, never text
    redact_pii: bool = False

    @classmethod
    def from_env(cls) -> "RagConfig":
        provider = (_env("NEXUS_LLM_PROVIDER") or DEFAULT_PROVIDER).lower()
        price_raw = _env("NEXUS_LLM_PRICE_TABLE")
        price_table: dict = {}
        if price_raw:
            try:
                parsed = json.loads(price_raw)
            except json.JSONDecodeError:
                logger.warning("NEXUS_LLM_PRICE_TABLE is not valid JSON; "
                               "cost will be reported as null")
                parsed = None
            if isinstance(parsed, dict):
                for model, entry in parsed.items():
                    if isinstance(entry, dict):
                        price_table[str(model)] = {
                            "in": float(entry.get("in", 0) or 0),
                            "out": float(entry.get("out", 0) or 0),
                        }
            elif parsed is not None:
                logger.warning("NEXUS_LLM_PRICE_TABLE must be a JSON object; "
                               "cost will be reported as null")
        return cls(
            provider=provider,
            api_key=_env("NEXUS_LLM_API_KEY"),
            base_url=_env("NEXUS_LLM_BASE_URL"),
            model=_env("NEXUS_LLM_MODEL"),
            temperature=_env_float("NEXUS_LLM_TEMPERATURE", 0.0, 0.0, 2.0),
            max_tokens=_env_int("NEXUS_LLM_MAX_TOKENS", 1024, 1, 8192),
            timeout=_env_float("NEXUS_LLM_TIMEOUT", 30.0, 0.5,
                               HARD_TIMEOUT_CEILING),
            max_retries=_env_int("NEXUS_LLM_MAX_RETRIES", 2, 0,
                                 MAX_RETRIES_CEILING),
            concurrency=_env_int("NEXUS_LLM_CONCURRENCY", 4, 1, 64),
            queue_wait=_env_float("NEXUS_LLM_QUEUE_WAIT", 2.0, 0.0, 30.0),
            price_table=price_table,
            rag_enabled=_env_bool("NEXUS_RAG_ENABLED", True),
            allow_stub_outside_dev=_env_bool("NEXUS_RAG_ALLOW_STUB", False),
            env=(_env("NEXUS_ENV", "production").lower() or "production"),
            use_rerank=_env_bool("NEXUS_RAG_USE_RERANK", True),
            max_context_tokens=_env_int("NEXUS_RAG_MAX_CONTEXT_TOKENS",
                                        6000, 1, 1_000_000),
            max_context_bytes=_env_int("NEXUS_RAG_MAX_CONTEXT_BYTES",
                                       24_576, 1, 10_000_000),
            chars_per_token=_env_int("NEXUS_RAG_CHARS_PER_TOKEN", 4, 1, 16),
            per_parent_cap=_env_int("NEXUS_RAG_PER_PARENT_CAP", 3, 1, 32),
            dedup_threshold=_env_float("NEXUS_RAG_DEDUP_THRESHOLD",
                                       0.85, 0.0, 1.0),
            stitch_neighbors=_env_bool("NEXUS_RAG_STITCH_NEIGHBORS", True),
            log_queries=_env_bool("NEXUS_RAG_LOG_QUERIES", False),
            redact_pii=_env_bool("NEXUS_RAG_REDACT_PII", False),
        )

    # ----------------------------------------------------------- boot policy

    def status(self) -> tuple[str, Optional[str]]:
        """(status, reason): 'ok' | 'absent' | 'disabled'.

        - disabled: NEXUS_RAG_ENABLED=0 (kill switch) -> /ask* refuse 503.
        - absent: real provider without a key, unknown provider, or the
          stub outside dev without NEXUS_RAG_ALLOW_STUB=1. Keyword/hybrid
          search keeps serving; /health reports rag: absent.
        - ok: the RAG subsystem is configured and enabled."""
        if not self.rag_enabled:
            return "disabled", "kill_switch"
        if self.provider not in _KNOWN_PROVIDERS:
            return "absent", "unknown_provider"
        if self.provider == "stub":
            if self.env == "dev" or self.allow_stub_outside_dev:
                return "ok", None
            return "absent", "stub_requires_opt_in"
        if not self.api_key:
            return "absent", "llm_key_missing"
        return "ok", None

    def default_model(self) -> str:
        if self.model:
            return self.model
        return {"openai": "gpt-4o-mini",
                "anthropic": "claude-3-5-haiku-latest",
                "stub": "stub-deterministic-1"}[self.provider]

    def resolved_model(self) -> str:
        """Model name actually used by the provider (for answer metadata
        and price lookups)."""
        return self.model or self.default_model()

    def estimate_cost(self, tokens_in: int, tokens_out: int) -> Optional[float]:
        """USD estimate from the price table ($/1M tokens); None when the
        model has no entry (cost honestly reported as null)."""
        entry = self.price_table.get(self.resolved_model())
        if not entry:
            return None
        return round((tokens_in / 1e6) * entry["in"]
                     + (tokens_out / 1e6) * entry["out"], 6)


def build_provider(config: RagConfig):
    """Construct the configured provider (NO network at construction).
    Returns (provider | None, status, reason)."""
    status, reason = config.status()
    if status != "ok":
        if reason:
            logger.warning("RAG subsystem %s: %s", status, reason)
        return None, status, reason
    from .provider import AnthropicProvider, OpenAIProvider, StubProvider
    if config.provider == "stub":
        return StubProvider(), status, reason
    if config.provider == "openai":
        return OpenAIProvider(config), status, reason
    if config.provider == "anthropic":
        return AnthropicProvider(config), status, reason
    return None, "absent", "unknown_provider"  # unreachable (status checked)

