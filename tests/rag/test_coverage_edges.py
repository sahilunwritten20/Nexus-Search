"""P7-10 — coverage closure for the rag package (target >= 90%): the
untested branches in session stores (sqlite expiry paths, prune, cap
errors, history rendering) and pipeline streaming/refusal edge paths.
Every test pins real behavior, not coverage cosmetics."""
import os
import tempfile
import time
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.core.indexer import Indexer
from nexus_search.core.storage import Storage
from nexus_search.core.vector_store import VectorStoreManager
from nexus_search.core.embedding_sync import EmbeddingSync
from nexus_search.core.hybrid_search import HybridSearch
from nexus_search.rag.config import RagConfig
from nexus_search.rag.context import make_storage_neighbor_fetcher
from nexus_search.rag.metrics import RagMetrics
from nexus_search.rag.pipeline import RagService
from nexus_search.rag.provider import StubProvider, ProviderError
from nexus_search.rag.retrieval import RagRetriever
from nexus_search.rag.session import (
    MemorySessionStore, SessionCapExceeded, SessionExpired, Session,
    SqliteSessionStore, heuristic_condense,
)


def _build(tmp_name="cov.db"):
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, tmp_name)
    storage = Storage(db)
    indexer = Indexer(storage)
    vs = VectorStoreManager(db)
    sync = EmbeddingSync(vs, batch_size=64)
    sync.attach(indexer)
    indexer.add_document(
        "bm25-doc", "BM25 scores lexical matches over an inverted index. "
        "Term frequency and inverse document frequency drive the ranking.",
        title="BM25")
    indexer.add_document(
        "vector-doc", "Dense embeddings map text into a vector space where "
        "cosine similarity approximates semantic relatedness.",
        title="Vectors")
    sync.flush()
    hybrid = HybridSearch(storage, vector_store=vs, db_path=db)
    config = RagConfig(env="dev")
    retriever = RagRetriever(hybrid, storage, link_intel=None)
    service = RagService(config, retriever, StubProvider(), storage,
                         metrics=RagMetrics(),
                         neighbor_fetcher=make_storage_neighbor_fetcher(storage))
    return storage, hybrid, vs, sync, service


def _memstore(**kw):
    return MemorySessionStore(max_turns=kw.get("max_turns", 10),
                              max_context_tokens=kw.get("max_context_tokens", 10**6),
                              chars_per_token=4,
                              max_turn_bytes=kw.get("max_turn_bytes", 2000),
                              ttl=kw.get("ttl", 3600),
                              global_cap=kw.get("global_cap", 100))


class TestMemoryStoreEdges(unittest.TestCase):
    def test_global_cap_evicts_lru_and_marks_expired(self):
        store = _memstore(global_cap=2, ttl=3600)
        store.append("k1", "a", "user", "one")
        time.sleep(0.01)
        store.append("k2", "b", "user", "two")
        store.append("k3", "c", "user", "three")     # evicts k1 (LRU)
        self.assertEqual(store.count(), 2)
        with self.assertRaises(SessionExpired):
            store.get("k1", "a")                      # tombstone -> expired

    def test_idle_eviction_on_get_marks_tombstone(self):
        store = _memstore(ttl=3600)
        store.append("k", "s", "user", "hello")
        store._sessions[("k", "s")].last_used = time.time() - 7200
        with self.assertRaises(SessionExpired):
            store.get("k", "s")
        self.assertEqual(store.count(), 0)

    def test_append_after_ttl_gap_raises_expired(self):
        store = _memstore(ttl=3600)
        store.append("k", "s", "user", "hello")
        store._sessions[("k", "s")].last_used = time.time() - 7200
        with self.assertRaises(SessionExpired):
            store.append("k", "s", "assistant", "late")

    def test_prune_expired_counts_idle(self):
        store = _memstore(ttl=3600)
        store.append("k", "s", "user", "x")
        self.assertEqual(store.prune_expired(), 0)
        store._sessions[("k", "s")].last_used = time.time() - 7200
        self.assertEqual(store.prune_expired(), 1)

    def test_history_render_caps_turns(self):
        session = Session(turns=[], last_used=time.time())
        session.turns = [type("T", (), {"role": "user", "text": f"q{i}",
                                        "ts": 0})() for i in range(8)]
        text = session.history_text(3)
        self.assertEqual(len(text.splitlines()), 3)
        self.assertIn("q7", text)


