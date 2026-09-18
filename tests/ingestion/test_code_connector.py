import shutil
import tempfile
import unittest
from pathlib import Path

from nexus_search.ingestion.connectors.code import iter_code


class TestCodeConnector(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        Path(self.root, "main.py").write_text("def main():\n    pass\n")
        Path(self.root, "app.js").write_text("function app() {}\n")
        Path(self.root, "readme.txt").write_text("not code")

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_finds_code_files(self):
        docs = {d.doc_id for d in iter_code(self.root)}
        self.assertIn("code:main.py", docs)
        self.assertIn("code:app.js", docs)

    def test_ignores_non_code_files(self):
        docs = list(iter_code(self.root))
        self.assertFalse(any("readme.txt" in d.doc_id for d in docs))

    def test_language_metadata_from_extension(self):
        docs = {d.doc_id: d for d in iter_code(self.root)}
        self.assertEqual(docs["code:main.py"].metadata["language"], "py")

    def test_doc_type_is_code(self):
        docs = list(iter_code(self.root))
        self.assertTrue(all(d.doc_type == "code" for d in docs))


if __name__ == "__main__":
    unittest.main()
