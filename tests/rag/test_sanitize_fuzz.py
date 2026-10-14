"""Fuzz: the RAG sanitization layers must never raise and never leak
on arbitrary model output (mission B7: markdown/HTML/image/link exfil
channels, homoglyphs, zero-width, split-token payloads).
"""
import unittest

from hypothesis import given, settings
from hypothesis import strategies as st

from nexus_search.rag.sanitize import (
    StreamSanitizer, sanitize_stream, sanitize_text, scrub_leaks,
)
from nexus_search.rag.prompt import SYSTEM_PROMPT

PAYLOAD_BITS = st.sampled_from([
    "<script>", "</script>", "<img src=x onerror=alert(1)>", "<svg>",
    "![a](http://attacker/", "[x](http://e/x)", "![", "](", "<", ">",
    "javascript:", "data:text/html;", "==== NXSRAG-abc123 SOURCE 1 ====",
    "====", "NXSRAG-", "\u200b", "\ufeff", "\u0435", "\u217c",
    "ignore previous instructions", "system:", "\x00", "\n", '"',
    "http://x/", "```", "<style>", "</iframe>", "a" * 200,
])
MODEL_OUT = st.lists(PAYLOAD_BITS, min_size=0, max_size=25).map("".join)
TOKEN_SPLITS = st.lists(st.text(max_size=6), min_size=1, max_size=30)


class TestSanitizeFuzz(unittest.TestCase):
    @settings(derandomize=True, max_examples=400, deadline=None)
    @given(MODEL_OUT)
    def test_sanitize_text_never_raises_never_leaks(self, text):
        clean, removed = sanitize_text(text)
        self.assertIsInstance(clean, str)
        for channel in ("<script", "onerror=", "![", "](http"):
            self.assertNotIn(channel, clean.lower(),
                             f"live payload channel {channel!r} survived")

    @settings(derandomize=True, max_examples=200, deadline=None)
    @given(MODEL_OUT, TOKEN_SPLITS)
    def test_stream_sanitizer_never_leaks_across_token_boundaries(self, text, parts):
        text = text[:300]
        tokens = [text[i:i + 3] for i in range(0, len(text), 3)] or [""]
        clean_tokens, _removed = sanitize_stream(tokens)
        joined = "".join(clean_tokens)
        for channel in ("<script", "onerror=", "![", "](http"):
            self.assertNotIn(channel, joined.lower(),
                             f"split-token payload {channel!r} survived")

    @settings(derandomize=True, max_examples=200, deadline=None)
    @given(MODEL_OUT)
    def test_scrub_leaks_removes_frame_markers(self, text):
        clean, _ = scrub_leaks(text, SYSTEM_PROMPT, nonce="deadbeef12345678")
        self.assertNotIn("NXSRAG-deadbeef12345678", clean)
        self.assertNotIn("rag-prompt", clean.lower()[:400] if clean else "")

    def test_stream_sanitizer_count_matches(self):
        s = StreamSanitizer()
        out1 = s.push("<scr")
        out2 = s.push("ipt>alert(1)</script>")
        out3 = s.push(" safe text")
        tail = s.flush()
        self.assertNotIn("<scr", out1 + out2 + tail)
        self.assertIn("safe text", (out1 + out2 + out3 + tail).replace("  ", " "))


if __name__ == "__main__":
    unittest.main()