class TestSqliteStoreEdges(unittest.TestCase):
    def test_turn_and_context_caps_raise_clearly(self):
        tmp = tempfile.mkdtemp()
        s = Storage(os.path.join(tmp, "e.db"))
        try:
            # turns cap: 2 turns max -> second append exceeds
            store = SqliteSessionStore(s, max_turns=2,
                                        max_context_tokens=10**6,
                                        chars_per_token=4, max_turn_bytes=100,
                                        ttl=3600, global_cap=10)
            store.append("k", "s", "user", "hello there friend")
            store.append("k", "s", "assistant", "reply")     # 2 turns: fits
            with self.assertRaises(SessionCapExceeded) as ctx:
                store.append("k", "s", "user", "third turn over the cap")
            self.assertIn("turns", str(ctx.exception))
            # context-token cap on the FIRST append
            store2 = SqliteSessionStore(s, max_turns=10,
                                         max_context_tokens=3,
                                         chars_per_token=4, max_turn_bytes=100,
                                         ttl=3600, global_cap=10)
            with self.assertRaises(SessionCapExceeded) as ctx2:
                store2.append("k2", "s", "user", "way more than three tokens")
            self.assertIn("context_tokens", str(ctx2.exception))
        finally:
            s.close()

    def test_get_after_ttl_delete_raises_expired(self):
        tmp = tempfile.mkdtemp()
        s = Storage(os.path.join(tmp, "e2.db"))
        try:
            store = SqliteSessionStore(s, max_turns=10,
                                       max_context_tokens=10**6,
                                       chars_per_token=4, max_turn_bytes=100,
                                       ttl=0.2, global_cap=10)
            store.append("k", "s", "user", "x")
            time.sleep(0.3)
            with self.assertRaises(SessionExpired):
                store.get("k", "s")
            self.assertEqual(s.rag_session_count(), 0)   # pruned on read
        finally:
            s.close()

    def test_tombstone_blocks_reopen(self):
        # the realistic path: get() on an idle-past-TTL session deletes
        # the rows AND tombstones; a later append then refuses honestly
        tmp = tempfile.mkdtemp()
        s = Storage(os.path.join(tmp, "e3.db"))
        try:
            store = SqliteSessionStore(s, max_turns=10,
                                       max_context_tokens=10**6,
                                       chars_per_token=4, max_turn_bytes=100,
                                       ttl=0.2, global_cap=10)
            store.append("k", "s", "user", "x")
            time.sleep(0.3)
            with self.assertRaises(SessionExpired):
                store.get("k", "s")      # deletes rows + tombstones
            with self.assertRaises(SessionExpired):
                store.append("k", "s", "user", "again")
            self.assertEqual(s.rag_session_count(), 0)
        finally:
            s.close()

    def test_prune_expired_deletes_old_rows(self):
        tmp = tempfile.mkdtemp()
        s = Storage(os.path.join(tmp, "e4.db"))
        try:
            store = SqliteSessionStore(s, max_turns=10,
                                       max_context_tokens=10**6,
                                       chars_per_token=4, max_turn_bytes=100,
                                       ttl=3600, global_cap=10)
            store.append("k", "old", "user", "x")
            with s.lock:
                s.conn.execute(
                    "UPDATE rag_sessions SET updated_at = ? "
                    "WHERE session_id = 'old'", (time.time() - 999999,))
                s.conn.commit()
            self.assertEqual(store.prune_expired(), 1)
            self.assertIsNone(store.get("k", "old"))
        finally:
            s.close()


class TestHeuristicCondenseEdges(unittest.TestCase):
    def test_marker_and_short_queries_splice_history(self):
        history = "User: what is bm25 ranking"
        self.assertEqual(heuristic_condense("why", history),
                         "what is bm25 ranking why")
        self.assertEqual(heuristic_condense("tell me more", history),
                         "what is bm25 ranking tell me more")
        # long standalone question passes through even with markers absent
        self.assertEqual(heuristic_condense("a standalone question about "
                                            "crawlers and frontier queues",
                                            history),
                         "a standalone question about crawlers and "
                         "frontier queues")

    def test_no_prev_user_line_falls_back_to_query(self):
        self.assertEqual(heuristic_condense("it", "Assistant: hello"),
                         "it")


