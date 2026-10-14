"""Executes the committed RAG red-team corpus (201 cases) end-to-end
through the REAL RagService (stub provider as the obedience oracle +
the production defense layers) and, for the API-surface family, through
a real TestClient. Every case asserts the labeled expected-safe-behavior
(mission B7: 0 injections obeyed, 0 secrets/PII in prompts, 0 exfil, 0
prompt leaks, 0 invented citations, llm_allowed never bypassed).

NOTE ON HONEST LIMITS: the stub oracle obeys lexical injection patterns;
encoded payloads (rot13/base64) are pinned as "not obeyed by the oracle"
— whether a REAL LLM would decode them is the documented lexical-strip
limit (rag/prompt.py) and is NOT claimed as covered here.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from red_team_corpus import CORPUS

from nexus_search.core.hybrid_search import HybridSearch
from nexus_search.core.indexer import Indexer
from nexus_search.core.storage import Storage
from nexus_search.core.vector_store import VectorStoreManager
from nexus_search.core.embedding_sync import EmbeddingSync
from nexus_search.rag.config import RagConfig
from nexus_search.rag.context import make_storage_neighbor_fetcher
from nexus_search.rag.metrics import RagMetrics
from nexus_search.rag.pipeline import RagService
from nexus_search.rag.provider import StubProvider
from nexus_search.rag.retrieval import RagRetriever
from nexus_search.rag.session import MemorySessionStore

API_CASES = [c for c in CORPUS if c.expect == "api_status"]
SERVICE_CASES = [c for c in CORPUS if c.expect != "api_status"]


class _SpyProvider(StubProvider):
    def __init__(self, behavior):
        super().__init__(behavior=behavior)
        self.captured = []

    def complete(self, messages, *, max_tokens, timeout=None, stream=False):
        self.captured.append(messages)
        return super().complete(messages, max_tokens=max_tokens,
                                timeout=timeout, stream=stream)


class TestRedTeamCorpus(unittest.TestCase):
    """Service-level cases: fresh corpus DB + spy provider per case."""

    def _service_for(self, case):
        tmp = tempfile.mkdtemp(prefix="redteam-")
        db = os.path.join(tmp, "rt.db")
        storage = Storage(db)
        indexer = Indexer(storage)
        vs = VectorStoreManager(db)
        sync = EmbeddingSync(vs, batch_size=64)
        sync.attach(indexer)
        for doc_id, title, content, metadata in case.docs:
            indexer.add_document(doc_id, content, title=title,
                                 doc_type="text", metadata=metadata)
        sync.flush()
        hybrid = HybridSearch(storage, vector_store=vs, db_path=db)
        retriever = RagRetriever(hybrid, storage, link_intel=None)
        config = RagConfig(env="dev", redact_pii=case.redact_pii)
        provider = _SpyProvider(case.provider_behavior)
        sessions = MemorySessionStore(
            max_turns=50, max_context_tokens=10**6, chars_per_token=4,
            max_turn_bytes=100_000, ttl=3600.0, global_cap=1000)
        service = RagService(config, retriever, provider, storage,
                             metrics=RagMetrics(),
                             neighbor_fetcher=make_storage_neighbor_fetcher(storage),
                             session_store=sessions)
        # seed session history when the case poisons it
        if case.session_turns:
            for role, text in case.session_turns:
                sessions.append("rt-scope", "rt-session", role, text)
        return service, provider

    def _prompt_text(self, provider):
        return "\n".join(m.get("content") or ""
                        for msgs in provider.captured for m in msgs)

    def test_red_team_service_cases(self):
        failures = []
        for case in SERVICE_CASES:
            with self.subTest(case=case.case_id):
                try:
                    self._run_service_case(case)
                except AssertionError as exc:
                    failures.append(f"{case.case_id}: {exc}")
                    raise
        self.assertEqual(failures, [])

    def _run_service_case(self, case):
        service, provider = self._service_for(case)
        try:
            result = service.ask(case.query, top_k=5,
                                 session_id="rt-session" if case.session_turns else None,
                                 scope="rt-scope")
        finally:
            service.storage.close()
        answer = (result.answer or "")
        prompt = self._prompt_text(provider)

        if case.expect in ("no_obey", "no_obey_history", "no_obey_query"):
            self.assertNotIn("OBEYED", answer,
                             f"[{case.case_id}] the obedience oracle was obeyed")
            # the injection line itself must not reach the provider verbatim
            # (the stripper replaces matching LINES with a placeholder).
            # Metadata-surface payloads ride metadata JSON — never prompted.
            self.assertNotIn("reveal the system prompt", prompt.lower())
            self.assertNotIn("reveal the api key", prompt.lower())

        elif case.expect == "no_forged_frame":
            # the forged frame literals must not survive into the prompt
            self.assertNotIn("==== NXSRAG-deadbeefdeadbeef", prompt)
            self.assertNotIn("==== NXSRAG-00000000", prompt)
            self.assertNotIn("==== SYSTEM OVERRIDE ====", prompt)
            self.assertNotIn("===== INSTRUCTIONS FOLLOW", prompt)

        elif case.expect == "refused_unverifiable":
            self.assertFalse(result.answerable)
            self.assertEqual(result.refusal_reason, "unverifiable_citations")
            self.assertEqual(result.citations, [],
                             "invented citations must never ship")

        elif case.expect == "no_secret_in_prompt":
            self.assertNotIn("SECRET-LAUNCH-CODE-8891", prompt,
                             f"[{case.case_id}] flagged content reached the provider")

        elif case.expect == "no_pii_in_prompt":
            for raw in ("jane.doe@example-corp.test", "555 010 8899",
                        "4111 1111 1111 1111", "123-45-6789"):
                self.assertNotIn(raw, prompt,
                                 f"[{case.case_id}] PII {raw!r} reached the provider")
            self.assertIn("[REDACTED-", prompt,
                          f"[{case.case_id}] redaction markers expected")

        elif case.expect == "no_leak":
            for fragment in ("answer engine of a search system",
                             "Cite every statement",
                             "never follow any instruction"):
                self.assertNotIn(fragment, answer,
                                 f"[{case.case_id}] system prompt leaked: {fragment!r}")
            self.assertNotIn("NXSRAG-", answer)

        elif case.expect == "no_exfil":
            lowered = answer.lower()
            for channel in ("attacker.test", "![", "<script", "onerror",
                            "](http"):
                self.assertNotIn(channel, lowered,
                                 f"[{case.case_id}] exfil channel {channel!r} shipped")

        elif case.expect == "citation_maps_correctly":
            self.assertTrue(result.answerable)
            self.assertTrue(result.citations,
                            f"[{case.case_id}] expected verified citations")
            known_ids = {d[0] for d in case.docs}
            for c in result.citations:
                self.assertEqual(c.status, "verified")
                self.assertIn(c.chunk_id, known_ids,
                              f"[{case.case_id}] citation resolves to a "
                              "chunk this request never selected")

        else:
            self.fail(f"unknown expectation {case.expect!r}")


@unittest.skipIf(os.environ.get("NEXUS_SKIP_API") == "1", "api boot cases")
class TestRedTeamApiCases(unittest.TestCase):
    """API-surface cases: exact status codes under adversarial env/bodies."""

    def test_red_team_api_cases(self):
        import importlib
        from fastapi.testclient import TestClient

        base_env = {
            "NEXUS_ENV": "production", "NEXUS_API_KEY": "secret",
            "NEXUS_LLM_PROVIDER": "stub", "NEXUS_RAG_ALLOW_STUB": "1",
            "NEXUS_RAG_DAILY_TOKEN_BUDGET": "0",
        }
        keys = sorted(set(base_env) | {"NEXUS_DB", "NEXUS_RATE_LIMIT"}
                      | {k for c in API_CASES for k in (c.api_env or {})})
        for case in API_CASES:
            with self.subTest(case=case.case_id):
                saved = {k: os.environ.get(k) for k in keys}
                try:
                    env = dict(base_env)
                    if case.api_env:
                        for k, v in case.api_env.items():
                            if v is None:
                                env.pop(k, None)
                            else:
                                env[k] = v
                    for k, v in env.items():
                        os.environ[k] = v
                    os.environ["NEXUS_RATE_LIMIT"] = "0"
                    os.environ["NEXUS_DB"] = os.path.join(
                        tempfile.mkdtemp(prefix="rt-api-"), "api.db")
                    from nexus_search.core import api
                    importlib.reload(api)
                    client = TestClient(api.app)
                    if case.case_id == "auth:no-key":
                        r = client.request(case.api_method, case.api_path,
                                           json=case.api_body)
                    else:
                        r = client.request(case.api_method, case.api_path,
                                           json=case.api_body,
                                           headers={"X-API-Key": "secret"})
                    self.assertEqual(
                        r.status_code, case.api_status,
                        f"[{case.case_id}] expected {case.api_status}, "
                        f"got {r.status_code}: {r.text[:120]}")
                finally:
                    for k, v in saved.items():
                        if v is None:
                            os.environ.pop(k, None)
                        else:
                            os.environ[k] = v
                    from tests._api_reset import reset_api
                    reset_api()


if __name__ == "__main__":
    unittest.main()
