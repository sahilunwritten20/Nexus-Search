"""Score/feature normalization — the ONE home for this math in Nexus Search.

`hybrid_search` (Phase 4, weighted fusion) and the Phase 5 ranker both used
to normalize independently; keeping two subtly-drifting implementations is
how ranking bugs are born, so both now import from here. The semantics are
exactly the pre-existing hybrid_search ones.
"""
import math


def min_max_normalize(scores: dict[str, float]) -> dict[str, float]:
    """Min-max normalize a score pool to [0, 1].

    All-equal pools collapse to 1.0 when positive, 0.0 otherwise (a flat
    non-zero signal should stay visible, not vanish)."""
    if not scores:
        return {}
    values = list(scores.values())
    min_score = min(values)
    max_score = max(values)
    if max_score == min_score:
        return {k: (1.0 if v > 0 else 0.0) for k, v in scores.items()}
    return {k: (v - min_score) / (max_score - min_score) for k, v in scores.items()}


def min_max_normalize_list(values: list[float]) -> list[float]:
    """List form of min_max_normalize (same collapse rule), order preserved."""
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi == lo:
        return [1.0 if v > 0 else 0.0 for v in values]
    return [(v - lo) / (hi - lo) for v in values]


def zscore_normalize(values: list[float]) -> list[float]:
    """Z-score normalization (mean 0, std 1). A constant pool z-scores to all
    zeros — a flat signal carries no ranking information, unlike the min-max
    collapse rule which preserves a positive constant's visibility."""
    if not values:
        return []
    n = len(values)
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    std = math.sqrt(var)
    if std == 0:
        return [0.0 for _ in values]
    return [(v - mean) / std for v in values]


def zscore_normalize_list(values: list[float]) -> list[float]:
    """Z-score normalize (mean 0, std 1). Zero-variance pools collapse to 0.0 —
    a flat signal carries no ranking information and should not fire."""
    if not values:
        return []
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    std = math.sqrt(var)
    if std == 0:
        return [0.0] * len(values)
    return [(v - mean) / std for v in values]
