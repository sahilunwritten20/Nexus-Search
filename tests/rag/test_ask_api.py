"""P7-6 — POST /ask: unconditional key-gating (never _READ_AUTH), bounded
request model, budget 429 over SHARED state (two app instances, one DB),
kill switch/absent 503s, provider-busy 429, request ids, OpenAPI
examples, errors that never leak internals."""
import importlib
import os
import tempfile
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None

KEYS = ("NEXUS_ENV", "NEXUS_API_KEY", "NEXUS_DB", "NEXUS_LLM_PROVIDER",
        "NEXUS_RAG_ALLOW_STUB", "NEXUS_RAG_ENABLED", "NEXUS_REQUIRE_AUTH_FOR_READS",
        "NEXUS_RAG_DAILY_TOKEN_BUDGET", "NEXUS_LLM_MAX_TOKENS",
        "NEXUS_RAG_MAX_CONTEXT_TOKENS")


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class _AskApiTest(unittest.TestCase):
    def _boot(self, **env):
        from nexus_search.core import api
        defaults = {"NEXUS_ENV": "production", "NEXUS_API_KEY": "secret",
                    "NEXUS_LLM_PROVIDER": "stub", "NEXUS_RAG_ALLOW_STUB": "1",
                    "NEXUS_RAG_DAILY_TOKEN_BUDGET": "0"}
        defaults.update(env)
        for key in KEYS:
            if key not in defaults:
                os.environ.pop(key, None)
        for key, value in defaults.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        api = importlib.reload(api)
        for doc_id, title, content in [
            ("bm25-doc", "BM25 and Lexical Retrieval",
             "BM25 scores lexical matches over an inverted index. Term "
             "frequency and inverse document frequency drive the ranking."),
            ("vector-doc", "Vector Embeddings",
             "Dense embeddings map text into a vector space where cosine "
             "similarity approximates semantic relatedness."),
        ]:
            api._indexer.add_document(doc_id, content, title=title)
        return api, TestClient(api.app)

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in KEYS}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        from nexus_search.core import api
        importlib.reload(api)  # restore a booting state for other tests


class TestAskAuth(_AskApiTest):
    def test_ask_is_key_gated_even_when_reads_are_open(self):
        _, client = self._boot()   # production + key, reads OPEN by default
        # reads really are open (no key): proves _READ_AUTH did not engage
        self.assertEqual(client.get("/search", params={"q": "bm25"}).status_code, 200)
        # /ask is NOT open: 401 without the key (WP14-8: unconditional)
        r = client.post("/ask", json={"query": "bm25 lexical ranking"})
        self.assertEqual(r.status_code, 401)
        # and works with the verified key
        r2 = client.post("/ask", json={"query": "bm25 lexical inverted index ranking"},
                         headers={"X-API-Key": "secret"})
        self.assertEqual(r2.status_code, 200, r2.text)
        body = r2.json()
        self.assertTrue(body["answerable"])
        self.assertEqual(body["metadata"]["provider"], "stub")
        self.assertTrue(body["request_id"])

    def test_wrong_key_is_401(self):
        _, client = self._boot()
        r = client.post("/ask", json={"query": "bm25"},
                        headers={"X-API-Key": "not-the-key"})
        self.assertEqual(r.status_code, 401)


class TestAskBounds(_AskApiTest):
    def test_query_bounds_are_4xx_before_any_spend(self):
        _, client = self._boot()
        ok = {"X-API-Key": "secret"}
        too_long = "x" * 2001
        r = client.post("/ask", json={"query": too_long}, headers=ok)
        self.assertEqual(r.status_code, 422)  # pydantic bound
        too_many_words = " ".join(["w"] * 129)
        r2 = client.post("/ask", json={"query": too_many_words}, headers=ok)
        self.assertIn(r2.status_code, (400, 422))
        empty = client.post("/ask", json={"query": "   "}, headers=ok)
        self.assertIn(empty.status_code, (400, 422))
        bad_mode = client.post("/ask", json={"query": "bm25", "mode": "agentic"},
                                headers=ok)
        self.assertEqual(bad_mode.status_code, 422)
        big_k = client.post("/ask", json={"query": "bm25", "top_k": 1000},
                            headers=ok)
        self.assertEqual(big_k.status_code, 422)

    def test_session_id_bounded(self):
        _, client = self._boot()
        ok = {"X-API-Key": "secret"}
        too_long = "s" * 65
        r = client.post("/ask", json={"query": "bm25", "session_id": too_long},
                        headers=ok)
        self.assertEqual(r.status_code, 422)
        bad_charset = client.post(
            "/ask", json={"query": "bm25", "session_id": "bad id!; drop"},
            headers=ok)
        self.assertEqual(bad_charset.status_code, 422)
        fine = client.post("/ask", json={"query": "bm25 lexical inverted index",
                                          "session_id": "user-1.2:s"},
                            headers=ok)
        self.assertEqual(fine.status_code, 200)


