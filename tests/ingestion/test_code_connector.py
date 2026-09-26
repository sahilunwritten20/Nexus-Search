"""Code connector: size guard, lossy-but-honest decoding, ignored dirs."""
import os
import shutil
import tempfile
import unittest

from nexus_search.ingestion.connectors.code import iter_code


class TestCodeConnector(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, name: str, data) -> None:
        p = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if isinstance(data, bytes):
            with open(p, "wb") as f:
                f.write(data)
        else:
            with open(p, "w", encoding="utf-8") as f:
                f.write(data)

    def test_indexes_source_files(self):
        self._write("main.py", "def hello():\n    return 'world'\n")
        docs = list(iter_code(self.dir))
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].doc_type, "code")
        self.assertIn("hello", docs[0].content)

    def test_oversized_file_refused(self):
        self._write("big.py", "x = 'a'\n" * 100)
        os.environ["NEXUS_MAX_INGEST_BYTES"] = "100"
        try:
            docs = list(iter_code(self.dir))
        finally:
            os.environ.pop("NEXUS_MAX_INGEST_BYTES", None)
        self.assertEqual(docs, [])  # refused with a warning, not swallowed

    def test_invalid_utf8_decodes_with_replacement(self):
        self._write("bad.py", b"ok = 1\n\xff\xfe binary garbage \x89")
        docs = list(iter_code(self.dir))
        self.assertEqual(len(docs), 1)
        self.assertIn("ok = 1", docs[0].content)
        self.assertIn("�", docs[0].content)  # marked, not silently dropped

    def test_ignored_dirs_skipped(self):
        self._write("node_modules/dep/index.js", "var x = 1;")
        self._write("src/app.js", "var y = 2;")
        docs = list(iter_code(self.dir))
        self.assertEqual([d.metadata["path"] for d in docs], [os.path.join("src", "app.js")])


if __name__ == "__main__":
    unittest.main()
