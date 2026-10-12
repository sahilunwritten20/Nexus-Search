"""P7-6 — per-bucket token budgets over SHARED state.

The slowapi limiter is IN-PROCESS MEMORY (PHASE7_PLAN §4/WP14-8), so the
token budget lives in the `rag_token_usage` SQLite table (storage
migration v3) through the ONE shared Storage connection set — caps hold
across workers and replicas. Bucketing mirrors `_rate_limit_key`: only a
key that VERIFIES against NEXUS_API_KEY earns its own bucket (hashed);
everything else falls through to the client IP.

Flow per /ask: PRE-CHECK the estimated worst-case spend BEFORE the
provider call (over budget -> 429 + Retry-After, zero spend), then
SETTLE with the actual usage after the call. The estimate = prompt token
estimate (chars/token heuristic) + max_tokens — an upper bound, so a
request that would not fit even in the best case is refused up front.
"""
import hashlib
import hmac
import logging

logger = logging.getLogger("nexus_search.rag.budget")

# NEXUS_RAG_DAILY_TOKEN_BUDGET: tokens per bucket per UTC-day. 0 = off.
DEFAULT_DAILY_TOKEN_BUDGET = 1_000_000


def daily_budget_from_env() -> int:
    import os
    raw = (os.environ.get("NEXUS_RAG_DAILY_TOKEN_BUDGET") or "").strip()
    if not raw:
        return DEFAULT_DAILY_TOKEN_BUDGET
    try:
        value = int(raw)
    except ValueError:
        logger.warning("NEXUS_RAG_DAILY_TOKEN_BUDGET=%r is not an int; "
                       "default %d applies", raw, DEFAULT_DAILY_TOKEN_BUDGET)
        return DEFAULT_DAILY_TOKEN_BUDGET
    return max(0, value)


def bucket_key(api_key: str | None, configured_key: str | None,
               client_ip: str | None) -> str:
    """The budget bucket: a VERIFIED key hashes to its own bucket
    (constant-time check, hash stored — never the raw key); anything
    else (missing/wrong key, open dev mode) falls through to the IP."""
    if api_key and configured_key:
        if hmac.compare_digest(api_key, configured_key):
            return "key:" + hashlib.sha256(api_key.encode()).hexdigest()[:24]
    return "ip:" + (client_ip or "unknown")


class BudgetExceeded(Exception):
    """Over the per-bucket daily token budget. The API maps this to
    429 + Retry-After (until the UTC day rolls over)."""

    def __init__(self, used: int, budget: int):
        super().__init__("rag_token_budget_exceeded")
        self.used = used
        self.budget = budget


def precheck(storage, bucket: str, estimate_tokens: int,
             budget: int | None = None) -> None:
    """Raise BudgetExceeded when `bucket` cannot fit `estimate_tokens`
    more tokens today. Called BEFORE any provider spend."""
    budget = daily_budget_from_env() if budget is None else budget
    if budget <= 0:
        return  # 0 = unlimited (documented)
    used = storage.rag_usage_get(bucket)
    if used + estimate_tokens > budget:
        logger.info("rag token budget pre-check refused bucket=%s "
                    "used=%d estimate=%d budget=%d", bucket, used,
                    estimate_tokens, budget)
        raise BudgetExceeded(used, budget)


def settle(storage, bucket: str, tokens_actual: int) -> None:
    """Record the ACTUAL spend after the call (mission: settle with
    actual usage afterward). Refusals and degraded responses settle 0
    (they spend nothing on the provider)."""
    if tokens_actual <= 0:
        return
    storage.rag_usage_add(bucket, tokens_actual)
