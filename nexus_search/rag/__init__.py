"""Phase 7 — AI Search / RAG.

Package layout (work packages add modules in order; none of these may
import `core.api` — `core/api.py` constructs and wires the service):
  config.py      env -> RagConfig (boot policy, provider, budgets, floors)
  retrieval.py   P7-1  RagRetriever over the ONE shared HybridSearch
  context.py     P7-2  budgeted, deduped, numbered context selection
  provider.py    P7-3  LLMProvider interface + Stub/OpenAI/Anthropic adapters
  egress.py      P7-3  data-egress controls (llm_allowed, PII, query logs)
  metrics.py     P7-3  RAG counters for /metrics
  gate.py        P7-4  raw-signal refuse-gate + frozen floors
  calibration.py P7-4  floor calibration harness on the WP10 fixture
"""
