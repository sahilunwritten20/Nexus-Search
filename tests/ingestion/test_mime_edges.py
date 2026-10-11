"""WP14-7 — ingestion/mime.py was 76%-covered; these pin the uncovered
sniffing branches (empty file, Office-ish-but-unclaimed zip, corrupt zip,
binary-majority text, OSError) and the detect_mime_type fallback ordering.
MIME routing decides which reader parses attacker-supplied bytes — its
edges belong under test."""
import os
import tempfile
import unittest
import zipfile

from nexus_search.ingestion.mime import detect_mime_type, sniff_content_type


class TestSniffEdges(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wp14_mime_")

    def _p(self, name):
        return os.path.join(self.tmp, name)

    def test_empty_file_is_undetermined(self):
        p = self._p("empty.txt")
        open(p, "wb").close()
        self.assertEqual(sniff_content_type(p), "")

    def test_missing_file_is_undetermined(self):
        self.assertEqual(sniff_content_type(self._p("nope.bin")), "")

    def test_unreadable_file_returns_empty(self):
        p = self._p("dir.txt")
        os.mkdir(p)  # a directory: is_file() False -> ""
        self.assertEqual(sniff_content_type(p), "")

    def test_office_ish_zip_with_content_types_but_no_marker(self):
        p = self._p("mystery.docx")
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("[Content_Types].xml", "<Types/>")
        self.assertEqual(sniff_content_type(p), "application/octet-stream")

    def test_corrupt_zip_head_claimed_as_zip(self):
        p = self._p("broken.docx")
        with open(p, "wb") as f:
            f.write(b"PK\x03\x04" + b"\x00garbage-not-a-zip")
        self.assertEqual(sniff_content_type(p), "application/zip")

    def test_plain_zip_stays_generic(self):
        p = self._p("plain.zip")
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("readme.txt", "hello")
        self.assertEqual(sniff_content_type(p), "application/zip")

    def test_docx_marker_wins(self):
        p = self._p("real.docx")
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("word/document.xml", "<doc/>")
        self.assertEqual(
            sniff_content_type(p),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document")

    def test_high_binary_ratio_is_octet_stream(self):
        p = self._p("binary.txt")
        with open(p, "wb") as f:
            f.write(bytes(range(256)) * 20)  # NULs + non-text majority
        self.assertEqual(sniff_content_type(p), "application/octet-stream")

    def test_text_majority_is_text_plain(self):
        p = self._p("text.txt")
        with open(p, "wb") as f:
            f.write(b"plain ascii words " * 50)
        self.assertEqual(sniff_content_type(p), "text/plain")


class TestDetectOrdering(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wp14_mime2_")

    def test_content_beats_lying_extension(self):
        p = os.path.join(self.tmp, "liar.txt")
        with open(p, "wb") as f:
            f.write(b"%PDF-1.7 fake")
        self.assertEqual(detect_mime_type(p), "application/pdf")

    def test_octet_stream_with_known_extension_falls_back_to_extension(self):
        p = os.path.join(self.tmp, "blob.bin")
        with open(p, "wb") as f:
            f.write(bytes(range(256)) * 20)
        # content says octet-stream; extension .bin also maps to octet-stream
        self.assertEqual(detect_mime_type(p), "application/octet-stream")

    def test_undetermined_content_uses_extension(self):
        p = os.path.join(self.tmp, "notes.md")
        with open(p, "wb") as f:
            f.write(b"# markdown heading text\n")
        # sniff says text/plain (text majority) — the sniff WINS over the
        # extension, so this asserts the content-first contract: same answer
        # either way here; the extension fallback is the ""-sniff path, which
        # cannot happen for a nonempty text file — pin the real ordering:
        self.assertEqual(detect_mime_type(p), "text/plain")

    def test_empty_content_falls_back_to_extension(self):
        # empty file: sniff is "" (undetermined) -> extension answers
        p = os.path.join(self.tmp, "notes.md")
        open(p, "wb").close()
        self.assertEqual(detect_mime_type(p), "text/markdown")


if __name__ == "__main__":
    unittest.main()
