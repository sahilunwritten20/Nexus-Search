"""One-shot WP7 doc updates (script so it's repeatable + reviewable)."""
import re

# ---------------- README.md ----------------
p = "README.md"
src = open(p, encoding="utf-8").read()

old = "| `NEXUS_EMBEDDER` | *(auto)*       | `hash:384` (offline) or `st:all-MiniLM-L6-v2` |"
new = ("| `NEXUS_EMBEDDER` | `hash:384`  | offline-safe default; set `st:sentence-transformers/all-MiniLM-L6-v2` for real semantics (model pre-baked in the image) |")
assert old in src, "embedder row"
src = src.replace(old, new)

old = """**Embedder default is lexical, not semantic.** The default
`NEXUS_EMBEDDER=hash:384` (also the docker-compose default) is a deterministic
offline hasher"""
new = """**Embedder default is lexical, not semantic.** `NEXUS_EMBEDDER` now
defaults to `hash:384` in code, compose AND this table (they agree since the
audit): a deterministic offline hasher"""
assert old in src, "lexical paragraph"
src = src.replace(old, new)

# new env vars in the table (after the authority weight rows already added)
old = "| `NEXUS_RATE_LIMIT` | `60/minute`  | Per-client limit on `/search` and write endpoints (`0` disables) |"
new = """| `NEXUS_RATE_LIMIT` | `60/minute`  | Per-client limit on `/search` and write endpoints (`0` disables) |
| `NEXUS_TRUST_PROXY` | *(off)*     | `N`: rate-limit key on the Nth-from-right `X-Forwarded-For` entry (reverse-proxy deployments); off = spoofed XFF ignored |
| `NEXUS_ANCHOR_WEIGHT` | `0.0`     | Phase 6: inbound-anchor/query-overlap weight in rerank (same rollout/rollback as authority) |
| `NEXUS_AUTHORITY_RECOMPUTE_INTERVAL` | `0` | Seconds between background PageRank passes (`0` = off; CLI recompute unaffected) |
| `NEXUS_DOMAIN_AUTHORITY_FALLBACK` | `0` | `1`: unknown URLs fall back to their domain's aggregate score |
| `NEXUS_RERANK_WEIGHTS` | *(unset)*    | JSON object overriding any subset of rerank signal weights, clamped [0,1] (e.g. `{"title_match":0.3}`) |
| `NEXUS_SPELL_MAX_TERM_LEN` etc. | see `.env.example` | Spell-correction bounds (BUG-01): term 20, dist-2 12, 8 corrections/query, 4000-comparison budget |
| `NEXUS_MAX_QUERY_TERMS` | `128`       | Max positive terms per query (extras dropped deterministically) |
| `NEXUS_MAX_CANDIDATES` | `1000`       | Fused-pool hard bound; `offset+top_k` past it is a clear 400 |"""
assert old in src, "rate-limit row anchor"
src = src.replace(old, new)

# ops: backup + dead-links/orphans + A/B purge
old = """**Rollout:** set `NEXUS_AUTHORITY_WEIGHT=0.2` (and optionally
`NEXUS_POPULARITY_WEIGHT=0.1`) + restart."""
new = """Graph ops: `python -m nexus_search.crawler.cli dead-links|orphans --db $NEXUS_DB`
(read-only reports). Backups: `python -m nexus_search.core.backup --db $NEXUS_DB
--out backups/ --with-frontier` (consistent snapshot under WAL; restore = copy
back while the writer is stopped). A/B log retention:
`python -m nexus_search.ranking.ab purge --db $NEXUS_DB --days 30`.

**Rollout:** set `NEXUS_AUTHORITY_WEIGHT=0.2` (and optionally
`NEXUS_POPULARITY_WEIGHT=0.1`) + restart."""
assert old in src, "rollout anchor"
src = src.replace(old, new)

open(p, "w", encoding="utf-8").write(src)
print("README updated")

# ---------------- .env.example ----------------
p = ".env.example"
src = open(p, encoding="utf-8").read()
old = "# A/B query log: rows never expire automatically. Schedule a retention purge"
new = """# Reverse-proxy deployments only: N = number of trusted proxy hops; the
# rate limiter then keys on the Nth-from-right X-Forwarded-For entry.
# OFF by default — an untrusted XFF header must never forge a bucket.
# NEXUS_TRUST_PROXY=1

# JSON object overriding any subset of rerank signal weights (clamped [0,1]);
# unknown keys/non-numeric values are logged and ignored, never a boot failure.
# NEXUS_RERANK_WEIGHTS={"title_match":0.3,"freshness":0}

# A/B query log: rows never expire automatically. Schedule a retention purge"""
assert old in src, "env.example anchor"
src = src.replace(old, new)
open(p, "w", encoding="utf-8").write(src)
print(".env.example updated")

# ---------------- PHASE4_PLAN banner ----------------
p = "docs/PHASE4_PLAN.md"
src = open(p, encoding="utf-8").read()
if not src.startswith("> **Historical plan"):
    src = ("> **Historical plan — superseded internals.** This document is the\n"
           "> Phase 4 design as written before implementation. The shipped system\n"
           "> differs deliberately: one `doc_vectors` table replaced the planned\n"
           "> `embeddings`+`vectors` pair, RRF fusion was added alongside weighted\n"
           "> fusion, and the debug surface became the key-gated POST /search/explain\n"
           "> plus /search?debug=true. Current design: nexus_search/core/ and\n"
           "> docs/CHANGELOG_UPGRADE.md.\n\n" + src)
    open(p, "w", encoding="utf-8").write(src)
print("PHASE4_PLAN banner added")

# ---------------- SPEC.md honesty rows ----------------
p = "SPEC.md"
src = open(p, encoding="utf-8").read()
old = "| 5 | Ranking | **Done + partial**"
assert old in src, "SPEC row 5"
src = src.replace(old, "| 5 | Ranking | **Done** — LTR framework + weighted-sum model (no trained model: no labeled data exists yet); A/B: instrumentation (bucketing + query log); analysis needs real traffic. Click signals remain a placeholder until Phase 7 |")
old = "| 6 | Link intelligence | **Done + partial**"
assert old in src, "SPEC row 6"
src = src.replace(old, "| 6 | Link intelligence | **Done** — link graph (normalized node identity), PageRank authority, anti-farm heuristics, traversal/components/dead-link/orphan reports, anchor-relevance + domain-authority signals (weights default 0.0), background recompute + version-guarded incremental recompute. No ML spam classifier (heuristics only, stated); authority quality untested against web-scale spam |")
open(p, "w", encoding="utf-8").write(src)
print("SPEC.md updated")