class TestStreamingEdges(unittest.TestCase):
    """Pipeline streaming edge paths: refusal/degraded stream events,
    llm-condense failure fallback in-stream, provider error mid-stream."""
    @classmethod
    def setUpClass(cls):
        cls.storage, cls.hybrid, cls.vs, cls.sync, cls.service = _build()

    @classmethod
    def tearDownClass(cls):
        cls.sync.close(); cls.hybrid.close(); cls.vs.close()
        cls.storage.close()

    def _collect(self, gen):
        return list(gen)

    def test_refusal_stream_shape(self):
        events = self._collect(self.service.ask_streaming(
            "best recipe for sourdough bread at home"))
        kinds = [e["event"] for e in events]
        self.assertEqual(kinds[0], "meta")
        self.assertNotIn("token", kinds)
        self.assertEqual(kinds[-1], "done")
        self.assertFalse(events[0]["data"]["answerable"])

    def test_degraded_provider_timeout_stream_shape(self):
        class SlowStub(StubProvider):
            def complete(self, messages, *, max_tokens, timeout=None,
                         stream=False):
                raise TimeoutError() if False else ProviderError("stub_error")
        service = RagService(self.service.config, self.service.retriever,
                             SlowStub(), self.storage, metrics=RagMetrics(),
                             neighbor_fetcher=self.service._neighbor_fetcher)
        events = self._collect(service.ask_streaming(
            "bm25 lexical inverted index ranking"))
        kinds = [e["event"] for e in events]
        self.assertIn("error", kinds)
        self.assertEqual(kinds[-1], "done")
        self.assertEqual(events[-1]["data"]["degraded_reason"],
                         "provider_error")

    def test_refusal_with_session_stores_turns(self):
        service = RagService(
            self.service.config, self.service.retriever,
            StubProvider(), self.storage, metrics=RagMetrics(),
            neighbor_fetcher=self.service._neighbor_fetcher,
            session_store=_memstore())
        service.ask("best recipe for sourdough bread",
                    session_id="r1", scope="k")
        service.ask("bm25 lexical inverted index ranking",
                    session_id="r1", scope="k")
        session = service.sessions.get("k", "r1")
        roles = [t.role for t in session.turns]
        self.assertEqual(roles.count("user"), 2)
        self.assertEqual(roles.count("assistant"), 2)  # incl. refusal note

    def test_llm_condense_stream_falls_back_on_provider_error(self):
        from nexus_search.rag.config import RagConfig
        import dataclasses
        from nexus_search.rag.provider import LLMResult, LLMUsage

        class CondenseOnlyError(StubProvider):
            """Fails ONLY condense calls; answers normally otherwise —
            proves the fallback keeps the stream alive."""

            def complete(self, messages, *, max_tokens, timeout=None,
                         stream=False):
                if "CONDENSE" in messages[-1]["content"]:
                    raise ProviderError("stub_error")
                if stream:
                    return super().complete(messages, max_tokens=max_tokens,
                                            timeout=timeout, stream=True)
                return LLMResult(text="BM25 scores lexical matches [1]",
                                 usage=LLMUsage(10, 8),
                                 model=self.model, provider=self.name)

        cfg = dataclasses.replace(self.service.config,
                                  followup_condense="llm")
        service = RagService(cfg, self.service.retriever,
                             CondenseOnlyError(), self.storage,
                             metrics=RagMetrics(),
                             neighbor_fetcher=self.service._neighbor_fetcher,
                             session_store=_memstore())
        seen = []
        original = service.retriever.retrieve

        def spy(query, *a, **kw):
            seen.append(query)
            return original(query, *a, **kw)

        service.retriever.retrieve = spy
        service.ask("bm25 lexical inverted index",
                    session_id="c1", scope="k")
        events = self._collect(service.ask_streaming(
            "and the ranking function?", session_id="c1", scope="k"))
        kinds = [e["event"] for e in events]
        self.assertIn("token", kinds)     # heuristic fallback worked
        self.assertIn("citations", kinds)
        # the condense failed -> heuristic still spliced the history
        self.assertIn("bm25 lexical inverted index", seen[-1])


if __name__ == "__main__":
    unittest.main()
