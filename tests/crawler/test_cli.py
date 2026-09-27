"""Direct CLI coverage for crawler/cli.py::main() (argv-patched, temp DBs)."""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.core.storage import Storage
from nexus_search.crawler.cli import main


class _Page(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/robots.txt":
            body = b"User-agent: *\nAllow: /\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
        else:
            body = (b"<html><head><title>Seed Page</title></head>"
                    b"<body>seed page about search crawlers</body></html>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class TestCrawlerCliBlocklist(unittest.TestCase):
    """block / unblock / blocklist subcommands need no server."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "crawl.db")

    def tearDown(self):
        for _ in range(10):
            try:
                shutil.rmtree(self.dir)
                break
            except PermissionError:
                time.sleep(0.05)

    def run_cli(self, *argv) -> str:
        out = io.StringIO()
        with patch.object(sys, "argv", ["nexus-crawl", *argv]):
            with contextlib.redirect_stdout(out):
                main()
        return out.getvalue()

    def test_block_list_unblock_roundtrip(self):
        self.assertIn("blocked spam.example",
                      self.run_cli("block", "spam.example", "--reason", "ops", "--db", self.db))
        listing = self.run_cli("blocklist", "--db", self.db)
        self.assertIn("spam.example", listing)
        self.assertIn("ops", listing)
        self.assertIn("unblocked spam.example",
                      self.run_cli("unblock", "spam.example", "--db", self.db))
        self.assertNotIn("spam.example", self.run_cli("blocklist", "--db", self.db))

    def test_unblock_unknown_host_reports(self):
        self.assertIn("not blocked", self.run_cli("unblock", "never.example", "--db", self.db))

    def test_block_requires_host(self):
        with self.assertRaises(SystemExit):
            self.run_cli("block", "--db", self.db)


class TestCrawlerCliRun(unittest.TestCase):
    """A real crawl through main() against a local seed server."""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Page)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.seed_url = f"http://127.0.0.1:{cls.server.server_port}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "crawl.db")
        self.seeds = os.path.join(self.dir, "seeds.txt")
        with open(self.seeds, "w", encoding="utf-8") as f:
            f.write(self.seed_url + "\n")

    def tearDown(self):
        for _ in range(10):
            try:
                shutil.rmtree(self.dir)
                break
            except PermissionError:
                time.sleep(0.05)

    def test_crawl_run_indexes_the_seed(self):
        out = io.StringIO()
        with patch.object(sys, "argv", ["nexus-crawl", "--seeds", self.seeds,
                                        "--db", self.db, "--allow-private-hosts",
                                        "--max-pages", "5",
                                        "--config", os.path.join(self.dir, "none.yaml"),
                                        "--max-depth", "0"]):
            with contextlib.redirect_stdout(out):
                main()
        self.assertIn("crawled", out.getvalue())
        storage = Storage(self.db)
        try:
            self.assertEqual(storage.document_count(), 1)
            doc = storage.get_document(f"web:{self.seed_url}")
            self.assertIsNotNone(doc)
            self.assertIn("seed page", doc.content)
        finally:
            storage.close()


if __name__ == "__main__":
    unittest.main()
