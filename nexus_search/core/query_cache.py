"""Short-TTL query-result cache for /search.

Why: at prototype scale every search re-scans postings lists cold. Repeat
traffic (dashboards, autocomplete-adjacent retries, user paging "next")
is wasteful; a tiny TTL cache absorbs it.

Invariants that keep this honest rather than a caching bug factory:
- TTL-bounded (default 5 seconds, NEXUS_CACHE_TTL) — it may be BRIEFLY
  stale, never permanently stale
- hard-invalidated on every write: add_document/delete_document call
  `cache.clear()`; correctness never depends on the TTL
- keyed on the full request signature (query + every ranking-relevant
  parameter), so tweaking a weight can't collide with a cached page
"""
import time
from typing import Optional


class QueryCache:
    """In-process LRU-style cache. TTL=0 disables (testable, documented)."""

    def __init__(self, ttl_seconds: float = 5.0, max_entries: int = 128):
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._items: dict[tuple, tuple[float, object]] = {}
        self._hits = 0
        self._misses = 0

    @staticmethod
    def key_of(query: str, **params) -> tuple:
        """Deterministic cache key from the query and every parameter that
        could change the result ordering/content."""
        items = tuple(sorted((k, v) for k, v in params.items()))
        return (query, *items)

    def get(self, key) -> Optional[object]:
        entry = self._items.get(key)
        if entry is None:
            self._misses += 1
            return None
        ts, value = entry
        if self.ttl <= 0 or (time.time() - ts) > self.ttl:
            del self._items[key]
            self._misses += 1
            return None
        self._hits += 1
        return value

    def set(self, key, value: object) -> None:
        if self.ttl <= 0:
            return
        if len(self._items) >= self.max_entries:
            oldest = min(self._items, key=lambda k: self._items[k][0])
            del self._items[oldest]
        self._items[key] = (time.time(), value)

    def clear(self) -> None:
        self._items.clear()

    def stats(self) -> dict:
        return {"entries": len(self._items), "hits": self._hits,
                "misses": self._misses, "ttl_seconds": self.ttl}
