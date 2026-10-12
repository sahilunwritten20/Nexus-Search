"""P7-7 — POST /ask/stream (SSE): meta-before-token (refusal streams no
tokens), token streaming sanitized incl. split payloads, citations at
end (authoritative), truncated on LLM timeout, disconnect cancels + slot
release, per-key open-stream cap -> 429, proxy headers, auth + budget."""
import importlib
import json
import os
import tempfile
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None

KEYS = ("NEXUS_ENV", "NEXUS_API_KEY", "NEXUS_DB", "NEXUS_LLM_PROVIDER",
        "NEXUS_RAG_ALLOW_STUB", "NEXUS_RAG_ENABLED",
        "NEXUS_RAG_DAILY_TOKEN_BUDGET", "NEXUS_LLM_MAX_TOKENS",
        "NEXUS_RAG_MAX_CONTEXT_TOKENS", "NEXUS_RAG_MAX_OPEN_STREAMS",
        "NEXUS_LLM_TIMEOUT")


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        event, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[7:].strip()
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if event:
            events.append((event, data))
    return events


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class AskStreamApiTest(unittest.TestCase):
    def _boot(self, **env):
        from nexus_search.core import api
        defaults = {"NEXUS_ENV": "production", "NEXUS_API_KEY": "secret",
                    "NEXUS_LLM_PROVIDER": "stub", "NEXUS_RAG_ALLOW_STUB": "1",
                    "NEXUS_RAG_DAILY_TOKEN_BUDGET": "0",
                    "NEXUS_RAG_MAX_OPEN_STREAMS": "16"}
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
        importlib.reload(api)

    def _post_stream(self, client, query, **kw):
        with client.stream("POST", "/ask/stream",
                           json={"query": query, **kw},
                           headers={"X-API-Key": "secret"}) as r:
            body = "".join(chunk.decode() for chunk in r.iter_raw())
            status = r.status_code
            headers = dict(r.headers)
        return status, parse_sse(body), headers

    # ------------------------------------------------------------- happy

    def test_stream_order_meta_tokens_citations_done(self):
        _, client = self._boot()
        status, events, headers = self._post_stream(
            client, "bm25 lexical inverted index ranking")
        self.assertEqual(status, 200)
        names = [e for e, _ in events]
        self.assertEqual(names[0], "meta")      # before any token
        self.assertIn("token", names)
        self.assertEqual(names[-1], "done")
        self.assertLess(names.index("token"), names.index("citations"))
        meta = events[0][1]
        self.assertTrue(meta["answerable"])
        self.assertIn("gate_signals", meta["retrieval"])
        citations_ev = [d for e, d in events if e == "citations"][0]
        self.assertTrue(citations_ev["citations_verified"])
        self.assertEqual(citations_ev["citations"][0]["chunk_id"], "bm25-doc")
        done = events[-1][1]
        self.assertIn("answer", done)
        self.assertIn("[1]", done["answer"])
        self.assertGreater(done["spend"]["tokens_out"], 0)

    def test_sse_headers_proxy_buffering_disabled(self):
        _, client = self._boot()
        status, _, headers = self._post_stream(client, "bm25 lexical")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("x-accel-buffering"), "no")
        self.assertEqual(headers.get("cache-control"), "no-cache")
        self.assertTrue(headers.get("content-type", "").startswith("text/event-stream"))

    # ---------------------------------------------------------- refusal

    def test_refusal_streams_no_tokens(self):
        _, client = self._boot()
        status, events, _ = self._post_stream(
            client, "best recipe for sourdough bread")
        self.assertEqual(status, 200)
        names = [e for e, _ in events]
        self.assertEqual(names[0], "meta")
        self.assertNotIn("token", names)
        self.assertFalse(events[0][1]["answerable"])
        done = events[-1][1]
        self.assertEqual(done["spend"]["tokens_in"], 0)
        self.assertTrue(done["sources"])

    # -------------------------------------------------------- truncation

    def test_llm_timeout_streams_truncated_never_hangs(self):
        # stub behavior=slow inside a stream: the provider call exceeds
        # the hard timeout -> truncated marker, explicit, no silent hang
        api, client = self._boot(NEXUS_LLM_TIMEOUT="0.5")
        from nexus_search.rag.provider import StubProvider
        original = api._rag_service.provider
        api._rag_service.provider = StubProvider(behavior="slow")
        try:
            status, events, _ = self._post_stream(
                client, "bm25 lexical inverted index")
        finally:
            api._rag_service.provider = original
        names = [e for e, _ in events]
        self.assertIn("truncated", names)
        self.assertIn("provider_timeout",
                       [d for e, d in events if e == "truncated"][0]["reason"])
        self.assertEqual(names[-1], "done")
        done = events[-1][1]
        self.assertEqual(done["degraded_reason"], "provider_timeout")
        self.assertTrue(done["sources"])

    # ---------------------------------------------------------- defenses

    def test_streamed_xss_and_exfil_payloads_never_ship(self):
        # payloads SPLIT across token boundaries must not survive
        api, client = self._boot()
        from nexus_search.rag.provider import StubProvider
        for behavior in ("markdown_exfil", "html_payload"):
            original = api._rag_service.provider
            api._rag_service.provider = StubProvider(behavior=behavior)
            try:
                status, events, _ = self._post_stream(
                    client, "bm25 lexical inverted index")
            finally:
                api._rag_service.provider = original
            streamed = "".join(d["text"] for e, d in events if e == "token")
            self.assertNotIn("attacker.example", streamed)
            self.assertNotIn("<script>", streamed)
            done = events[-1][1]
            self.assertNotIn("attacker.example", done.get("answer") or "")
            self.assertNotIn("<img", done.get("answer") or "")

    def test_citation_verification_at_stream_end_is_authoritative(self):
        api, client = self._boot()
        from nexus_search.rag.provider import StubProvider
        original = api._rag_service.provider
        api._rag_service.provider = StubProvider(behavior="hallucinated_citation")
        try:
            status, events, _ = self._post_stream(
                client, "bm25 lexical inverted index")
        finally:
            api._rag_service.provider = original
        names = [e for e, _ in events]
        citations_ev = [d for e, d in events if e == "citations"][0]
        self.assertFalse(citations_ev["citations_verified"])
        self.assertEqual(citations_ev["citations"], [])
        done = events[-1][1]
        self.assertFalse(done["answerable"])
        self.assertEqual(done["refusal_reason"], "unverifiable_citations")

    # --------------------------------------------------------- disconnect

    def test_client_disconnect_releases_slot_and_settles_nothing(self):
        api, client = self._boot()
        with client.stream("POST", "/ask/stream",
                           json={"query": "bm25 lexical inverted index"},
                           headers={"X-API-Key": "secret"}) as r:
            it = r.iter_raw()
            chunk = next(iter(it))
            self.assertTrue(chunk)  # stream started
            # closing the response mid-stream = client disconnect
        # after disconnect the bucket slot must be free again:
        # a subsequent stream must be accepted (no 429)
        status2, events2, _ = self._post_stream(client, "bm25 lexical")
        self.assertEqual(status2, 200)
        self.assertEqual(api._rag_open_stream_count(), 0)

    def test_open_stream_cap_per_key_429(self):
        # The stub completes instantly, so an HTTP-level held-open stream
        # cannot exist against it; the cap BRANCH is exercised directly:
        # with the bucket already at its cap the endpoint must 429.
        api, client = self._boot(NEXUS_RAG_MAX_OPEN_STREAMS="1")
        bucket = api._rag_bucket("secret", None)
        api._rag_open_streams[bucket] = 1   # simulate the held-open stream
        try:
            r2 = client.post("/ask/stream",
                             json={"query": "bm25 lexical"},
                             headers={"X-API-Key": "secret"})
            self.assertEqual(r2.status_code, 429)
            self.assertIn("retry-after",
                          {k.lower() for k in r2.headers.keys()})
            self.assertIn("too many open rag streams", r2.json()["detail"])
        finally:
            api._rag_open_streams.pop(bucket, None)
        # slot free again -> accepted
        status, events, _ = self._post_stream(client, "bm25 lexical")
        self.assertEqual(status, 200)
        self.assertEqual(api._rag_open_stream_count(), 0)

    # --------------------------------------------------------- api gates

    def test_auth_and_budget_gates_match_ask(self):
        _, client = self._boot()
        no_key = client.post("/ask/stream", json={"query": "bm25"})
        self.assertEqual(no_key.status_code, 401)
        budgeted = self._boot(
            NEXUS_RAG_DAILY_TOKEN_BUDGET="1500",
            NEXUS_LLM_MAX_TOKENS="256",
            NEXUS_RAG_MAX_CONTEXT_TOKENS="500")
        api2, client2 = budgeted
        for _ in range(10):
            r = client2.post("/ask/stream",
                             json={"query": "bm25 lexical inverted index"},
                             headers={"X-API-Key": "secret"})
            if r.status_code == 429:
                self.assertIn("budget", r.json()["detail"].lower())
                break
        else:
            self.fail("budget never fired on /ask/stream within 10 asks")


if __name__ == "__main__":
    unittest.main()
