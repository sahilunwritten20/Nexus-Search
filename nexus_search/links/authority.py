"""Offline authority computation (Phase 6): PageRank + anti-farm corrections.

Runs ONLY off the request path (CLI/scheduled recompute). Reads the durable
edge set from LinkGraph, computes PageRank with the edge-weight corrections
from docs/PHASE6_PLAN.md (nofollow passes nothing, reciprocal pairs are
discounted, self-links dropped), applies the domain-diversity factor, and
writes scores via a shadow table swapped in one transaction — a crash can
never leave half-written scores behind.

Proven-not-perfumed: tests/links/ asserts the mission's exact success shape
(clique vs. diverse inlinks vs. orphan) computes the right ordering.
"""
import logging
import math
import sqlite3
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import numpy as np

from .graph import LinkGraph

logger = logging.getLogger("nexus_search.links.authority")

DAMPING = 0.85
MAX_ITERATIONS = 30
CONVERGENCE_TOL = 1e-6
RECIPROCAL_FACTOR = 0.25  # mutual admiration discount, both directions

# rel values that mark an edge as not-an-endorsement (SEO convention)
_NON_ENDORSING = ("nofollow", "sponsored", "ugc")


@dataclass
class AuthorityStats:
    pages: int
    edges_in: int
    edges_used: int
    reciprocal_pairs: int
    iterations: int
    converged: bool
    seconds: float


def _endorses(rel_attrs: str) -> bool:
    rel = rel_attrs.lower()
    return not any(token in rel for token in _NON_ENDORSING)


def compute_authority(graph: LinkGraph) -> AuthorityStats:
    """Recompute authority/popularity for every known URL. Safe on any graph
    shape (empty, singleton, cycles, disconnected); scores replace shadow-swap
    atomically at the end."""
    started = time.time()
    edges = graph.edges()
    stats = AuthorityStats(pages=0, edges_in=len(edges), edges_used=0,
                           reciprocal_pairs=0, iterations=0, converged=False,
                           seconds=0.0)

    endorsing = {}  # (from, to) -> None, dedup by PK
    for e in edges:
        if e.from_url == e.to_url or not _endorses(e.rel_attrs):
            continue
        endorsing[(e.from_url, e.to_url)] = True

    urls = sorted({u for pair in endorsing for u in pair})
    n = len(urls)
    # scores table is rebuilt even on empty input (clears stale rows), but a
    # graph with no endorsing edges yields NEUTRAL-by-omission downstream
    if n == 0:
        _write_scores(graph, {}, seconds=time.time() - started)
        stats.seconds = time.time() - started
        return stats

    idx = {u: i for i, u in enumerate(urls)}
    src, dst, wts = [], [], []
    seen_pairs = set(endorsing)
    for (a, b) in sorted(endorsing):
        w = 1.0
        if (b, a) in seen_pairs:
            w *= RECIPROCAL_FACTOR
            stats.reciprocal_pairs += 1
        src.append(idx[a]); dst.append(idx[b]); wts.append(w)
    stats.edges_used = len(src)
    stats.reciprocal_pairs //= 2  # counted each direction

    src = np.asarray(src, dtype=np.int64)
    dst = np.asarray(dst, dtype=np.int64)
    wts = np.asarray(wts, dtype=np.float64)

    # per-source total outflow weight (dangling sources get zero here)
    out = np.zeros(n, dtype=np.float64)
    np.add.at(out, src, wts)
    safe_out = np.where(out == 0.0, 1.0, out)

    pr = np.full(n, 1.0 / n)
    teleport = (1.0 - DAMPING) / n
    converged = False
    it = 0
    for it in range(1, MAX_ITERATIONS + 1):
        contribution = pr[src] * wts / safe_out[src]
        new = np.full(n, teleport)
        # dangling nodes: their mass redistributes to everyone
        dangling_mass = pr[out == 0.0].sum()
        np.add.at(new, dst, DAMPING * contribution)
        new += DAMPING * dangling_mass / n
        delta = np.abs(new - pr).sum()
        pr = new
        if delta < CONVERGENCE_TOL:
            converged = True
            break

    # per-URL inbound stats (endorsing edges only) + domain diversity
    in_count = np.zeros(n, dtype=np.int64)
    in_domains: list[set] = [set() for _ in range(n)]
    for (a, b) in endorsing:
        i = idx[b]
        in_count[i] += 1
        in_domains[i].add((urlsplit(a).hostname or "").lower())

    scores: dict[str, tuple[float, float, int, int, float]] = {}
    # normalize PR to a comparable [0,1]-ish range for the ranker: divide by
    # the max (top page = 1.0, everything else relative — robust across graphs)
    max_pr = float(pr.max()) if n else 1.0
    for u, i in idx.items():
        diversity = min(len(in_domains[i]) / 10.0, 1.0)
        authority = (float(pr[i]) / max_pr) * (0.5 + 0.5 * diversity)
        popularity = math.log1p(len(in_domains[i])) / math.log1p(100.0)
        scores[u] = (authority, popularity, int(in_count[i]),
                     len(in_domains[i]), float(pr[i]))
    _write_scores(graph, scores, seconds=time.time() - started)
    stats.pages = n
    stats.iterations = it
    stats.converged = converged
    stats.seconds = time.time() - started
    return stats


def _write_scores(graph: LinkGraph, scores: dict, seconds: float) -> None:
    """Shadow-swap the scores table: readers never see a partial rewrite."""
    now = time.time()
    with graph.lock:
        conn = graph.conn
        conn.execute("DROP TABLE IF EXISTS authority_scores_shadow")
        conn.execute("""
            CREATE TABLE authority_scores_shadow (
                url TEXT PRIMARY KEY, pagerank REAL NOT NULL,
                inlink_count INTEGER NOT NULL, inlink_domains INTEGER NOT NULL,
                authority REAL NOT NULL, popularity REAL NOT NULL,
                computed_at REAL NOT NULL)""")
        if scores:
            conn.executemany(
                "INSERT INTO authority_scores_shadow (url, pagerank, inlink_count, "
                "inlink_domains, authority, popularity, computed_at) VALUES (?,?,?,?,?,?,?)",
                [(u, pr, ic, idom, auth, pop, now)
                 for u, (auth, pop, ic, idom, pr) in scores.items()])
        conn.commit()  # shadow complete — Python begins an implicit txn on DDL
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("ALTER TABLE authority_scores RENAME TO authority_scores_old")
            conn.execute("ALTER TABLE authority_scores_shadow RENAME TO authority_scores")
            conn.execute("DROP TABLE authority_scores_old")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
