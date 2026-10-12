"""P7-5 — output sanitization: plain text, HTML stripped, markdown
image/link exfil removed, split-token streaming, no eval/exec/template
paths on model output anywhere in the package."""
import os
import unittest

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.rag.sanitize import (
    NoopModerator, StreamSanitizer, sanitize_stream, sanitize_text,
)

XSS = "<script>alert(1)</script><img src=x onerror=alert(1)>"
MD_EXFIL = "See ![leak](http://attacker.example/pixel) and [click](http://attacker.example/link)."


class TestSanitizeText(unittest.TestCase):
    def test_script_blocks_removed_whole(self):
        clean, removed = sanitize_text(f"Safe [1]. {XSS}")
        self.assertEqual(clean, "Safe [1]. ")
        self.assertGreater(removed, 0)
        self.assertNotIn("script", clean)
        self.assertNotIn("alert", clean)
        self.assertNotIn("<", clean)

    def test_markdown_images_removed_entirely(self):
        clean, removed = sanitize_text(MD_EXFIL)
        self.assertNotIn("attacker.example", clean)
        self.assertNotIn("![", clean)
        # link labels survive (may be legitimate citations); URLs do not
        self.assertIn("click", clean)
        self.assertGreater(removed, 0)

    def test_citation_markers_survive(self):
        clean, _ = sanitize_text("The answer is 42 [1] and [2] applies.")
        self.assertIn("[1]", clean)
        self.assertIn("[2]", clean)

    def test_bare_urls_in_parens_neutralized(self):
        clean, removed = sanitize_text("go to (http://attacker.example/x) now")
        self.assertNotIn("attacker.example", clean)
        self.assertIn("[link removed]", clean)

    def test_plain_answers_pass_through_untouched(self):
        text = "Paris is the capital of France [1]. It has been since 508 AD."
        clean, removed = sanitize_text(text)
        self.assertEqual(clean, text)
        self.assertEqual(removed, 0)

    def test_noop_moderator_passes_through(self):
        self.assertIsNone(NoopModerator().moderate("anything"))


class TestStreamSanitizer(unittest.TestCase):
    def _tokens(self, text, size=3):
        return [text[i:i + size] for i in range(0, len(text), size)]

    def assert_reassembles(self, clean_stream, expected_substrings,
                            forbidden, text, size=3):
        out, _ = sanitize_stream(self._tokens(text, size))
        joined = "".join(out)
        for s in expected_substrings:
            self.assertIn(s, joined)
        for f in forbidden:
            self.assertNotIn(f, joined, f"payload {f!r} survived streaming")

    def test_streamed_script_split_mid_token(self):
        # every 1-2 char split must catch the script payload
        for size in (1, 2, 3, 5, 7):
            with self.subTest(size=size):
                self.assert_reassembles(
                    None, ["Safe [1]."], ["script", "alert", "<"],
                    f"Safe [1]. {XSS}", size)

    def test_streamed_markdown_exfil_split_mid_token(self):
        for size in (1, 2, 3, 5, 7):
            with self.subTest(size=size):
                self.assert_reassembles(
                    None, ["See", "click"], ["attacker.example", "!["],
                    MD_EXFIL, size)

    def test_stream_plain_text_unchanged(self):
        text = "Paris is the capital of France [1]."
        out, removed = sanitize_stream(self._tokens(text))
        self.assertEqual("".join(out), text)
        self.assertEqual(removed, 0)

    def test_push_then_flush_drains_the_buffer(self):
        s = StreamSanitizer()
        out = []
        out.append(s.push("answer with an unfinished <scr"))
        out.append(s.push("ipt>alert(1)</script> tail [1]"))
        out.append(s.flush())
        joined = "".join(out)
        self.assertIn("answer with an unfinished", joined)
        self.assertIn("tail [1]", joined)
        self.assertNotIn("alert", joined)
        self.assertGreater(s.removed_count, 0)


class TestNoDynamicExecution(unittest.TestCase):
    def test_package_never_evals_templates_or_fstrings_model_output(self):
        import pathlib
        import re as _re
        rag_dir = pathlib.Path(os.path.dirname(
            os.path.abspath(__import__("nexus_search.rag.sanitize", fromlist=["x"]).__file__)))
        # bare builtin calls only: re.compile( etc. are method calls
        forbidden = (_re.compile(r"(?<![.\w])eval\("),
                     _re.compile(r"(?<![.\w])exec\("),
                     _re.compile(r"(?<![.\w])compile\("),
                     _re.compile(r"(?<![.\w])Template\("),
                     _re.compile(r"\.format\(.*%"),)
        names = ("eval", "exec", "compile", "Template", "str.format on output")
        for path in rag_dir.glob("*.py"):
            with open(path, encoding="utf-8") as fh:
                for i, line in enumerate(fh, 1):
                    for pattern, name in zip(forbidden, names):
                        self.assertIsNone(
                            pattern.search(line),
                            f"{path.name}:{i} uses {name} — model output "
                            "must never be dynamically executed")


if __name__ == "__main__":
    unittest.main()
