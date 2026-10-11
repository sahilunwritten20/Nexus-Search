"""P7-3 â€” data-egress controls for the RAG answer path.

Everything below exists to bound WHAT leaves the system on an /ask call:

1. `llm_allowed` (P7-2/P7-3): document/chunk metadata MAY carry
   ``llm_allowed: false``. Such a chunk's TEXT is never sent to an LLM
   provider â€” it is excluded from the prompt (and only from the prompt:
   it may still appear as a retrieved source in a retrieval-only
   response).
2. `redact_pii` (P7-3): optional regex-based redaction of outbound prompt
   text (NEXUS_RAG_REDACT_PII, default OFF) â€” a documented, honest
   best-effort scrubber, not a guarantee.
3. `query_hash` (P7-3): when NEXUS_RAG_LOG_QUERIES=1, RAG logs query
   HASHES (sha256, first 16 hex) â€” never query text.
4. `EGRESS_DOC` (P7-3): the canonical, test-pinned list of exactly what
   leaves the system on an /ask call; README/docs copy it verbatim.

Secrets rule (PHASE7_PLAN Â§1): the provider key is read once from
NEXUS_LLM_API_KEY and may never appear in a log record, /health,
/metrics or any error body â€” enforced by the poisoned-key test.
"""
import hashlib
import re

_LLM_ALLOWED_KEY = "llm_allowed"


def llm_allowed(metadata: dict | None) -> bool:
    """False only when metadata explicitly says llm_allowed is false.

    Everything else (missing key, true, wrong type) allows: the flag is an
    operator opt-OUT for sensitive chunks, so an unset flag must not
    silently change corpus behavior."""
    try:
        value = (metadata or {}).get(_LLM_ALLOWED_KEY)
    except AttributeError:  # metadata is not even a mapping
        return True
    return value is not False


# ---------------------------------------------------------------------------
# PII redaction (NEXUS_RAG_REDACT_PII, default OFF; best-effort regexes)
# ---------------------------------------------------------------------------

_PII_PATTERNS: list[tuple[str, str]] = [
    # emails
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
     "[REDACTED-EMAIL]"),
    # credit-card-like: 13-19 digits with spaces/dashes (Luhn-free on purpose)
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[REDACTED-CARD]"),
    # phone-like: +country and 7+ digits with separators
    (re.compile(r"\+?\d[\d\s().-]{8,}\d"), "[REDACTED-PHONE]"),
    # SSN-like
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED-SSN]"),
]


def redact_pii(text: str) -> tuple[str, int]:
    """Best-effort regex redaction of outbound prompt text. Returns
    (redacted_text, redaction_count). DOCUMENTED LIMITS: pattern-based,
    not semantic â€” a novel format or a deliberate obfuscation
    (j a n e @ d o e . c o m) passes through. Quote verification checks
    against the text AS SENT, so redacted prompts still verify correctly."""
    if not text:
        return text, 0
    count = 0
    for pattern, marker in _PII_PATTERNS:
        text, n = pattern.subn(marker, text)
        count += n
    return text, count


def query_hash(query: str) -> str:
    """The ONLY form of a query that may be logged: sha256, truncated."""
    return hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# The canonical egress list (docs + README must match this verbatim)
# ---------------------------------------------------------------------------

EGRESS_DOC = [
    "On an /ask call with a REAL provider configured, the following leaves "
    "the process:",
    "1. The numbered, fenced source chunks that survived context selection "
    "(minus any llm_allowed:false chunks, minus PII if NEXUS_RAG_REDACT_PII=1).",
    "2. The user query (post-redaction when enabled) and, on follow-ups, "
    "the condensed session transcript placed in the DATA region.",
    "3. The fixed system prompt (answer only from sources, cite [n], refuse "
    "without evidence, no tool use, answer in the question's language).",
    "4. Model name and decoding parameters configured for the provider.",
    "NEVER leaves: the API key is sent only to the configured provider "
    "endpoint as an auth header; no key, prompt or answer text is logged, "
    "exposed on /health or /metrics, or returned in error bodies.",
    "With the stub provider (default in dev/CI) nothing leaves the process "
    "at all - it is offline by construction.",
]


if __name__ == "__main__":  # pragma: no cover - documentation helper
    for line in EGRESS_DOC:
        print(line)


