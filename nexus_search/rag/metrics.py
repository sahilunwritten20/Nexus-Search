"""P7-3 — thread-safe RAG counters (rendered by /metrics in P7-10).

Numbers only: tokens in/out, estimated cost, requests, refusals, degraded
answers, provider errors/timeouts, injection redactions. A test proves no
prompt text or key ever reaches the snapshot (metrics are counts, never
content)."""
import threading
import time


def _day_key(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


class RagMetrics:
    """Day-keyed counters with unbounded-day pruning (keeps `max_days`)."""

    def __init__(self, max_days: int = 14):
        self._lock = threading.Lock()
        self._max_days = max_days
        self._daily: dict[str, dict[str, float]] = {}
        self._totals: dict[str, float] = {}

    # ---------------------------------------------------------------- core

    def _bump(self, name: str, value: float = 1.0, *, daily: bool = False,
              day: str | None = None) -> None:
        with self._lock:
            self._totals[name] = self._totals.get(name, 0.0) + value
            if daily:
                key = day or _day_key(time.time())
                bucket = self._daily.setdefault(key, {})
                bucket[name] = bucket.get(name, 0.0) + value

    def _prune(self) -> None:
        if len(self._daily) <= self._max_days:
            return
        for key in sorted(self._daily)[:-self._max_days]:
            self._daily.pop(key, None)

    # ------------------------------------------------------------- events

    def record_request(self) -> None:
        self._bump("requests", daily=True)

    def record_refusal(self, reason: str) -> None:
        self._bump("refusals", daily=True)
        self._bump(f"refusals_{reason}")

    def record_degraded(self) -> None:
        self._bump("degraded_answers", daily=True)

    def record_usage(self, tokens_in: int, tokens_out: int,
                     cost: float | None) -> None:
        self._bump("tokens_in", float(tokens_in), daily=True)
        self._bump("tokens_out", float(tokens_out), daily=True)
        if cost is not None:
            self._bump("estimated_cost_usd", float(cost), daily=True)

    def record_provider_error(self, code: str) -> None:
        self._bump("provider_errors", daily=True)
        self._bump(f"provider_error_{code}")
        if code == "provider_timeout":
            self._bump("provider_timeouts", daily=True)

    def record_redactions(self, count: int) -> None:
        if count:
            self._bump("redactions", float(count))

    def record_citation_failure(self, count: int = 1) -> None:
        self._bump("citation_verification_failures", float(count))

    def record_unsupported_claim(self, count: int = 1) -> None:
        self._bump("unsupported_claims", float(count))

    def record_budget_rejection(self) -> None:
        self._bump("budget_rejections", daily=True)

    def record_open_stream(self, delta: int = 1) -> None:
        self._bump("open_sse_streams", float(delta))
        with self._lock:
            current = self._totals.get("open_sse_streams", 0.0)
            if current < 0:
                self._totals["open_sse_streams"] = 0.0

    # ------------------------------------------------------------ reading

    def snapshot(self) -> dict:
        with self._lock:
            self._prune()
            return {
                "totals": dict(sorted(self._totals.items())),
                "daily": {day: dict(sorted(bucket.items()))
                          for day, bucket in sorted(self._daily.items())},
            }


# One process-wide instance; the API wires it into /metrics (P7-10).
_GLOBAL = RagMetrics()


def global_metrics() -> RagMetrics:
    return _GLOBAL
