"""Phase 5 — Advanced Ranking package.

Query understanding (ranking.query), ranking signals/features
(ranking.features, ranking.signals), the weighted re-ranker
(ranking.ranker), A/B instrumentation (ranking.ab), and Search UX helpers
(ranking.suggestions). Everything here is offline and stdlib-only, matching
the rest of the project's philosophy: heuristics are honest heuristics, and
signals that have no real data source yet are placeholders that say so.
"""
from .query import (
    QueryUnderstanding, SynonymMap, normalize_query, correct_spelling,
    expand_synonyms, detect_language, detect_intent, extract_entities,
    understand_query,
)
from .features import RankingFeatures, SignalContext, build_context, extract_features
from .ranker import RankedResult, RankingModel, RankingWeights, WeightedSumModel, rerank
from .ab import ExperimentLog, assign_variant

__all__ = [
    "QueryUnderstanding", "SynonymMap", "normalize_query", "correct_spelling",
    "expand_synonyms", "detect_language", "detect_intent", "extract_entities",
    "understand_query",
    "RankingFeatures", "SignalContext", "build_context", "extract_features",
    "RankedResult", "RankingModel", "RankingWeights", "WeightedSumModel", "rerank",
    "ExperimentLog", "assign_variant",
]
