import shutil
import tempfile
import unittest
from pathlib import Path

from nexus_search.ingestion.connectors.files import iter_files


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


if __name__ == "__main__":
    unittest.main()
