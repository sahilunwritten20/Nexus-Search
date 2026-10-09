import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from nexus_search.ingestion.connectors.files import iter_files, read_file, DEFAULT_MAX_INGEST_BYTES


class TestDecompressionBombGuard(unittest.TestCase):
    """P1-12a: zip-container readers (docx/xlsx/pptx) must refuse containers
    whose DECLARED decompressed payload exceeds NEXUS_MAX_DECOMPRESSED_BYTES
    BEFORE any reader materializes it, and the PDF reader must refuse
    absurd page counts (NEXUS_MAX_PDF_PAGES). NEXUS_MAX_INGEST_BYTES caps
    the FILE size, not the payload — a small 'docx' can declare megabytes
    of XML and a real bomb declares gigabytes."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        os.environ.pop("NEXUS_MAX_DECOMPRESSED_BYTES", None)
        os.environ.pop("NEXUS_MAX_PDF_PAGES", None)

    def _pad_with_huge_declared_entry(self, src: Path, pad_bytes: int) -> Path:
        """Re-zip a real OOXML file plus one entry with a big HONEST declared
        size (highly compressible filler) — the zip-bomb shape."""
        out = self.dir / ("bomb_" + src.name)
        with zipfile.ZipFile(str(src)) as zin, \
                zipfile.ZipFile(str(out), "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                zout.writestr(info, zin.read(info.filename))
            zout.writestr("pad/padding.xml", b"<pad>" + b"A" * (pad_bytes - 5))
        return out

    def _legit_docx(self, name="legit.docx"):
        from docx import Document
        d = Document()
        d.add_paragraph("hello docx world")
        path = self.dir / name
        d.save(str(path))
        return path

    def test_bomb_shaped_docx_refused_before_parsing(self):
        os.environ["NEXUS_MAX_DECOMPRESSED_BYTES"] = "1000000"  # 1 MB
        bomb = self._pad_with_huge_declared_entry(self._legit_docx(), 5_000_000)
        with self.assertLogs("nexus_search.ingestion.files", level="WARNING") as logs:
            self.assertEqual(read_file(bomb), "")
        self.assertTrue(any("decompressed" in r.getMessage() for r in logs.records),
                        [r.getMessage() for r in logs.records])

    def test_bomb_shaped_xlsx_refused(self):
        from openpyxl import Workbook
        wb = Workbook()
        wb.active["A1"] = "hello xlsx"
        path = self.dir / "legit.xlsx"
        wb.save(str(path))
        os.environ["NEXUS_MAX_DECOMPRESSED_BYTES"] = "1000000"
        bomb = self._pad_with_huge_declared_entry(path, 5_000_000)
        self.assertEqual(read_file(bomb), "")

    def test_bomb_shaped_pptx_refused(self):
        from pptx import Presentation
        prs = Presentation()
        path = self.dir / "legit.pptx"
        prs.save(str(path))
        os.environ["NEXUS_MAX_DECOMPRESSED_BYTES"] = "1000000"
        bomb = self._pad_with_huge_declared_entry(path, 5_000_000)
        self.assertEqual(read_file(bomb), "")

    def test_legit_ooxml_files_still_parse(self):
        docx = self._legit_docx()
        out = read_file(docx)
        self.assertIn("hello docx world", out)

        from openpyxl import Workbook
        wb = Workbook()
        wb.active["A1"] = "hello xlsx"
        xlsx = self.dir / "legit2.xlsx"
        wb.save(str(xlsx))
        self.assertIn("hello xlsx", read_file(xlsx))

        from pptx import Presentation
        prs = Presentation()
        path = self.dir / "legit2.pptx"
        prs.save(str(path))
        self.assertIsInstance(read_file(path), str)  # parses, no refusal

    def test_huge_page_count_pdf_refused(self):
        from pypdf import PdfWriter
        writer = PdfWriter()
        for _ in range(6):
            writer.add_blank_page(width=612, height=792)
        path = self.dir / "six.pdf"
        with open(path, "wb") as f:
            writer.write(f)
        os.environ["NEXUS_MAX_PDF_PAGES"] = "5"
        with self.assertLogs("nexus_search.ingestion.files", level="WARNING") as logs:
            self.assertEqual(read_file(path), "")
        self.assertTrue(any("pages" in r.getMessage() for r in logs.records))

    def test_small_pdf_page_count_unaffected(self):
        # a default-bound PDF is never page-refused (blank page -> no text
        # -> "" is fine; the point is NO refusal)
        from pypdf import PdfWriter
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        path = self.dir / "one.pdf"
        with open(path, "wb") as f:
            writer.write(f)
        self.assertEqual(read_file(path), "")

    def test_decompression_bounds_exist_with_sane_defaults(self):
        """WP12-B8 (audit R8): python-docx materializes ~5x the declared XML
        in RSS (measured: 20 MiB payload -> 100 MiB tracemalloc peak, 223 s
        parse), so the old 512 MiB payload bound admitted ~2.5 GB RSS from a
        <1 MiB file. Defaults now: 64 MiB total payload, 32 MiB per entry —
        a measured-safe ~320 MiB worst case; env overrides unchanged."""
        from nexus_search.ingestion.connectors import files
        self.assertEqual(files.DEFAULT_MAX_DECOMPRESSED_BYTES, 64 * 1024 * 1024)
        self.assertEqual(files.DEFAULT_MAX_ENTRY_BYTES, 32 * 1024 * 1024)
        self.assertEqual(files.DEFAULT_MAX_PDF_PAGES, 10_000)

    def test_r8_shaped_bomb_refused_under_new_default(self):
        """Audit R8's exact shape: a <1 MiB docx whose word/document.xml
        declares ~100 MiB of valid XML. Old default (512 MiB) let it parse
        (~2.5 GB RSS at python-docx's 5x amplification); the new 64 MiB
        default must refuse it with NO env var set."""
        import zipfile
        path = self.dir / "r8.docx"
        with zipfile.ZipFile(str(path), "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml",
                        '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                        '<Default Extension="xml" ContentType="application/xml"/>'
                        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                        '</Types>')
            zf.writestr("_rels/.rels",
                        '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                        '</Relationships>')
            zf.writestr("word/_rels/document.xml.rels",
                        '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>')
            zf.writestr("word/document.xml",
                        b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                        + b'<w:p><w:r><w:t>lorem ipsum dolor sit amet </w:t></w:r></w:p>' * 1_750_000
                        + b'</w:body></w:document>')
        self.assertLess(path.stat().st_size, 1024 * 1024)  # tiny file, big claim
        self.assertEqual(read_file(path), "")

    def test_per_entry_cap_refuses_one_huge_entry(self):
        """WP12-B8: a single zip entry declaring > 32 MiB is a bomb even
        when the 64 MiB TOTAL bound would allow it (one huge + one small
        entry is the classic nested-bomb shape)."""
        os.environ.pop("NEXUS_MAX_DECOMPRESSED_BYTES", None)
        os.environ.pop("NEXUS_MAX_ENTRY_BYTES", None)
        bomb = self._pad_with_huge_declared_entry(self._legit_docx(), 40_000_000)
        with self.assertLogs("nexus_search.ingestion.files", level="WARNING") as logs:
            self.assertEqual(read_file(bomb), "")
        self.assertTrue(any("entry" in r.getMessage() for r in logs.records),
                        [r.getMessage() for r in logs.records])

    def test_per_entry_cap_env_override_restores(self):
        """NEXUS_MAX_ENTRY_BYTES is an env override like the total bound."""
        bomb = self._pad_with_huge_declared_entry(self._legit_docx(), 40_000_000)
        os.environ["NEXUS_MAX_ENTRY_BYTES"] = "100000000"  # 100 MB: allows it
        os.environ["NEXUS_MAX_DECOMPRESSED_BYTES"] = "100000000"
        self.assertIn("hello docx world", read_file(bomb))


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
