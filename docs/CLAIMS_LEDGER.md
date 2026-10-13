# Claims Ledger — review/hardening

Every claim from ROADMAP/README/SPEC/docs/code comments/.env.example.
Result: TRUE / FALSE / PARTIAL / UNVERIFIABLE. FALSE|PARTIAL => finding ID in REVIEW_CHECKLIST.md.

Legend: source = file:line. verify = command/test that produced the evidence.

## README.md

| # | Claim | Source | How verified | Result | Evidence |
|---|-------|--------|--------------|--------|----------|
| R-1 | "844 passed, 7 skipped, 614 subtests" offline | README.md:165 | 3x full pytest runs, fresh venv | PENDING | worklog |
| R-2 | "849 passed, 2 skipped" with NEXUS_RUN_MODEL_TESTS=1 | README.md:168 | model-gated run | PENDING | - |
| R-3 | NEXUS_ENV != dev refuses boot without NEXUS_API_KEY | README.md:43 | boot probe | PENDING | - |
| R-4 | Auth required POST/DELETE /documents; 401 without key | README.md:44 | curl unauth | PENDING | - |
| R-5 | GET /search bounds: 2000 chars, 128 words -> 400 | README.md:94-96 | API probe | PENDING | - |
| R-6 | /metrics key-gated | README.md:93 | curl unauth | PENDING | - |
| R-7 | Rate limit 60/minute default on /search + writes | README.md:49 | limiter test/probe | PENDING | - |
| R-8 | Reindex --shadow zero-downtime | README.md:101-103 | test + read code | PENDING | - |
| R-9 | Backups: consistent snapshot under WAL | README.md:122-124 | execute backup+restore | PENDING | - |
| R-10 | Embedder default hash:384 (code+compose+table agree) | README.md:178-183 | grep defaults | PENDING | - |
| R-11 | Stub outside dev requires NEXUS_RAG_ALLOW_STUB=1 | README.md:79 | boot probe | PENDING | - |
| R-12 | NEXUS_RAG_ENABLED=0 -> /ask 503, /health rag disabled | README.md:78 | kill-switch test | PENDING | - |
| R-13 | LLM_TIMEOUT clamped <=30s | README.md:74 | read config + test | PENDING | - |
| R-14 | Retries: connect/429/5xx only; read timeouts fail fast | README.md:75 | provider tests | PENDING | - |
| R-15 | Concurrency semaphore -> 429 + Retry-After | README.md:76 | provider test | PENDING | - |
| R-16 | Price table unset -> cost null | README.md:77 | ask test | PENDING | - |
| R-17 | Context budgets never exceeded; drops reported | README.md:81 | context tests | PENDING | - |
| R-18 | llm_allowed:false never reaches provider | README.md:543 | capture stub egress test | PENDING | - |
| R-19 | Gate: 83.3% unanswerable refusal @ 5.9% false refusal held-out | README.md:554-558 | rerun calibration held-out | PENDING | - |
| R-20 | CI floors: invented citations 0, injection 0, exfil 0, leak 0 | README.md:582-585 | benchmark run | PENDING | - |
| R-21 | Sessions cross-key isolation mutation-killed | README.md:576-577 | mutation check tests | PENDING | - |
| R-22 | Budgets hold across 2 workers (shared SQLite) | README.md:571-572 | two-instance test | PENDING | - |
| R-23 | Background PageRank worker clean shutdown | README.md:513-514 | lifespan test | PENDING | - |
| R-24 | Graph benchmark 10k/100k recompute 3.2s vs 30s bound | README.md:517-518 | run benchmark | PENDING | - |
| R-25 | Authority weight default 0.0 inert (zero-weight identity pinned) | README.md:47,490-491 | rerank test | PENDING | - |
| R-26 | DNS pinning: pinned IP is the one connected | README.md:65 (fetcher) | read code + tests | PENDING | - |
| R-27 | .env.example documents all vars (lint-enforced, 61 knobs) | README.md:591 | env-check script | PENDING | - |
| R-28 | Docker image non-root, model baked to /opt/hf | README.md:148-150 | Dockerfile read/build | PENDING | - |
| R-29 | Phase 6 "every bullet matches shipped code" with tests | README.md:466-468 | per-bullet trace | PENDING | - |
| R-30 | Per-file test counts (Phase 3/4/5 sections) | README.md:326-453 | pytest --collect-only | PENDING | - |
| R-31 | "tests/golden/keyword_baseline.json unchanged by allowlist" | README.md:105-108 | golden test run | PENDING | - |
| R-32 | Requirements split: runtime never imports dev deps | requirements.txt:34-36 | grep imports | PENDING | - |
| R-33 | networkx in venv but not in requirements* | venv pip list | grep usage | PENDING | - |

## SPEC.md / docs/*.md / code comments

(to be populated as files are read — each doc read appends rows)

## .env.example

(to be populated by the env-consistency script diff)
