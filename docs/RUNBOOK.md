# Runbook — deploy, operate, recover

Every procedure here was EXECUTED at least once by the review audit
(evidence in docs/REVIEW_WORKLOG.md) or is covered by a committed test.

## Deploy

``` bash
# 1. Config: copy .env.example, set AT MINIMUM NEXUS_API_KEY (boot
#    refuses without it in production mode) and NEXUS_DB.
# 2. Container path (non-root image; model pre-baked under /opt/hf):
docker compose up          # compose injects every NEXUS_* from .env

# 3. Bare-metal path:
PIP_EXTRA_INDEX_URL=https://download.pytorch.org/whl/cpu \
  pip install -r requirements.txt -r requirements-dev.txt
NEXUS_API_KEY=... uvicorn nexus_search.core.api:app --host 0.0.0.0
```
Single process per SQLite file (documented design). Readiness gate for
load balancers: `GET /ready` (503 until storage + vector subsystem are
usable). `GET /health` carries nuance (degraded modes, coverage).

## Rollback (authority / rerank weights)

- `NEXUS_AUTHORITY_WEIGHT` / `NEXUS_POPULARITY_WEIGHT` /
  `NEXUS_ANCHOR_WEIGHT` / `NEXUS_RERANK_WEIGHT_CLICK`: set to 0 +
  restart. Signals are additive; ranking returns to pure retrieval.
  No reindex, no migration. (Zero-weight identity is test-pinned.)
- `NEXUS_EMBED_BATCH`, `NEXUS_RAG_*`, spell budgets: env-only, restart
  to apply.
- `NEXUS_STEMMING` changes the TOKENIZER: requires
  `python -m nexus_search.core.reindex --db $NEXUS_DB --shadow`
  (zero-downtime swap; replays in-flight writes).

## Backup / restore (executed + verified by the audit)

``` bash
python -m nexus_search.core.backup --db $NEXUS_DB --out backups/ --with-frontier
# restore: STOP the writer, copy the snapshot back over NEXUS_DB, start.
```
Verified: online snapshot under WAL, row counts + doc-id sets equal,
`PRAGMA integrity_check` ok on both sides (worklog S1.14).

## Key rotation

1. Set the new `NEXUS_API_KEY`; 2. restart (or roll instances); clients
   with the old key get 401. There is exactly ONE key (no keyring) —
   rotating is env + restart. LLM key: `NEXUS_LLM_API_KEY` env-only;
   never in logs (poisoned-key tests enforce).

## Data retention (SCHEDULE THESE)

``` bash
python -m nexus_search.ranking.ab purge --db $NEXUS_DB --days 30   # A/B raw query text
python -m nexus_search.rag ... # session/usage pruning:
#   SqliteSessionStore: storage.rag_session_prune(ttl)  (scope-paired, max-activity rule)
#   rag_usage_prune(keep_days=14)                      (cost rows only)
python -m nexus_search.core.vector_maintenance $NEXUS_DB            # re-embed backfill + GC orphans
```

## Incident triage

| Symptom | First checks | Likely fixes |
|---|---|---|
| /health degraded, coverage < 0.9 | embedding sync stats in /metrics (errors, queue) | run vector_maintenance (backfill); check embedder env |
| /search latency spike | /metrics query_cache hit rate; NEXUS_MAX_CANDIDATES; huge offset windows (bounded by contract) | raise cache TTL; page via cursor; check corpus growth |
| /ask 503 "rag subsystem absent/disabled" | boot policy: key, provider name, NEXUS_RAG_ALLOW_STUB, kill switch | fix env; /health rag block names the reason |
| /ask 429 "budget exceeded" | usage rows in rag_token_usage (UTC day keys) | raise NEXUS_RAG_DAILY_TOKEN_BUDGET or wait for UTC midnight (Retry-After states it) |
| "unverifiable_citations" refusals rising | /metrics citation_verification_failures | provider model drift or citation layer regression — check gate_floors.json + model name |
| Crawler hammering a host | frontier DB + politeness crawl-delay logs | blocklist the host (`crawler.cli block host --reason`); verify NEXUS_TRUST_PROXY only behind real proxies |
| SQLite "database is locked" | second writer process attached to the same DB? | one process per DB file (design); restart the stray |
| Crash mid-crawl | frontier requeues in_progress/skipped at open (tested) | restart the crawl; kill -9 consistency suite proves no orphan postings |

## Reindex / rebuild

``` bash
python -m nexus_search.core.reindex --db $NEXUS_DB --shadow       # postings
python -m nexus_search.core.vector_maintenance $NEXUS_DB           # vectors
python -m nexus_search.crawler.cli normalize-links --db $NEXUS_DB --recompute  # graph URL space
python -m nexus_search.crawler.cli authority --recompute --db $NEXUS_DB        # scores
```

## Capacity planning (measured)

- Graph: 10k pages / 100k edges recompute 3.2s (30s budget; smoke mode
  in CI). Scale benchmark: selective hybrid ~27ms @10k docs (hash
  embedder). SQLite-scale limits acknowledged by SPEC.md — beyond it,
  Phase 8 (shared store) is the documented path.
- RAG: p50/p95 bounded by the provider timeout (<=30s hard); budget =
  NEXUS_LLM_PRICE_TABLE absent -> cost null (honest).
