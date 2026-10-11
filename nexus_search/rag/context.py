"""P7-2 — context selection: pure, deterministic, unit-tested.

Given the rank-ordered `RagChunk` list from retrieval, decide WHICH chunks
enter the prompt and in what order:

1. llm_allowed:false chunks are excluded here (egress control — their text
   never reaches a provider).
2. Per-parent cap: at most N chunks from one parent (default 3) — a single
   long document must not eat the whole context.
3. Near-duplicate suppression: token-Jaccard against already-selected text
   (core/diversity.py's similarity idea), threshold configurable — mirrors
   and boilerplate copies waste budget.
4. Hard budget: `max_context_tokens` (chars-per-token heuristic estimate —
   deliberately NO tokenizer dependency) and `max_context_bytes`. The
   selection can NEVER exceed either; every drop says why.
5. Optional neighbor stitching: the ±1 adjacent chunks of a selected chunk
   (same parent only, fetched through the Storage batched path) may be
   stitched in when the budget allows — the citation still points at the
   selected chunk's own chunk_id.
6. Stable source numbering [1..n] mapped to chunk_id/parent_id.

Determinism: first-fit in retrieval rank order; ties and equal scores never
reorder anything (input order is the tie-break by construction).
"""
import math
from dataclasses import dataclass, field
from typing import Callable, Optional

from .egress import llm_allowed
from .retrieval import RagChunk


def estimate_tokens(text: str, chars_per_token: int = 4) -> int:
    """chars/token heuristic (default 4 ≈ one token per ~4 chars for English
    prose on modern tokenizers). Deliberately no tokenizer dependency —
    the estimate only sizes the context budget, it never gates refusal."""
    if not text:
        return 0
    return math.ceil(len(text) / max(chars_per_token, 1))


def _token_set(text: str) -> frozenset:
    from ..core.tokenizer import tokenize
    return frozenset(tokenize(text))


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass
class SelectedSource:
    """One prompt source: stable number, the cited chunk, and the (possibly
    neighbor-stitched) text that enters the prompt."""
    number: int                  # [1..n], stable
    chunk_id: str
    parent_id: str
    chunk_index: Optional[int]
    title: str
    url: Optional[str]
    text: str                    # the text that enters the prompt
    stitched_with: list[str] = field(default_factory=list)  # neighbor ids


@dataclass
class ContextSelection:
    sources: list[SelectedSource] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)  # {chunk_id, reason}
    stats: dict = field(default_factory=dict)

    @property
    def numbers_by_chunk_id(self) -> dict[str, int]:
        return {s.chunk_id: s.number for s in self.sources}


