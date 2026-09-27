"""Direct CLI coverage for ingestion/cli.py::main() (argv-patched, temp DBs)."""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.core.storage import Storage
from nexus_search.ingestion.cli import main
from nexus_search.ingestion.failures import FailureQueue
from nexus_search.ingestion.types import IngestDoc


class _CliCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "cli.db")

    def tearDown(self):
        for _ in range(10):
            try:
                shutil.rmtree(self.dir)
                break
            except PermissionError:
                import time
                time.sleep(0.05)

    def run_cli(self, *argv) -> str:
        out = io.StringIO()
        with patch.object(sys, "argv", ["nexus-ingest", *argv]):
            with contextlib.redirect_stdout(out):
                main()
        return out.getvalue()

    def test_ingest_files_source_reports_stats(self):
        docs_dir = os.path.join(self.dir, "docs")
        os.makedirs(docs_dir)
        with open(os.path.join(docs_dir, "guide.txt"), "w", encoding="utf-8") as f:
            f.write("a guide about search engines and crawlers")
        out = self.run_cli("--source", "files", "--path", docs_dir, "--db", self.db)
        self.assertIn("indexed", out)  # stats dict reports the ingest
        storage = Storage(self.db)
        try:
            self.assertEqual(storage.document_count(), 1)
        finally:
            storage.close()

    def test_ingest_code_source(self):
        src_dir = os.path.join(self.dir, "src")
        os.makedirs(src_dir)
        with open(os.path.join(src_dir, "app.py"), "w", encoding="utf-8") as f:
            f.write("def main():\n    return 42\n")
        self.run_cli("--source", "code", "--path", src_dir, "--db", self.db)
        storage = Storage(self.db)
        try:
            self.assertEqual(storage.document_count(), 1)
        finally:
            storage.close()

    def test_ingest_product_source(self):
        csv_path = os.path.join(self.dir, "catalog.csv")
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write("id,name,price\n1,Blue Mug,12.99\n")
        self.run_cli("--source", "product", "--path", csv_path, "--db", self.db)
        storage = Storage(self.db)
        try:
            self.assertEqual(storage.document_count(), 1)
        finally:
            storage.close()

    def test_list_failures(self):
        queue = FailureQueue(self.db)
        queue.record(IngestDoc("dead-doc", "T", "x", "text", {}), "ingest blew up")
        queue.close()
        out = self.run_cli("--source", "files", "--path", self.dir, "--db", self.db,
                           "--list-failures")
        self.assertIn("dead-doc", out)
        self.assertIn("1 dead-lettered", out)

    def test_replay_failures_recovers(self):
        queue = FailureQueue(self.db)
        queue.record(IngestDoc("retry-doc", "T", "recoverable body of text here", "text", {}),
                     "transient")
        # CLI replay honors backoff (only DUE rows); age the row past it
        queue.conn.execute("UPDATE failed_ingestions SET next_retry_at = 0")
        queue.conn.commit()
        queue.close()
        out = self.run_cli("--source", "files", "--path", self.dir, "--db", self.db,
                           "--replay-failures")
        self.assertIn("recovered", out)  # replay stats reported
        storage = Storage(self.db)
        try:
            self.assertIsNotNone(storage.get_document("retry-doc"))
        finally:
            storage.close()


if __name__ == "__main__":
    unittest.main()
