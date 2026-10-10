"""Phase 5 Stage 3 — the re-ranker.

This stage RE-RANKS Phase 4 hybrid results; it never re-runs retrieval.
Candidate generation stays in HybridSearch.search_page() — the ranker takes
that page, computes Stage 2 features per candidate, and blends them with
configurable weights.

Learning-to-rank: what ships is the FRAMEWORK (RankingModel ABC +
WeightedSumModel), not a trained model — there is no labeled click/relevance
data in this codebase to train or honestly evaluate one against (SPEC.md:
honesty over completeness theater). WeightedSumModel is a real linear model;
RankingModel is the seam where a trained model slots in once labels exist.

Correctness invariant (tested): weights that zero every non-retrieval signal
reproduce the input ranking EXACTLY, because bm25_score/semantic_similarity
features are the already-computed per-source fusion contributions
(HybridSearchResult.bm25_normalized/vector_normalized with debug=True), so
WeightedSumModel(weights=(1,1,0..)) returns the RRF/weighted fused score
unchanged. Re-deriving normalized scores here instead would break that.
"""
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from ..core.hybrid_search import HybridSearchResult, SearchMode  # noqa: F401  (typing)
from ..core.storage import Storage
from .features import RankingFeatures, SignalContext, build_context, extract_features
from .query import QueryUnderstanding

logger = logging.getLogger("nexus_search.ranker")


@dataclass
class RankingWeights:
    """One float per Stage 2 signal. Defaults keep retrieval dominant —
    opting into the ranker must not silently reorder a Phase 4 result set."""
    bm25_score: float = 1.0
    semantic_similarity: float = 1.0
    title_match: float = 0.2
    url_match: float = 0.05
    phrase_match: float = 0.15
    freshness: float = 0.05
    document_quality: float = 0.05
    content_quality: float = 0.05
    language_relevance: float = 0.05
    source_authority: float = 0.0   # Phase 6 link-graph authority (opt-in)
    popularity: float = 0.0         # Phase 6 link-graph popularity (opt-in)
    click_signal: float = 0.0       # placeholder slot — no data source until Phase 8+
    anchor_relevance: float = 0.0   # Phase 6 inbound-anchor/query overlap (opt-in)

    def as_dict(self) -> dict[str, float]:
        return {f: getattr(self, f) for f in self.__dataclass_fields__}

    @classmethod
    def from_env(cls) -> "RankingWeights":
        """Production rollout knobs (Phase 6): the link-intelligence signals
        ship OFF (0.0) until an operator opts in after evaluating uplift —
        NEXUS_AUTHORITY_WEIGHT / NEXUS_POPULARITY_WEIGHT /
        NEXUS_ANCHOR_WEIGHT / NEXUS_RERANK_WEIGHT_CLICK, each clamped to
        [0,1] so a fat-fingered env value can't zero the whole ranking score.

        NEXUS_RERANK_WEIGHTS (optional) overrides ANY subset of the signal
        weights as a JSON object, e.g. {"title_match": 0.3, "freshness": 0}.
        Unknown keys and non-numeric values are logged and ignored — never a
        boot failure — and every value clamps to [0,1]. When unset (the
        default) nothing changes:         constructor defaults apply."""
        import json
        import os
        weights = cls()
        for env, field_name in (("NEXUS_AUTHORITY_WEIGHT", "source_authority"),
                                 ("NEXUS_POPULARITY_WEIGHT", "popularity"),
                                 ("NEXUS_ANCHOR_WEIGHT", "anchor_relevance"),
                                 ("NEXUS_RERANK_WEIGHT_CLICK", "click_signal")):
            raw = os.environ.get(env, "").strip()
            if not raw:
                continue
            try:
                value = float(raw)
            except ValueError:
                logger.warning("%s=%r is not a float; left at default", env, raw)
                continue
            clamped = max(0.0, min(1.0, value))
            if clamped != value:
                logger.warning("%s=%r clamped to %.2f", env, raw, clamped)
            setattr(weights, field_name, clamped)
        raw = (os.environ.get("NEXUS_RERANK_WEIGHTS") or "").strip()
        if raw:
            try:
                overrides = json.loads(raw)
            except json.JSONDecodeError as exc:
                logger.warning("NEXUS_RERANK_WEIGHTS is not valid JSON (%s); "
                                "constructor defaults apply", exc)
                overrides = None
            if isinstance(overrides, dict):
                for key, value in overrides.items():
                    if key not in weights.__dataclass_fields__:
                        logger.warning("NEXUS_RERANK_WEIGHTS: unknown signal %r "
                                       "ignored", key)
                        continue
                    try:
                        numeric = float(value)
                    except (TypeError, ValueError):
                        logger.warning("NEXUS_RERANK_WEIGHTS: %r=%r is not a "
                                       "float; ignored", key, value)
                        continue
                    clamped = max(0.0, min(1.0, numeric))
                    if clamped != numeric:
                        logger.warning("NEXUS_RERANK_WEIGHTS: %r clamped to %.2f",
                                       key, clamped)
                    setattr(weights, key, clamped)
            elif overrides is not None:
                logger.warning("NEXUS_RERANK_WEIGHTS must be a JSON object; "
                                "constructor defaults apply")
        return weights


