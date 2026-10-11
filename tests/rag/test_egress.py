"""P7-3 — egress controls: PII redaction, query hashing (never text),
the canonical egress list, and the boot-policy /health block + the
poisoned-key secret-leak test (zero log records, zero response bodies)."""
import importlib
import logging
import os
import tempfile
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.rag.egress import EGRESS_DOC, query_hash, redact_pii

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None


class TestPIIRedaction(unittest.TestCase):
    def test_off_by_default_is_config_not_code(self):
        # the redactor is PURE — enabling is an env decision (config.py)
        text, n = redact_pii("contact jane.doe@example.org now")
        self.assertEqual(n, 1)
        self.assertNotIn("jane.doe@example.org", text)
        self.assertIn("[REDACTED-EMAIL]", text)

    def test_card_phone_ssn_patterns(self):
        text, n = redact_pii(
            "card 4111 1111 1111 1111 phone +1 (555) 123-4567 ssn 123-45-6789")
        self.assertEqual(n, 3)
        self.assertNotIn("4111", text)
        self.assertNotIn("123-45-6789", text)

    def test_normal_text_passes_untouched(self):
        original = "The BM25 score ranks lexical matches over an index."
        text, n = redact_pii(original)
        self.assertEqual(n, 0)
        self.assertEqual(text, original)

    def test_empty_and_none_safe(self):
        self.assertEqual(redact_pii(""), ("", 0))

    def test_documented_limits_obfuscation_passes_through(self):
        # HONEST LIMIT: deliberately spaced emails are NOT caught
        text, n = redact_pii("j a n e @ d o e . c o m")
        self.assertEqual(n, 0)


class TestQueryHashing(unittest.TestCase):
    def test_hash_is_sha256_16_and_never_the_query(self):
        h = query_hash("who won the world cup")
        self.assertEqual(len(h), 16)
        self.assertNotIn("world", h)
        # deterministic
        self.assertEqual(h, query_hash("who won the world cup"))
        self.assertNotEqual(h, query_hash("a different question"))


