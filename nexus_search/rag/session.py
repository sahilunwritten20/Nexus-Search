"""P7-8 — conversation context: sessions.

DEFAULT: RAM-only, per-worker (sticky routing REQUIRED for multi-worker
deployments — documented; a session that lands on a worker that does not
know it fails with `session_expired`, never silently treated as new when
a tombstone exists). OPT-IN: a shared SQLite session store
(NEXUS_RAG_SESSION_STORE=sqlite) through the ONE storage connection set
(migration v4) with TTL pruning — the ONLY persistence path, OFF by
default, retention-controlled via rag_session_prune.

SECURITY: session ids are client-supplied (untrusted, length-capped,
charset-restricted at the request model). Sessions are SCOPED per budget
bucket (verified-key hash or IP) — one key can never read another key's
session. Transcript text is UNTRUSTED: it enters prompts only through
the data region with the injection stripper applied (prompt.py).

CAPS (each enforced BEFORE the LLM call, each a clear 4xx):
max turns per session (default 10), max session context tokens (default
4K, chars/token heuristic), max retained bytes per turn (default 2000).
Idle-TTL eviction + a global session cap protect memory.

FOLLOW-UPS: a bounded "condense follow-up into a standalone query" step
— deterministic heuristic by default (zero LLM spend); NEXUS_RAG_FOLLOWUP_
CONDENSE=llm uses the provider (counted in the budget) and falls back to
the heuristic on ANY failure. Retrieval ALWAYS runs on the standalone
query; the transcript is data, never instructions.
"""
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from .provider import ProviderError


class SessionExpired(Exception):
    """The session id references a session this store no longer has
    (idle-TTL eviction or restart). The API maps this to a clear 4xx."""


class SessionCapExceeded(Exception):
    """A session cap (turns / context tokens / turn bytes) was hit.
    The API maps this to 400 with the cap named."""


@dataclass
class SessionTurn:
    role: str          # "user" | "assistant"
    text: str
    ts: float


@dataclass
class Session:
    turns: list[SessionTurn] = field(default_factory=list)
    last_used: float = 0.0

    def history_text(self, max_turns: int) -> str:
        """Render the transcript for the prompt's DATA region."""
        lines = []
        for t in self.turns[-max_turns:]:
            who = "User" if t.role == "user" else "Assistant"
            lines.append(f"{who}: {t.text}")
        return "\n".join(lines)


