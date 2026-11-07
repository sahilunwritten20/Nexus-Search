"""Phase 5 — Advanced Ranking package.

Query understanding (ranking.query), ranking signals/features
(ranking.features, ranking.signals), the weighted re-ranker
(ranking.ranker), A/B instrumentation (ranking.ab), and Search UX helpers
(ranking.suggestions). Everything here is offline and stdlib-only, matching
the rest of the project's philosophy: heuristics are honest heuristics, and
signals that have no real data source yet are placeholders that say so.
"""

from .ab import ExperimentLog, assign_variant
from .features import RankingFeatures, SignalContext, build_context, extract_features
from .query import (
    QueryUnderstanding,
    SynonymMap,
    correct_spelling,
    detect_intent,
    detect_language,
    expand_synonyms,
    extract_entities,
    normalize_query,
    understand_query,
)
from .ranker import RankedResult, RankingModel, RankingWeights, WeightedSumModel, rerank

__all__ = [
    'ExperimentLog',
    'QueryUnderstanding',
    'RankedResult',
    'RankingFeatures',
    'RankingModel',
    'RankingWeights',
    'SignalContext',
    'SynonymMap',
    'WeightedSumModel',
    'assign_variant',
    'build_context',
    'correct_spelling',
    'detect_intent',
    'detect_language',
    'expand_synonyms',
    'extract_entities',
    'extract_features',
    'normalize_query',
    'rerank',
    'understand_query',
]
