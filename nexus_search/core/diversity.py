"""MMR-style result diversification (Phase 1 build-out #6).

What exists today: chunk-grouping already guarantees one result per parent
document. It does NOT stop a family of near-duplicate DOCUMENTS (seeded
crawl mirrors, copies with different ids) from filling the whole first page,
pushing distinct content off it.

This pass is Maximal Marginal Relevance: greedily pick the next result as
(score − λ × max similarity to anything already chosen). Similarity is
token-Jaccard over title+content — no embeddings, no extra dependency (the
ranking package's Phase 5 signals do real feature work; this is a cheap
anti-flooding valve).

It is OPT-IN (diversity=0.0 → candidates pass through byte-identical).
When on, the number of near-duplicates is bounded by their marginal gain
falling below alternatives — not by a hard dumb cap, so a diverse corpus
with ONE dominant topic still fills the page.
"""
from dataclasses import dataclass
from typing import Callable, Optional


def _token_set(text: str) -> frozenset[str]:
    from .tokenizer import tokenize
    return frozenset(tokenize(text))


@dataclass
class DiverseResult:
    """A result plus why it scored as it did for diversification."""
    result: object          # SearchResult / HybridSearchResult passthrough
    final_score: float      # score after MMR penalty
    similarity_penalty: float  # max Jaccard to any previously chosen result


def mmr_select(
    results: list,
    doc_text: Callable[[object], str],
    lambda_: float = 0.5,
    max_results: Optional[int] = None,
    similarity_threshold: float = 1.0,
) -> list[DiverseResult]:
    """Greedy MMR selection.

    - `results` must be pre-ranked by score (input order = relevance order of
      ties, which keeps pre-existing deterministic tie-breaks on farms of
      identical items)
    - `doc_text` extracts each result's similarity text (title + content)
    - `lambda_` in [0,1]: relevance weight. lambda=1.0 ⇒ pure relevance order;
      lambda=0.0 ⇒ pure novelty. We expose this as diversity=1−lambda_.
    - `similarity_threshold`: above this Jaccard vs. an already-chosen result,
      a candidate is SKIPPED outright unless nothing else qualifies (hard
      anti-flood for exact mirrors; 1.0 disables the hard skip)

    Ordering guarantee that matters: with similarity_threshold=1.0,
    lambda_=1.0 the output order equals the input order exactly.
    """
    if max_results is None:
        max_results = len(results)
    chosen: list[DiverseResult] = []
    chosen_sets: list[frozenset] = []
    remaining = list(results)
    # Tokenize each candidate ONCE — similarity is recomputed against the
    # chosen set every round, but the text work is not (and doc_text may hit
    # storage: O(n) fetches total, not O(n²)).
    remaining_sets = [_token_set(doc_text(c)) for c in results]

    while remaining and len(chosen) < max_results:
        # score candidates against chosen
        best_idx, best_pen, best_score = None, 0.0, float("-inf")
        skipped: list[int] = []
        for i, cand in enumerate(remaining):
            cand_set = remaining_sets[i]
            max_sim = 0.0
            for s in chosen_sets:
                if not cand_set or not s:
                    continue
                inter = len(cand_set & s)
                uni = len(cand_set | s) or 1
                sim = inter / uni
                if sim > max_sim:
                    max_sim = sim
            if max_sim >= similarity_threshold and similarity_threshold < 1.0:
                skipped.append(i)
                continue
            # standard MMR: relevance minus diversity penalty
            merged_score = lambda_ * cand.score - (1 - lambda_) * max_sim
            if merged_score > best_score:
                best_idx, best_pen, best_score = i, max_sim, merged_score

        if best_idx is None:
            # Everything left is above the hard threshold. Do NOT refill the
            # page with duplicates (that would defeat the flood guard); the
            # ONLY re-admission case is "the page would otherwise stay empty".
            if not chosen and skipped:
                chosen_sets.append(remaining_sets[skipped[0]])
                cand = remaining.pop(skipped[0])
                chosen.append(DiverseResult(cand, cand.score, 1.0))
            break

        chosen_sets.append(remaining_sets.pop(best_idx))
        cand = remaining.pop(best_idx)
        chosen.append(DiverseResult(cand, best_score, best_pen))

    return chosen
