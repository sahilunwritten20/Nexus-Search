import os
import shutil
import tempfile
import unittest
from pathlib import Path

from nexus_search.ingestion.connectors.files import iter_files, read_file, DEFAULT_MAX_INGEST_BYTES


class TestFilesConnector(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        Path(self.root, "a.txt").write_text("hello from a")
        Path(self.root, "b.md").write_text("# hello from b")
        Path(self.root, "ignored.bin").write_bytes(b"\x00\x01\x02")
        sub = Path(self.root, "sub")
        sub.mkdir()
        Path(sub, "c.txt").write_text("nested file content")

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_finds_txt_and_md_files(self):
        docs = list(iter_files(self.root))
        doc_ids = {d.doc_id for d in docs}
        self.assertIn("file:a.txt", doc_ids)
        self.assertIn("file:b.md", doc_ids)

    def test_ignores_non_text_extensions(self):
        docs = list(iter_files(self.root))
        self.assertFalse(any("ignored.bin" in d.doc_id for d in docs))

    def test_recurses_into_subdirectories(self):
        docs = list(iter_files(self.root))
        self.assertTrue(any("sub" in d.doc_id and "c.txt" in d.doc_id for d in docs))

    def test_content_is_read_correctly(self):
        docs = {d.doc_id: d for d in iter_files(self.root)}
        self.assertEqual(docs["file:a.txt"].content, "hello from a")

    def test_doc_type_is_file(self):
        docs = list(iter_files(self.root))
        self.assertTrue(all(d.doc_type == "file" for d in docs))

    def test_custom_extensions_filter(self):
        docs = list(iter_files(self.root, extensions={".md"}))
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].doc_id, "file:b.md")


class TestSizeLimit(unittest.TestCase):
    """NEXUS_MAX_INGEST_BYTES: refused BEFORE reading, boundary-exact."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        os.environ.pop("NEXUS_MAX_INGEST_BYTES", None)

    def test_oversized_file_skipped(self):
        os.environ["NEXUS_MAX_INGEST_BYTES"] = "10"
        f = self.dir / "big.txt"
        f.write_text("x" * 11, encoding="utf-8")
        self.assertEqual(read_file(f), "")

    def test_exactly_at_limit_is_read(self):
        os.environ["NEXUS_MAX_INGEST_BYTES"] = "10"
        f = self.dir / "edge.txt"
        f.write_text("x" * 10, encoding="utf-8")
        self.assertEqual(read_file(f), "x" * 10)

    def test_under_limit_unaffected(self):
        os.environ["NEXUS_MAX_INGEST_BYTES"] = "100"
        f = self.dir / "ok.txt"
        f.write_text("hello", encoding="utf-8")
        self.assertEqual(read_file(f), "hello")

    def test_invalid_env_value_falls_back_to_default(self):
        os.environ["NEXUS_MAX_INGEST_BYTES"] = "not-a-number"
        self.assertGreater(DEFAULT_MAX_INGEST_BYTES, 0)
        f = self.dir / "small.txt"
        f.write_text("hello", encoding="utf-8")
        self.assertEqual(read_file(f), "hello")

    def test_size_limit_applies_to_all_readers(self):
        os.environ["NEXUS_MAX_INGEST_BYTES"] = "5"
        f = self.dir / "big.json"
        f.write_text('{"k": "' + "v" * 100 + '"}', encoding="utf-8")
        self.assertEqual(read_file(f), "")

    def test_iter_files_skips_oversized(self):
        os.environ["NEXUS_MAX_INGEST_BYTES"] = "10"
        (self.dir / "small.txt").write_text("ok ok", encoding="utf-8")
        (self.dir / "big.txt").write_text("x" * 5000, encoding="utf-8")
        docs = list(iter_files(str(self.dir)))
        self.assertEqual(len(docs), 1)


class TestOcrFallback(unittest.TestCase):
    """OCR is opt-in (NEXUS_OCR=1) and degrades honestly when unavailable."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        os.environ.pop("NEXUS_OCR", None)

    def _image_pdf(self, name="scan.pdf"):
        # a minimal valid PDF with an empty page: no text layer to extract
        f = self.dir / name
        f.write_bytes(
            b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 72 72]>>endobj\n"
            b"trailer<</Root 1 0 R>>\n%%EOF")
        return f

    def test_ocr_off_by_default_returns_empty(self):
        os.environ.pop("NEXUS_OCR", None)
        out = read_file(self._image_pdf())
        self.assertEqual(out, "")  # unchanged pre-fix behavior

    def test_ocr_flag_without_tesseract_degrades_not_crashes(self):
        import shutil as _sh
        os.environ["NEXUS_OCR"] = "1"
        if _sh.which("tesseract"):
            self.skipTest("tesseract present; the no-binary path is untestable here")
        out = read_file(self._image_pdf())
        self.assertEqual(out, "")  # logged warning, empty result, no exception


if __name__ == "__main__":
    unittest.main()