@dataclass
class RankedResult:
    """A hybrid result plus its computed features and final blended score."""
    result: HybridSearchResult
    features: RankingFeatures
    final_score: float

    # Convenience pass-throughs so consumers don't reach into .result
    @property
    def doc_id(self) -> str:
        return self.result.doc_id

    @property
    def score(self) -> float:
        return self.final_score


class RankingModel(ABC):
    """The LTR seam. score() takes precomputed features only — a trained
    model (LightGBM etc.) belongs BEHIND this interface, once click/relevance
    labels exist (Phase 8+; no Phase 7 data source is planned). Nothing to
    train on = nothing trained; the framework is the deliverable."""

    @abstractmethod
    def score(self, features: RankingFeatures) -> float:
        """Map a feature vector to a single ranking score."""


class WeightedSumModel(RankingModel):
    """The linear weighted-sum model: score = Σ weight_i × feature_i.

    Features are normalized by construction (all signals return [0, 1], and
    the two retrieval signals carry the fusion's own per-source
    contributions). Available fallback: plain identity when every weight is 0
    (score 0.0 for all — caller's tie-break order then dominates)."""

    def __init__(self, weights: Optional[RankingWeights] = None):
        self.weights = weights or RankingWeights()

    def score(self, features: RankingFeatures) -> float:
        w = self.weights
        f = features
        return (
            w.bm25_score * f.bm25_score
            + w.semantic_similarity * f.semantic_similarity
            + w.title_match * f.title_match
            + w.url_match * f.url_match
            + w.phrase_match * f.phrase_match
            + w.freshness * f.freshness
            + w.document_quality * f.document_quality
            + w.content_quality * f.content_quality
            + w.language_relevance * f.language_relevance
            + w.source_authority * f.source_authority
            + w.popularity * f.popularity
            + w.click_signal * f.click_signal
            + w.anchor_relevance * f.anchor_relevance
        )


def _contribution(result: HybridSearchResult, which: str) -> Optional[float]:
    """Per-source fusion contribution when available (hybrid debug rows),
    else the raw per-source score. None when this retriever didn't hit."""
    contrib = result.bm25_normalized if which == "bm25" else result.vector_normalized
    if contrib is not None:
        return contrib
    raw = result.bm25_score if which == "bm25" else result.vector_score
    return raw


def rerank(
    results: list[HybridSearchResult],
    query: str,
    weights: Optional[RankingWeights] = None,
    *,
    storage: Optional[Storage] = None,
    understanding: Optional[QueryUnderstanding] = None,
    model: Optional[RankingModel] = None,
    link_intel=None,
) -> list[RankedResult]:
    """Re-rank Phase 4 results with Stage 2 features.

    storage=None is legal (offline/unit tests): document-dependent signals
    read NEUTRAL/0.0 instead of crashing; link_intel=None likewise leaves
    authority/popularity at NEUTRAL (Phase 6 graph absent). The sort is
    deterministic: (-final_score, doc_id), same as bm25.py/hybrid_search.py.
    """
    model = model or WeightedSumModel(weights)
    context = build_context(query, understanding=understanding, link_intel=link_intel)

    # One fetch for the whole page — extract_features is per-candidate, its
    # storage read was the N+1.
    doc_cache: dict = {}
    if storage is not None:
        doc_cache = storage.get_documents([r.doc_id for r in results])

    ranked: list[RankedResult] = []
    for result in results:
        ctx = SignalContext(
            query=query,
            understanding=understanding,
            now=context.now,
            freshness_half_life_days=context.freshness_half_life_days,
            bm25_contribution=_contribution(result, "bm25"),
            vector_contribution=_contribution(result, "vector"),
            default_authority=context.default_authority,
            default_popularity=context.default_popularity,
            default_click=context.default_click,
            link_intel=context.link_intel,  # MUST flow to per-result contexts
        )
        if storage is not None:
            features = extract_features(result.doc_id, storage, ctx,
                                        doc=doc_cache.get(result.doc_id))
        else:
            features = extract_features(result.doc_id, _NullStorage(), ctx)
        ranked.append(RankedResult(result=result, features=features,
                                   final_score=model.score(features)))

    ranked.sort(key=lambda r: (-r.final_score, r.doc_id))
    return ranked


class _NullStorage:
    """Stand-in when rerank() is called without storage: every doc lookup
    misses, and signals degrade to their documented neutral/zero behavior."""

    def get_document(self, doc_id):
        return None
