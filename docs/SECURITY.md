# Security — threat model (STRIDE) per phase

Written by the review/hardening audit from the code. Residual risks are
HONEST: each one lists what would remove it. Keep in sync with
docs/ARCHITECTURE.md.

## Trust boundaries

1. HTTP clients → API (FastAPI). Auth: X-API-Key constant-time; writes
   and /ask* ALWAYS require it in production (fail-closed boot without
   one); reads open by design, opt-in gating via
   NEXUS_REQUIRE_AUTH_FOR_READS.
2. The web → crawler (requests + optional Playwright). Everything
   fetched is untrusted.
3. LLM provider endpoint → us (OpenAI-compatible/Anthropic over HTTPS;
   key in the Authorization header only).
4. Document content → everything downstream (index, vectors, prompts,
   snippets, link graph): untrusted by default.
5. Environment/secrets → process. Keys never logged (poisoned-key
   tests); rate-limit/budget buckets key on VERIFIED keys only.

## STRIDE matrix

| Phase | Spoofing | Tampering | Repudiation | Info disclosure | DoS | Elevation |
|---|---|---|---|---|---|---|
| P1 core | API key spoof → constant-time compare; per-verified-key limiter buckets | cursor is opaque but only encodes offset (validated 400); query bounds 2000ch/128w pre-parse | query log records variant + mode (no auth event log yet — see residual) | debug output key-gated; /docs off in production; errors carry no internals | rate limit 60/min default; NEXUS_MAX_CANDIDATES pool bound; term/phrase caps; inf-weight 400 | filters rendered into query syntax (no SQL interpolation anywhere — all SQL is parameterized; B608 findings are '?'-join construction, reviewed) |
| P2 ingestion | — | size caps (64MB file / 64MB payload / 32MB entry / 10k pages); zip-bomb refusal pre-read; content-sniff routing (extension lies can't misroute binaries; generic zips refused) | failure ledger + content history give an audit trail | — | bounds above + per-request 4xx; deep-JSON nesting → RecursionError caught → refused | MIME sniffing prevents reader confusion |
| P3 crawler | UA must be a real contact (placeholder refused) | robots.txt fail-closed when unreachable; RFC 9309 matcher OWNED (wildcards honored — version-independent); blocklist > robots > allow-list; redirect hops re-gated (SSRF + robots + blocklist + domains) | frontier is the durable record (visited/errors/skipped with reasons) | robots 500KiB cap; sitemap decompression cap 16MB | 5MB body cap + content-length check; 5-redirect cap; per-domain crawl-delay slots on a monotonic clock; DNS pinning fails CLOSED (pin failure = refused fetch) | SSRF suite pins loopback/private/mapped/mixed-DNS; render_js is opt-in with residual DNS-rebinding documented |
| P4 hybrid | — | atomic doc+postings writes; vector swap guarded by mem-version | /health coverage numbers | — | NEXUS_MAX_CANDIDATES; weights validated finite; fusion bounded | vector allowed_ids pushdown preserves filter semantics |
| P5 ranking | A/B bucketing deterministic per session id | — | A/B log stores RAW query text — retention purge REQUIRED (documented; CLI provided) | query text in the A/B log is the known privacy surface (consent/PII is the operator's responsibility; purge tool ships) | spell-correction budgets (BUG-01) | — |
| P6 links | link graph rows are normalized at ONE choke point (forged URL variants collapse) | shadow-swap scores; edge-version guard | first_seen/last_seen history per edge | /graph/* key-gated | anti-flood caps (1000/page, 500/domain-pair) | reciprocal/nofollow/domain-diversity anti-farm corrections; exact-hostname limit documented |
| P7 RAG | session ids client-supplied but charset/length-capped and SCOPED per verified-key bucket (cross-key reads impossible; prune is scope-paired) | nonce-fenced prompts (forged frames neutralized); injection stripping incl. homoglyphs/zero-width; query stripped too; citation verification re-fetches rows (fabricated -> refused) | metrics are counts only (numbers-only test); request_id per answer | llm_allowed:false never reaches the provider (spy-tested); PII redaction opt-in; /metrics numbers-only; /docs off; no key/prompt/body in logs (poisoned-key tests) | hard timeouts <=30s; bounded retries (connect/429/5xx only); concurrency semaphore -> 429+Retry-After; DAILY token budgets per bucket (shared SQLite, UTC day); stream char+duration caps; /metrics rate-limited | tool-calling impossible by construction (no tools parameter); REDTEAM corpus (201 cases) pins 0-obeyed / 0-leak / 0-exfil |

## Residual risks (honest, with the removal path)

1. **render_js DNS rebinding** (opt-in only): Chromium resolves its own
   DNS, so pinning cannot apply. Removal: egress firewall or a
   validating proxy in front of the browser.
2. **Lexical-only RAG defenses**: the injection stripper and prompt-leak
   scrubber are lexical (homoglyph/zero-width normalized, but paraphrase
   or decoded-payload attacks depend on model behavior). Removal: output
   moderation model (OutputModerator seam exists), model-assisted
   detection in the P7-9 human-review loop.
3. **Refuse-gate is English-calibrated, hash-embedder-calibrated**: an
   unknown embedder fails closed with a loud warning; non-English
   quality unmeasured. Removal: per-embedder + per-language calibration
   on judged data (train split only).
4. **Per-worker rate limiter** (slowapi) is in-process; the token budget
   is the only cross-worker cap. Removal: Phase 8 shared store.
5. **A/B log stores raw query text** — GDPR-relevant. Purge CLI ships;
   scheduled purge is the operator's runbook item.
6. **No admin action audit log** (who blocked a host, who changed keys).
   Removal: a structured admin-log table (Phase 10).
7. **Single-process SQLite**: horizontal scale and multi-writer safety
   are explicitly Phase 8.
8. **mutmut tool-run**: blocked natively on Windows (WSL-only, no Linux
   interpreter configured on this host); 14+ per-fix manual mutation
   checks are the compensating evidence; run mutmut in CI (Linux) to
   close this.

## Verification index

- SSRF/pinning/robots: tests/crawler/test_security*.py,
  test_dns_pinning.py, test_redirect_robots.py, test_robots_rfc9309.py
- RAG red team: tests/rag/red_team_corpus.py + test_red_team.py (201)
- Egress/secrets: tests/rag/test_egress.py (poisoned key),
  test_sanitize_fuzz.py
- Auth/limits/budgets: tests/core/test_api.py, tests/rag/test_ask_api.py,
  test_budget_rollover.py
- Crash/integrity: tests/core/test_crash_safety.py
- Supply chain: pip-audit clean (pinned set), no secrets in history
  (pattern sweep), no copyleft licenses