class MemorySessionStore:
    """RAM-only, per-worker. Idle-TTL + global-cap eviction; tombstones
    remember evicted ids so a follow-up gets session_expired (honest)
    rather than a silently fresh session (best-effort: tombstones do not
    survive restarts — documented limit)."""

    def __init__(self, *, max_turns: int, max_context_tokens: int,
                 chars_per_token: int, max_turn_bytes: int, ttl: float,
                 global_cap: int):
        self.max_turns = max_turns
        self.max_context_tokens = max_context_tokens
        self.chars_per_token = chars_per_token
        self.max_turn_bytes = max_turn_bytes
        self.ttl = ttl
        self.global_cap = global_cap
        self._lock = threading.Lock()
        self._sessions: dict[tuple[str, str], Session] = {}
        self._expired: set[tuple[str, str]] = set()

    # ------------------------------------------------------------- read

    def get(self, scope: str, session_id: str) -> Optional[Session]:
        key = (scope, session_id)
        with self._lock:
            now = time.time()
            session = self._sessions.get(key)
            if session is None:
                if key in self._expired:
                    self._expired.discard(key)
                    raise SessionExpired(session_id)
                return None
            if now - session.last_used > self.ttl:
                self._sessions.pop(key, None)
                self._expired.add(key)
                raise SessionExpired(session_id)
            session.last_used = now
            return Session(turns=list(session.turns), last_used=now)

    # ------------------------------------------------------------- write

    def append(self, scope: str, session_id: str, role: str, text: str) -> Session:
        key = (scope, session_id)
        if len(text) > self.max_turn_bytes:
            raise SessionCapExceeded(f"turn_bytes:{len(text)}")
        with self._lock:
            now = time.time()
            session = self._sessions.get(key)
            if session is None:
                if key in self._expired:
                    self._expired.discard(key)
                    raise SessionExpired(session_id)
            elif now - session.last_used > self.ttl:
                # idle past the TTL: same verdict get() would give
                self._sessions.pop(key, None)
                self._expired.add(key)
                raise SessionExpired(session_id)
            else:
                session.last_used = now
            if session is None:
                session = Session(last_used=now)
            # context-token cap over the WHOLE session, BEFORE anything runs
            candidate = Session(turns=session.turns + [SessionTurn(role, text[:self.max_turn_bytes], now)],
                                 last_used=now)
            if len(candidate.turns) > self.max_turns:
                self._check_memory_pressure()
                raise SessionCapExceeded(f"turns:{len(candidate.turns)}")
            est_tokens = sum(-(-len(t.text) // self.chars_per_token)
                             for t in candidate.turns)
            if est_tokens > self.max_context_tokens:
                self._check_memory_pressure()
                raise SessionCapExceeded(f"context_tokens:{est_tokens}")
            self._sessions[key] = candidate
            self._expired.discard(key)
            self._evict_idle_locked(now)
            self._evict_lru_locked()
            return Session(turns=list(candidate.turns), last_used=now)

    # ---------------------------------------------------------- eviction

    def _check_memory_pressure(self) -> None:
        pass  # pressure handled by global-cap eviction below

    def _evict_idle_locked(self, now: float) -> None:
        for key in [k for k, s in self._sessions.items()
                    if now - s.last_used > self.ttl]:
            self._sessions.pop(key, None)
            self._expired.add(key)

    def _evict_lru_locked(self) -> None:
        while len(self._sessions) > self.global_cap:
            oldest = min(self._sessions, key=lambda k: self._sessions[k].last_used)
            self._sessions.pop(oldest)
            self._expired.add(oldest)

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def prune_expired(self) -> int:
        now = time.time()
        with self._lock:
            return len([k for k, s in list(self._sessions.items())
                        if now - s.last_used > self.ttl])


class SqliteSessionStore:
    """OPT-IN shared store (NEXUS_RAG_SESSION_STORE=sqlite): the same
    contract on the ONE storage connection set (migration v4) so sessions
    are visible across workers/replicas. RETENTION: rows persist until
    rag_session_prune(ttl) runs — schedule it (the A/B-log purge
    precedent). Tombstones are RAM-side per process (best effort)."""

    def __init__(self, storage, *, max_turns: int, max_context_tokens: int,
                 chars_per_token: int, max_turn_bytes: int, ttl: float,
                 global_cap: int):
        self.storage = storage
        self.max_turns = max_turns
        self.max_context_tokens = max_context_tokens
        self.chars_per_token = chars_per_token
        self.max_turn_bytes = max_turn_bytes
        self.ttl = ttl
        self.global_cap = global_cap
        self._lock = threading.Lock()
        self._expired: set[tuple[str, str]] = set()

    def get(self, scope: str, session_id: str) -> Optional[Session]:
        with self._lock:
            rows = self.storage.rag_session_turns(scope, session_id)
            now = time.time()
            if not rows:
                if (scope, session_id) in self._expired:
                    self._expired.discard((scope, session_id))
                    raise SessionExpired(session_id)
                return None
            session = Session(last_used=now)
            for _idx, role, text, updated in rows:
                session.turns.append(SessionTurn(role=role, text=text,
                                                 ts=updated))
            if now - session.last_used > self.ttl:
                self.storage.rag_session_delete(scope, session_id)
                self._expired.add((scope, session_id))
                raise SessionExpired(session_id)
            if rows and now - max(r[3] for r in rows) > self.ttl:
                self.storage.rag_session_delete(scope, session_id)
                self._expired.add((scope, session_id))
                raise SessionExpired(session_id)
            return session

    def append(self, scope: str, session_id: str, role: str, text: str) -> Session:
        if len(text) > self.max_turn_bytes:
            raise SessionCapExceeded(f"turn_bytes:{len(text)}")
        with self._lock:
            rows = self.storage.rag_session_turns(scope, session_id)
            now = time.time()
            if (len(rows) + 1) > self.max_turns:
                raise SessionCapExceeded(f"turns:{len(rows) + 1}")
            est_tokens = sum(-(-len(r[2]) // self.chars_per_token)
                             for r in rows)
            est_tokens += -(-len(text) // self.chars_per_token)
            if est_tokens > self.max_context_tokens:
                raise SessionCapExceeded(f"context_tokens:{est_tokens}")
            if not rows and (scope, session_id) in self._expired:
                self._expired.discard((scope, session_id))
                raise SessionExpired(session_id)
            self.storage.rag_session_append(scope, session_id, len(rows),
                                            role, text, now)
            session = Session(last_used=now)
            session.turns = [SessionTurn(role=r[1], text=r[2], ts=r[3])
                             for r in rows]
            session.turns.append(SessionTurn(role, text, now))
            return session

    def count(self) -> int:
        return self.storage.rag_session_count()

    def prune_expired(self) -> int:
        return self.storage.rag_session_prune(self.ttl)


# ---------------------------------------------------------------------------
# Follow-up condensing
# ---------------------------------------------------------------------------

_FOLLOWUP_MARKERS = ("and", "but", "what about", "how about", "why",
                     "tell me more", "more", "it", "they", "that", "this")


def heuristic_condense(query: str, history_text: str) -> str:
    """Deterministic fallback: when the query looks like a follow-up
    (short, or starts with a follow-up marker) and history exists, splice
    the previous user turn onto it; otherwise pass it through. Zero LLM
    spend, fully testable."""
    if not history_text:
        return query
    lowered = query.strip().lower()
    words = lowered.split()
    looks_like_followup = (len(words) < 4
                           or lowered.startswith(_FOLLOWUP_MARKERS))
    if not looks_like_followup:
        return query
    prev_user = None
    for line in history_text.splitlines():
        if line.startswith("User: "):
            prev_user = line[len("User: "):]
    return f"{prev_user} {query}".strip() if prev_user else query


def condense_followup(query: str, history_text: str, *, mode: str,
                      provider=None, config=None,
                      messages_builder=None) -> str:
    """Bounded condense step. mode: 'heuristic' (default, zero spend),
    'llm' (provider call counted in the budget by the caller via the
    returned usage), or 'off'. ANY provider failure -> heuristic
    fallback (never a failed request over condensing)."""
    if not history_text:
        return query
    if mode == "off":
        return query
    if mode != "llm":
        return heuristic_condense(query, history_text)
    try:
        if provider is None or messages_builder is None:
            return heuristic_condense(query, history_text)
        result = provider.complete(
            messages_builder(history_text, query),
            max_tokens=min(config.max_tokens if config else 128, 256),
            timeout=(config.timeout if config else 30.0))
        rewritten = (result.text or "").strip()
        if rewritten and len(rewritten) < 1000:
            return rewritten
        return heuristic_condense(query, history_text)
    except ProviderError:
        return heuristic_condense(query, history_text)
