"""Phase 6 — Link Intelligence: the web graph layer.

Offline signals ("how much does the web vouch for this page") feeding the
Phase 5 ranker's source_authority/popularity slots. Nothing here runs on the
request path; see docs/PHASE6_PLAN.md for the design review.
"""
from .graph import LinkEdge, LinkGraph
from .authority import AuthorityStats, compute_authority

__all__ = ["LinkEdge", "LinkGraph", "AuthorityStats", "compute_authority"]