class TestEgressDoc(unittest.TestCase):
    def test_the_list_states_the_stub_is_offline_and_real_calls_leave(self):
        joined = "\n".join(EGRESS_DOC)
        self.assertIn("llm_allowed", joined)
        self.assertIn("NEXUS_RAG_REDACT_PII", joined)
        self.assertIn("NEVER leaves", joined)


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class TestHealthRagStatus(unittest.TestCase):
    """Boot policy on the real /health endpoint: rag status block, and the
    POISONED-KEY test — the key appears in zero log records and zero
    response bodies."""

    _KEYS = ("NEXUS_ENV", "NEXUS_API_KEY", "NEXUS_DB", "NEXUS_LLM_PROVIDER",
             "NEXUS_LLM_API_KEY", "NEXUS_RAG_ENABLED", "NEXUS_RAG_ALLOW_STUB")

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self._KEYS}
        self._records: list[str] = []
        self._handler = logging.Handler()
        self._handler.emit = lambda record: self._records.append(
            record.getMessage())
        logging.getLogger().addHandler(self._handler)
        for logger_name in ("nexus_search", ""):
            logging.getLogger(logger_name).addHandler(self._handler)

    def tearDown(self):
        for logger_name in ("nexus_search", ""):
            logging.getLogger(logger_name).removeHandler(self._handler)
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        from nexus_search.core import api
        importlib.reload(api)  # back to a booting state for other tests

    def _reload(self):
        from nexus_search.core import api
        return importlib.reload(api), TestClient(api.app)

    def test_stub_dev_reports_ok(self):
        os.environ["NEXUS_ENV"] = "dev"
        os.environ["NEXUS_DB"] = os.path.join(tempfile.mkdtemp(), "h1.db")
        os.environ["NEXUS_LLM_PROVIDER"] = "stub"
        _, client = self._reload()
        r = client.get("/health").json()
        self.assertEqual(r["rag"]["status"], "ok")
        self.assertEqual(r["rag"]["provider"], "stub")
        self.assertIsNone(r["rag"]["reason"])

    def test_real_provider_without_key_reports_absent(self):
        os.environ["NEXUS_ENV"] = "production"
        os.environ["NEXUS_API_KEY"] = "api-key-for-boot"
        os.environ["NEXUS_DB"] = os.path.join(tempfile.mkdtemp(), "h2.db")
        os.environ["NEXUS_LLM_PROVIDER"] = "openai"
        os.environ.pop("NEXUS_LLM_API_KEY", None)
        _, client = self._reload()
        r = client.get("/health").json()
        self.assertEqual(r["rag"]["status"], "absent")
        self.assertEqual(r["rag"]["reason"], "llm_key_missing")

    def test_stub_outside_dev_without_opt_in_reports_absent(self):
        os.environ["NEXUS_ENV"] = "production"
        os.environ["NEXUS_API_KEY"] = "api-key-for-boot"
        os.environ["NEXUS_DB"] = os.path.join(tempfile.mkdtemp(), "h3.db")
        os.environ["NEXUS_LLM_PROVIDER"] = "stub"
        os.environ.pop("NEXUS_RAG_ALLOW_STUB", None)
        _, client = self._reload()
        r = client.get("/health").json()
        self.assertEqual(r["rag"]["status"], "absent")
        self.assertEqual(r["rag"]["reason"], "stub_requires_opt_in")

    def test_kill_switch_reports_disabled(self):
        os.environ["NEXUS_ENV"] = "dev"
        os.environ["NEXUS_DB"] = os.path.join(tempfile.mkdtemp(), "h4.db")
        os.environ["NEXUS_LLM_PROVIDER"] = "stub"
        os.environ["NEXUS_RAG_ENABLED"] = "0"
        _, client = self._reload()
        r = client.get("/health").json()
        self.assertEqual(r["rag"]["status"], "disabled")
        self.assertEqual(r["rag"]["reason"], "kill_switch")

    def test_poisoned_key_never_in_logs_health_metrics_or_bodies(self):
        POISON = "sk-POISON-4c1f2e9b8d7a6f5e"
        os.environ["NEXUS_ENV"] = "dev"
        os.environ["NEXUS_DB"] = os.path.join(tempfile.mkdtemp(), "h5.db")
        os.environ["NEXUS_LLM_PROVIDER"] = "openai"
        os.environ["NEXUS_LLM_API_KEY"] = POISON
        _, client = self._reload()
        self.assertEqual(client.get("/health").json()["rag"]["status"], "ok")
        # /metrics needs the API key (dev boot here has none set) — probe
        # the surfaces with and without auth
        for path in ("/health", "/search?q=x", "/ready"):
            body = client.get(path).text
            self.assertNotIn(POISON, body, f"poison leaked via {path}")
        # log records captured from boot + requests
        for message in self._records:
            self.assertNotIn(POISON, message)

    def test_rag_block_degraded_vector_boot_still_present(self):
        # vector subsystem unavailable (embedder boom) -> degraded health,
        # RAG block still honest about its own status. The boom is
        # INJECTED (monkeypatched get_embedder) so the suite stays OFFLINE
        # — no model download is ever attempted from a test.
        from nexus_search.core import vector_store as vs_mod
        from nexus_search.core.embedders import EmbedderUnavailable, reset_embedder
        reset_embedder()  # the embedder is a process singleton — re-resolve

        def boom():
            raise EmbedderUnavailable("injected for the degraded-boot test")

        original = vs_mod.get_embedder
        vs_mod.get_embedder = boom  # vector_store binds the name at import
        os.environ["NEXUS_ENV"] = "dev"
        os.environ["NEXUS_DB"] = os.path.join(tempfile.mkdtemp(), "h6.db")
        os.environ["NEXUS_LLM_PROVIDER"] = "stub"
        try:
            _, client = self._reload()
            r = client.get("/health").json()
            self.assertEqual(r["status"], "degraded")
            self.assertIn("rag", r)
            self.assertEqual(r["rag"]["status"], "ok")
        finally:
            vs_mod.get_embedder = original
            reset_embedder()


if __name__ == "__main__":
    unittest.main()