class TestAskBehavior(_AskApiTest):
    def test_refusal_is_200_with_sources_and_zero_spend(self):
        _, client = self._boot()
        r = client.post("/ask", json={"query": "best recipe for sourdough bread"},
                        headers={"X-API-Key": "secret"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["answerable"])
        self.assertIsNotNone(body["refusal_reason"])
        self.assertIsNone(body["answer"])
        self.assertEqual(body["metadata"]["tokens_in"], 0)

    def test_kill_switch_503_and_search_keeps_serving(self):
        _, client = self._boot(NEXUS_RAG_ENABLED="0")
        r = client.post("/ask", json={"query": "bm25"},
                        headers={"X-API-Key": "secret"})
        self.assertEqual(r.status_code, 503)
        self.assertIn("disabled", r.json()["detail"])
        self.assertEqual(client.get("/search", params={"q": "bm25"}).status_code, 200)

    def test_absent_provider_503_and_health_reports_absent(self):
        _, client = self._boot(NEXUS_LLM_PROVIDER="openai",
                               NEXUS_RAG_ALLOW_STUB=None)
        os.environ.pop("NEXUS_LLM_API_KEY", None)
        from nexus_search.core import api
        importlib.reload(api)
        client = TestClient(api.app)
        r = client.post("/ask", json={"query": "bm25"},
                        headers={"X-API-Key": "secret"})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(client.get("/health").json()["rag"]["status"], "absent")
        self.assertEqual(client.get("/search", params={"q": "bm25"}).status_code, 200)

    def test_stub_outside_dev_without_opt_in_503(self):
        _, client = self._boot(NEXUS_RAG_ALLOW_STUB="0")
        r = client.post("/ask", json={"query": "bm25"},
                        headers={"X-API-Key": "secret"})
        self.assertEqual(r.status_code, 503)

    def test_openapi_examples_present(self):
        _, client = self._boot(NEXUS_ENV="dev")
        spec = client.get("/openapi.json").json()
        # FastAPI $refs the models; the examples live in the schemas
        req_schema = spec["components"]["schemas"]["AskRequest"]
        self.assertIn("examples", req_schema)
        resp_schema = spec["components"]["schemas"]["AskResponse"]
        self.assertIn("examples", resp_schema)

    def test_metrics_rag_block_is_numbers_only(self):
        api, client = self._boot()
        ok = {"X-API-Key": "secret"}
        poison_query = "my query about the launch code sk-POISON-98765"
        client.post("/ask", json={"query": "bm25 lexical inverted index"},
                    headers=ok)
        client.post("/ask", json={"query": poison_query}, headers=ok)
        r = client.get("/metrics", headers=ok)
        self.assertEqual(r.status_code, 200)
        rag = r.json()["rag"]
        self.assertIn(rag["status"], ("ok", "disabled", "absent"))
        self.assertEqual(rag["provider"], "stub")
        self.assertGreaterEqual(rag["counters"]["totals"]["requests"], 2)
        # NUMBERS ONLY: neither the query text nor any secret ships
        blob = r.text
        self.assertNotIn("poison_query", blob)
        self.assertNotIn("launch code", blob)
        self.assertNotIn("bm25 lexical", blob)
        self.assertNotIn("secret", blob.replace('"secret"', ""))

    def test_no_internals_in_error_bodies(self):
        _, client = self._boot()
        r = client.post("/ask", json={"query": "bm25"},
                        headers={"X-API-Key": "secret"})
        blob = r.text
        self.assertNotIn("Traceback", blob)
        self.assertNotIn(".py", blob)


class TestAskBudgetSharedState(_AskApiTest):
    def test_budget_holds_across_two_app_instances_over_one_db(self):
        db = os.path.join(tempfile.mkdtemp(), "shared_budget.db")
        api1, client1 = self._boot(
            NEXUS_RAG_DAILY_TOKEN_BUDGET="1500",
            NEXUS_LLM_MAX_TOKENS="256",
            NEXUS_RAG_MAX_CONTEXT_TOKENS="500",
            NEXUS_DB=db)
        # instance 2 over the SAME database file: a separate Storage
        # connection set, same shared rag_token_usage table
        api2, client2 = self._boot(
            NEXUS_RAG_DAILY_TOKEN_BUDGET="1500",
            NEXUS_LLM_MAX_TOKENS="256",
            NEXUS_RAG_MAX_CONTEXT_TOKENS="500",
            NEXUS_DB=db)
        headers = {"X-API-Key": "secret"}
        # both instances must see the SAME budget table (pre-check reads)
        code1 = code2 = None
        exhausted_after = 0
        for i in range(10):
            code1 = client1.post("/ask",
                                 json={"query": "bm25 lexical inverted index"},
                                 headers=headers).status_code
            if code1 == 429:
                exhausted_after = i
                break
            code2 = client2.post("/ask",
                                 json={"query": "vector cosine similarity"},
                                 headers=headers).status_code
            if code2 == 429:
                exhausted_after = i
                break
        # the budget DID bite on one of the instances...
        self.assertIn(429, (code1, code2))
        # ...and once exhausted, BOTH instances refuse (shared state)
        r2 = client2.post("/ask", json={"query": "bm25 lexical inverted index"},
                           headers=headers)
        r1 = client1.post("/ask", json={"query": "vector cosine similarity"},
                          headers=headers)
        self.assertEqual(r1.status_code, 429)
        self.assertEqual(r2.status_code, 429)
        self.assertIn("retry-after", {k.lower() for k in r1.headers.keys()})
        self.assertIn("budget", r1.json()["detail"].lower())

    def test_verified_key_gets_its_own_bucket_wrong_keys_share_ip(self):
        from nexus_search.rag.budget import bucket_key
        self.assertEqual(bucket_key("secret", "secret", "1.2.3.4"),
                         bucket_key("secret", "secret", "5.6.7.8"))
        self.assertNotEqual(bucket_key("secret", "secret", "1.2.3.4"),
                            bucket_key("wrong", "secret", "1.2.3.4"))
        # wrong keys fall through to the IP bucket (shared, like slowapi)
        self.assertEqual(bucket_key("wrong", "secret", "1.2.3.4"),
                         bucket_key("wrong2", "secret", "1.2.3.4"))


class TestStorageBudgetTable(unittest.TestCase):
    def test_usage_table_migration_and_prune(self):
        import time as _t
        tmp = tempfile.mkdtemp()
        from nexus_search.core.storage import Storage
        s1 = Storage(os.path.join(tmp, "b.db"))
        try:
            self.assertGreaterEqual(s1.schema_version, 3)
            self.assertEqual(s1.rag_usage_get("k1"), 0)
            self.assertEqual(s1.rag_usage_add("k1", 100), 100)
            self.assertEqual(s1.rag_usage_add("k1", 50), 150)
            # second instance over the same file SEES the same counter
            s2 = Storage(os.path.join(tmp, "b.db"))
            try:
                self.assertEqual(s2.rag_usage_get("k1"), 150)
                self.assertEqual(s2.rag_usage_add("k1", 1), 151)
                self.assertEqual(s1.rag_usage_get("k1"), 151)
            finally:
                s2.close()
            # prune removes only OLD days
            old_day = _t.strftime("%Y-%m-%d", _t.localtime(_t.time() - 40 * 86400))
            s1.rag_usage_add("k2", 999, day=old_day)
            self.assertEqual(s1.rag_usage_prune(keep_days=14), 1)
            self.assertEqual(s1.rag_usage_get("k1"), 151)
            with self.assertRaises(ValueError):
                s1.rag_usage_prune(keep_days=0)
        finally:
            s1.close()


if __name__ == "__main__":
    unittest.main()
