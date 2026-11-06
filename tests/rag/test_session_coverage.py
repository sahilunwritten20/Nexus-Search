"""Additional coverage for rag/session.py (currently 77%)"""
import os
import time
import unittest

from nexus_search.rag.session import (
    MemorySessionStore, SqliteSessionStore, SessionExpired, SessionCapExceeded,
    heuristic_condense, condense_followup
)


class DummyProvider:
    """Mock provider for condense_followup LLM mode"""
    def __init__(self, response_text="condensed query"):
        self.response_text = response_text
        self.called = False

    def complete(self, messages, max_tokens, timeout):
        self.called = True
        return type('Result', (), {'text': self.response_text})()


class DummyConfig:
    def __init__(self, max_tokens=128, timeout=30.0):
        self.max_tokens = max_tokens
        self.timeout = timeout


class TestSessionCoverage(unittest.TestCase):
    """Targeted tests for uncovered lines in session.py"""

    def setUp(self):
        self.store = MemorySessionStore(
            max_turns=3,
            max_context_tokens=1000,
            chars_per_token=4,
            max_turn_bytes=100,
            ttl=1.0,  # 1 second TTL for fast tests
            global_cap=10
        )

    def test_memory_get_expired_session_raises(self):
        """Test MemorySessionStore.get raises SessionExpired for expired session (lines 95-96, 98-101)"""
        # Create a session
        self.store.append("scope1", "session1", "user", "Hello")
        
        # Wait for TTL to expire
        time.sleep(1.5)
        
        # Should raise SessionExpired
        with self.assertRaises(SessionExpired):
            self.store.get("scope1", "session1")

    def test_memory_get_expired_session_adds_tombstone(self):
        """Test MemorySessionStore.get adds expired key to _expired (lines 94-101)"""
        self.store.append("scope1", "session1", "user", "Hello")
        time.sleep(1.5)
        
        with self.assertRaises(SessionExpired):
            self.store.get("scope1", "session1")
        
        # Second call should also raise (tombstone exists)
        with self.assertRaises(SessionExpired):
            self.store.get("scope1", "session1")

    def test_memory_append_expired_session_raises(self):
        """Test MemorySessionStore.append raises SessionExpired for expired session (lines 115-122)"""
        self.store.append("scope1", "session1", "user", "Hello")
        time.sleep(1.5)
        
        with self.assertRaises(SessionExpired):
            self.store.append("scope1", "session1", "user", "World")

    def test_memory_append_turn_bytes_cap(self):
        """Test MemorySessionStore.append enforces max_turn_bytes (line 109-110)"""
        long_text = "x" * 150  # exceeds max_turn_bytes=100
        with self.assertRaises(SessionCapExceeded) as cm:
            self.store.append("scope1", "session1", "user", long_text)
        self.assertIn("turn_bytes:150", str(cm.exception))

    def test_memory_append_max_turns_cap(self):
        """Test MemorySessionStore.append enforces max_turns (lines 130-132)"""
        self.store.append("scope1", "session1", "user", "Turn 1")
        self.store.append("scope1", "session1", "assistant", "Turn 2")
        self.store.append("scope1", "session1", "user", "Turn 3")
        # 4th turn should exceed max_turns=3
        with self.assertRaises(SessionCapExceeded) as cm:
            self.store.append("scope1", "session1", "assistant", "Turn 4")
        self.assertIn("turns:4", str(cm.exception))

    def test_memory_append_max_context_tokens_cap(self):
        """Test MemorySessionStore.append enforces max_context_tokens (lines 133-137)"""
        # Each turn ~25 chars -> ~7 tokens (chars_per_token=4)
        # max_context_tokens=20 is small enough
        store = MemorySessionStore(
            max_turns=10,
            max_context_tokens=20,  # Small cap
            chars_per_token=4,
            max_turn_bytes=100,
            ttl=3600,
            global_cap=10
        )
        store.append("scope1", "session1", "user", "This is a long first turn text")
        store.append("scope1", "session1", "assistant", "This is a long second turn text")
        # Third turn should exceed context tokens
        with self.assertRaises(SessionCapExceeded) as cm:
            store.append("scope1", "session1", "user", "This is a long third turn text")
        self.assertIn("context_tokens", str(cm.exception))

    def test_memory_evict_idle_locked(self):
        """Test MemorySessionStore._evict_idle_locked (lines 149-153)"""
        self.store.append("scope1", "session1", "user", "Hello")
        self.store.append("scope2", "session2", "user", "Hello")
        time.sleep(1.5)
        
        # Trigger eviction by calling append (which calls _evict_idle_locked)
        self.store.append("scope3", "session3", "user", "New")
        
        # session1 and session2 should be evicted (not in active sessions)
        # Note: get() raises SessionExpired for tombstones, returns None for never-existing
        self.assertNotIn(("scope1", "session1"), self.store._sessions)
        self.assertNotIn(("scope2", "session2"), self.store._sessions)

    def test_memory_evict_lru_locked(self):
        """Test MemorySessionStore._evict_lru_locked (lines 155-159)"""
        # Fill up to global_cap
        for i in range(10):
            self.store.append(f"scope{i}", f"session{i}", "user", "Hello")
        
        # Add one more - should evict LRU
        self.store.append("scope10", "session10", "user", "Hello")
        
        # session0 should be evicted (oldest)
        self.assertNotIn(("scope0", "session0"), self.store._sessions)

    def test_memory_prune_expired(self):
        """Test MemorySessionStore.prune_expired (lines 165-169)"""
        self.store.append("scope1", "session1", "user", "Hello")
        self.store.append("scope2", "session2", "user", "Hello")
        time.sleep(1.5)
        
        pruned = self.store.prune_expired()
        self.assertEqual(pruned, 2)

    def test_sqlite_get_expired_raises(self):
        """Test SqliteSessionStore.get raises SessionExpired for expired (lines 197-214)"""
        import tempfile
        from nexus_search.core.storage import Storage
        
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            storage = Storage(path)
            store = SqliteSessionStore(
                storage,
                max_turns=10,
                max_context_tokens=1000,
                chars_per_token=4,
                max_turn_bytes=100,
                ttl=1.0,
                global_cap=10
            )
            
            store.append("scope1", "session1", "user", "Hello")
            time.sleep(1.5)
            
            with self.assertRaises(SessionExpired):
                store.get("scope1", "session1")
        finally:
            storage.close()
            os.remove(path)

    def test_sqlite_append_expired_raises(self):
        """Test SqliteSessionStore.append raises SessionExpired for expired (lines 227-230)"""
        import tempfile
        import os
        from nexus_search.core.storage import Storage
        
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            storage = Storage(path)
            store = SqliteSessionStore(
                storage,
                max_turns=10,
                max_context_tokens=1000,
                chars_per_token=4,
                max_turn_bytes=100,
                ttl=1.0,
                global_cap=10
            )
            
            store.append("scope1", "session1", "user", "Hello")
            time.sleep(1.5)
            
            with self.assertRaises(SessionExpired):
                store.append("scope1", "session1", "user", "World")
        finally:
            storage.close()
            os.remove(path)

    def test_sqlite_append_max_turns_cap(self):
        """Test SqliteSessionStore.append enforces max_turns (lines 231-232)"""
        import tempfile
        import os
        from nexus_search.core.storage import Storage
        
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            storage = Storage(path)
            store = SqliteSessionStore(
                storage,
                max_turns=2,
                max_context_tokens=1000,
                chars_per_token=4,
                max_turn_bytes=100,
                ttl=3600,
                global_cap=10
            )
            
            store.append("scope1", "session1", "user", "Turn 1")
            store.append("scope1", "session1", "assistant", "Turn 2")
            
            with self.assertRaises(SessionCapExceeded) as cm:
                store.append("scope1", "session1", "user", "Turn 3")
            self.assertIn("turns:3", str(cm.exception))
        finally:
            storage.close()
            os.remove(path)

    def test_sqlite_append_max_context_tokens_cap(self):
        """Test SqliteSessionStore.append enforces max_context_tokens (lines 233-237)"""
        import tempfile
        import os
        from nexus_search.core.storage import Storage
        
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            storage = Storage(path)
            store = SqliteSessionStore(
                storage,
                max_turns=10,
                max_context_tokens=20,
                chars_per_token=4,
                max_turn_bytes=100,
                ttl=3600,
                global_cap=10
            )
            
            store.append("scope1", "session1", "user", "This is a long first turn text")
            store.append("scope1", "session1", "assistant", "This is a long second turn text")
            
            with self.assertRaises(SessionCapExceeded) as cm:
                store.append("scope1", "session1", "user", "This is a long third turn text")
            self.assertIn("context_tokens", str(cm.exception))
        finally:
            storage.close()
            os.remove(path)

    def test_sqlite_append_expired_session_resurrection_prevented(self):
        """Test SqliteSessionStore.append prevents resurrection (lines 227-240)"""
        import tempfile
        import os
        from nexus_search.core.storage import Storage
        
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            storage = Storage(path)
            store = SqliteSessionStore(
                storage,
                max_turns=10,
                max_context_tokens=1000,
                chars_per_token=4,
                max_turn_bytes=100,
                ttl=1.0,
                global_cap=10
            )
            
            store.append("scope1", "session1", "user", "Hello")
            time.sleep(1.5)
            
            # First append should raise and delete
            with self.assertRaises(SessionExpired):
                store.append("scope1", "session1", "user", "World")
            
            # Second append should also raise (tombstone)
            with self.assertRaises(SessionExpired):
                store.append("scope1", "session1", "user", "Again")
        finally:
            storage.close()
            os.remove(path)

    def test_heuristic_condense_various(self):
        """Test heuristic_condense edge cases (lines 264-281)"""
        # No history
        self.assertEqual(heuristic_condense("query", ""), "query")
        
        # Short query with history
        self.assertEqual(heuristic_condense("more", "User: Hello\nAssistant: Hi"), "Hello more")
        
        # Long query (not follow-up)
        self.assertEqual(heuristic_condense("what is the capital of france", "User: Hello"), "what is the capital of france")
        
        # Follow-up marker
        self.assertEqual(heuristic_condense("what about it", "User: Hello"), "Hello what about it")
        
        # No previous user turn
        self.assertEqual(heuristic_condense("more", "Assistant: Hi"), "more")

    def test_condense_followup_heuristic_mode(self):
        """Test condense_followup heuristic mode (lines 284-296)"""
        # No history
        self.assertEqual(condense_followup("query", "", mode="heuristic"), "query")
        
        # Off mode
        self.assertEqual(condense_followup("query", "history", mode="off"), "query")
        
        # Heuristic mode
        result = condense_followup("more", "User: Hello", mode="heuristic")
        self.assertEqual(result, "Hello more")

    def test_condense_followup_llm_mode_success(self):
        """Test condense_followup LLM mode success (lines 297-306)"""
        provider = DummyProvider("rewritten query")
        config = DummyConfig()
        
        def messages_builder(history, query):
            return f"History: {history}\nQuery: {query}"
        
        result = condense_followup(
            "more", "User: Hello",
            mode="llm",
            provider=provider,
            config=config,
            messages_builder=messages_builder
        )
        self.assertEqual(result, "rewritten query")
        self.assertTrue(provider.called)

    def test_condense_followup_llm_mode_fallback(self):
        """Test condense_followup LLM mode fallback on error (lines 308-309)"""
        # Provider raises ProviderError
        from nexus_search.rag.provider import ProviderError
        
        class FailingProvider:
            def complete(self, messages, max_tokens, timeout):
                raise ProviderError("provider down")
        
        provider = FailingProvider()
        config = DummyConfig()
        
        def messages_builder(history, query):
            return f"History: {history}\nQuery: {query}"
        
        result = condense_followup(
            "more", "User: Hello",
            mode="llm",
            provider=provider,
            config=config,
            messages_builder=messages_builder
        )
        # Should fall back to heuristic
        self.assertEqual(result, "Hello more")

    def test_condense_followup_llm_mode_no_provider(self):
        """Test condense_followup LLM mode with no provider (line 299)"""
        result = condense_followup(
            "more", "User: Hello",
            mode="llm",
            provider=None,
            config=None,
            messages_builder=None
        )
        self.assertEqual(result, "Hello more")

    def test_condense_followup_llm_mode_bad_result(self):
        """Test condense_followup LLM mode with bad result (lines 304-307)"""
        class BadProvider:
            def complete(self, messages, max_tokens, timeout):
                return type('Result', (), {'text': ""})  # empty result
        
        provider = BadProvider()
        config = DummyConfig()
        
        def messages_builder(history, query):
            return f"History: {history}\nQuery: {query}"
        
        result = condense_followup(
            "more", "User: Hello",
            mode="llm",
            provider=provider,
            config=config,
            messages_builder=messages_builder
        )
        # Empty result should fall back to heuristic
        self.assertEqual(result, "Hello more")

    def test_condense_followup_llm_mode_long_result(self):
        """Test condense_followup LLM mode with too long result (line 305)"""
        class LongProvider:
            def complete(self, messages, max_tokens, timeout):
                return type('Result', (), {'text': "x" * 1500})  # > 1000
        
        provider = LongProvider()
        config = DummyConfig()
        
        def messages_builder(history, query):
            return f"History: {history}\nQuery: {query}"
        
        result = condense_followup(
            "more", "User: Hello",
            mode="llm",
            provider=provider,
            config=config,
            messages_builder=messages_builder
        )
        # Too long should fall back to heuristic
        self.assertEqual(result, "Hello more")


if __name__ == "__main__":
    unittest.main()