def select_context(
    chunks: list[RagChunk],
    *,
    max_context_tokens: int = 6000,
    max_context_bytes: int = 24_576,
    chars_per_token: int = 4,
    per_parent_cap: int = 3,
    dedup_threshold: float = 0.85,
    stitch_neighbors: bool = True,
    fetch_neighbors: Optional[Callable[[str, list[int]], dict[int, str]]] = None,
) -> ContextSelection:
    """Budgeted, deduped, per-parent-capped, stably-numbered selection.

    `fetch_neighbors(parent_id, indexes) -> {chunk_index: text}` supplies
    adjacent-chunk text through ONE batched Storage read (the default
    adapter builds `parent#chunkN` ids and batch-fetches them); pass None
    (or stitch_neighbors=False) to disable stitching.

    Invariants (property-tested):
    - the selection NEVER exceeds max_context_tokens or max_context_bytes;
    - every input chunk is either a numbered source or recorded in
      `dropped` with exactly one reason;
    - numbering is 1..n in selection (rank) order, stable and deterministic.
    """
    selection = ContextSelection()
    used_tokens = 0
    used_bytes = 0
    per_parent: dict[str, int] = {}
    selected_sets: list[frozenset] = []
    selected_ids: set[str] = set()
    stitched_ids: set[str] = set()  # neighbors merged into an earlier source

    def _select_text(chunk: RagChunk) -> tuple[str, list[str]]:
        """Neighbor stitching for one selected chunk (budget-checked)."""
        if not stitch_neighbors or fetch_neighbors is None:
            return chunk.text, []
        if chunk.chunk_index is None or not chunk.chunk_count:
            return chunk.text, []
        indexes = [i for i in (chunk.chunk_index - 1, chunk.chunk_index + 1)
                   if 0 <= i < chunk.chunk_count]
        # only neighbors that are NOT already their own numbered sources
        wanted = []
        for i in indexes:
            nid = f"{chunk.parent_id}#chunk{i}"
            if nid not in selected_ids:
                wanted.append(i)
        if not wanted:
            return chunk.text, []
        fetched = fetch_neighbors(chunk.parent_id, wanted)
        pieces = []
        stitched_with = []
        for i in sorted([chunk.chunk_index] + [
                j for j in wanted if j in fetched]):
            if i == chunk.chunk_index:
                pieces.append(chunk.text)
            else:
                pieces.append(fetched[i])
                stitched_with.append(f"{chunk.parent_id}#chunk{i}")
        if len(pieces) == 1:
            return chunk.text, []
        stitched = "\n".join(pieces)
        base_tokens = estimate_tokens(chunk.text, chars_per_token)
        new_tokens = estimate_tokens(stitched, chars_per_token)
        if (used_tokens - base_tokens + new_tokens > max_context_tokens
                or used_bytes - len(chunk.text) + len(stitched) > max_context_bytes):
            return chunk.text, []  # budget says no: keep the bare chunk
        return stitched, stitched_with

    for chunk in chunks:
        if not llm_allowed(chunk.metadata):
            selection.dropped.append({"chunk_id": chunk.chunk_id,
                                      "reason": "llm_allowed"})
            continue
        if chunk.chunk_id in stitched_ids:
            # already inside an earlier source's stitched text
            selection.dropped.append({"chunk_id": chunk.chunk_id,
                                      "reason": "stitched_earlier"})
            continue
        count = per_parent.get(chunk.parent_id, 0)
        if count >= per_parent_cap:
            selection.dropped.append({"chunk_id": chunk.chunk_id,
                                      "reason": "per_parent_cap"})
            continue
        text_set = _token_set(chunk.text)
        if any(jaccard(text_set, s) >= dedup_threshold for s in selected_sets):
            selection.dropped.append({"chunk_id": chunk.chunk_id,
                                      "reason": "near_duplicate"})
            continue
        tokens = estimate_tokens(chunk.text, chars_per_token)
        nbytes = len(chunk.text)
        if used_tokens + tokens > max_context_tokens:
            selection.dropped.append({"chunk_id": chunk.chunk_id,
                                      "reason": "budget_tokens"})
            continue
        if used_bytes + nbytes > max_context_bytes:
            selection.dropped.append({"chunk_id": chunk.chunk_id,
                                      "reason": "budget_bytes"})
            continue
        # the chunk itself fits — commit it, then try to stitch neighbors
        used_tokens += tokens
        used_bytes += nbytes
        per_parent[chunk.parent_id] = count + 1
        selected_sets.append(text_set)
        selected_ids.add(chunk.chunk_id)
        final_text, stitched_with = _select_text(chunk)
        if stitched_with:
            delta_t = estimate_tokens(final_text, chars_per_token) - tokens
            delta_b = len(final_text) - nbytes
            used_tokens += delta_t
            used_bytes += delta_b
            stitched_ids.update(stitched_with)
        selection.sources.append(SelectedSource(
            number=len(selection.sources) + 1,
            chunk_id=chunk.chunk_id,
            parent_id=chunk.parent_id,
            chunk_index=chunk.chunk_index,
            title=chunk.title,
            url=chunk.url,
            text=final_text,
            stitched_with=stitched_with,
        ))

    selection.stats = {
        "sources": len(selection.sources),
        "dropped": len(selection.dropped),
        "context_tokens_estimated": used_tokens,
        "context_bytes": used_bytes,
        "max_context_tokens": max_context_tokens,
        "max_context_bytes": max_context_bytes,
        "drop_reasons": sorted({d["reason"] for d in selection.dropped}),
    }
    return selection


def make_storage_neighbor_fetcher(storage) -> Callable[[str, list[int]], dict[int, str]]:
    """The production neighbor fetcher: ONE batched Storage.get_documents
    for `parent#chunkN` ids (never per-neighbor reads — the N+1 rule).

    EGRESS CONTRACT: `fetch_neighbors` callables must never return the text
    of a chunk whose metadata says llm_allowed:false — this fetcher enforces
    it (stitching is a prompt path, so the egress filter applies to it
    exactly as it applies to selected chunks)."""
    def fetch(parent_id: str, indexes: list[int]) -> dict[int, str]:
        ids = [f"{parent_id}#chunk{i}" for i in indexes]
        docs = storage.get_documents(ids)
        out: dict[int, str] = {}
        for i in indexes:
            doc = docs.get(f"{parent_id}#chunk{i}")
            if doc is not None and llm_allowed(doc.metadata):
                out[i] = doc.content
        return out
    return fetch


if __name__ == "__main__":  # pragma: no cover
    print(select_context([]).stats)

