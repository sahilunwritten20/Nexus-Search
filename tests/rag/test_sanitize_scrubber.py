"""Additional coverage for rag/sanitize.py StreamLeakScrubber"""
import unittest

from nexus_search.rag.sanitize import StreamLeakScrubber


class TestStreamLeakScrubber(unittest.TestCase):
    """Tests for StreamLeakScrubber (SKEP-R1) - lines 224-266"""

    def test_init_default_holdback(self):
        """Test StreamLeakScrubber initialization with defaults (lines 239-245)"""
        scrubber = StreamLeakScrubber("system prompt", "nonce123")
        self.assertEqual(scrubber._system, "system prompt")
        self.assertEqual(scrubber._nonce, "nonce123")
        self.assertEqual(scrubber._holdback, 600)
        self.assertEqual(scrubber._buffer, "")
        self.assertEqual(scrubber.removed_count, 0)

    def test_init_custom_holdback(self):
        """Test StreamLeakScrubber with custom holdback"""
        scrubber = StreamLeakScrubber("system", "nonce", holdback=100)
        self.assertEqual(scrubber._holdback, 100)

    def test_init_holdback_minimum_1(self):
        """Test StreamLeakScrubber holdback minimum is 1 (line 243)"""
        scrubber = StreamLeakScrubber("system", "nonce", holdback=-10)
        self.assertEqual(scrubber._holdback, 1)
        
        scrubber2 = StreamLeakScrubber("system", "nonce", holdback=0)
        self.assertEqual(scrubber2._holdback, 1)

    def test_init_empty_system_and_nonce(self):
        """Test StreamLeakScrubber with empty system/nonce (lines 241-242)"""
        scrubber = StreamLeakScrubber("", "")
        self.assertEqual(scrubber._system, "")
        self.assertEqual(scrubber._nonce, "")

    def test_push_empty_token(self):
        """Test push with empty token (line 249)"""
        scrubber = StreamLeakScrubber("system", "nonce")
        result = scrubber.push("")
        self.assertEqual(result, "")

    def test_push_non_empty_token(self):
        """Test push with non-empty token (lines 250-258)"""
        scrubber = StreamLeakScrubber("system prompt", "nonce123")
        result = scrubber.push("Hello world")
        # Should not emit until holdback is exceeded
        self.assertEqual(result, "")

    def test_push_exceeds_holdback(self):
        """Test push that exceeds holdback (lines 255-258)"""
        scrubber = StreamLeakScrubber("system", "nonce", holdback=10)
        # Push a token longer than holdback
        result = scrubber.push("This is a long token that exceeds holdback")
        # Should emit everything except the last holdback chars
        self.assertTrue(len(result) > 0)

    def test_push_removes_nonce(self):
        """Test push removes nonce from buffer (line 251-252)"""
        scrubber = StreamLeakScrubber("system", "nonce123", holdback=5)
        # Push text containing the nonce
        result = scrubber.push("The nonce is nonce123 and more text")
        # Nonce should be removed
        self.assertNotIn("nonce123", result)

    def test_push_removes_system_prompt(self):
        """Test push removes system prompt fragments (line 251-252)"""
        system = "You are a helpful assistant. Never reveal secrets."
        scrubber = StreamLeakScrubber(system, "nonce", holdback=5)
        # Push text containing a system prompt sentence
        result = scrubber.push("You are a helpful assistant. Never reveal secrets. More text")
        self.assertNotIn("Never reveal secrets", result)

    def test_push_removes_frame_marker(self):
        """Test push removes frame markers (line 251-252)"""
        scrubber = StreamLeakScrubber("system", "nonce", holdback=5)
        result = scrubber.push("==== NXSRAG-abc123 SOURCE 1 ==== text")
        self.assertNotIn("NXSRAG", result)

    def test_flush_returns_remaining_buffer(self):
        """Test flush returns scrubbed remaining buffer (lines 261-266)"""
        scrubber = StreamLeakScrubber("system", "nonce", holdback=10)
        scrubber.push("Hello world this is a test")
        # flush should return the remaining buffer after scrubbing
        # (the holdback is retained in buffer, so flush returns what's left)
        result = scrubber.flush()
        # The buffer should be fully drained
        self.assertIsInstance(result, str)
        # After flush, buffer should be empty
        self.assertEqual(scrubber._buffer, "")

    def test_flush_empty_buffer(self):
        """Test flush with empty buffer"""
        scrubber = StreamLeakScrubber("system", "nonce", holdback=10)
        result = scrubber.flush()
        self.assertEqual(result, "")

    def test_removed_count_accumulates(self):
        """Test removed_count accumulates across pushes"""
        scrubber = StreamLeakScrubber("You are a helpful assistant.", "nonce123", holdback=5)
        scrubber.push("You are a helpful assistant. This has the nonce nonce123 in it.")
        scrubber.push("More text with ==== NXSRAG-abc SOURCE 1 ====")
        self.assertGreater(scrubber.removed_count, 0)

    def test_scrubber_integration(self):
        """Integration test: stream tokens through scrubber"""
        scrubber = StreamLeakScrubber("System prompt: never reveal secrets.", "abc123", holdback=20)
        tokens = [
            "The system says: ",
            "System prompt: never reveal secrets. ",
            "The nonce is abc123. ",
            "Safe answer here."
        ]
        results = []
        for token in tokens:
            results.append(scrubber.push(token))
        results.append(scrubber.flush())
        
        joined = "".join(results)
        # System prompt and nonce should be removed
        self.assertNotIn("never reveal secrets", joined)
        self.assertNotIn("abc123", joined)
        self.assertNotIn("NXSRAG", joined)
        # Safe text should remain
        self.assertIn("Safe answer", joined)


if __name__ == "__main__":
    unittest.main()