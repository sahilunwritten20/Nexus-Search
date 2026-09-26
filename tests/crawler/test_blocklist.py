"""Tests for the webmaster opt-out blocklist: the module itself, and its
wiring into CrawlPipeline (a blocked host must never reach fetcher.fetch)."""

import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from nexus_search.crawler.blocklist import Blocklist
from nexus_search.crawler.pipeline import CrawlPipeline


class AllowAllHandler(BaseHTTPRequestHandler):
    """Serves a robots.txt that ALLOWS everything, plus one page. """

    requests_log = []  # class-level so tests can assert what was requested

    def do_GET(self):
        type(self).requests_log.append(self.path)
        if self.path == "/robots.txt":
            body = b"User-agent: *\nAllow: /\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
        else:
            body = (b"<html><head><title>Page</title></head>"
                    b"<body>hello world</body></html>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # quiet
        pass


class TestBlocklistUnit(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = f"{self.tmpdir}/frontier.db"
        self.blocklist = Blocklist(self.db_path)

    def tearDown(self):
        self.blocklist.close()
        for _ in range(10):
            try:
                shutil.rmtree(self.tmpdir)
                return
            except PermissionError:
                time.sleep(0.05)

    def test_block_then_is_blocked(self):
        self.assertFalse(self.blocklist.is_blocked("http://example.com/"))
        self.blocklist.block("example.com", reason="webmaster opt-out")
        self.assertTrue(self.blocklist.is_blocked("http://example.com/"))
        self.assertTrue(self.blocklist.is_blocked("https://example.com/deep/page"))

    def test_case_and_url_forms(self):
        self.blocklist.block("http://Example.COM/x")
        self.assertTrue(self.blocklist.is_blocked("EXAMPLE.com"))
        self.assertTrue(self.blocklist.is_blocked("https://example.com/"))

    def test_parent_domain_covers_subdomains(self):
        self.blocklist.block("example.com")
        self.assertTrue(self.blocklist.is_blocked("blog.example.com"))
        self.assertFalse(self.blocklist.is_blocked("other.com"))

    def test_unblock(self):
        self.blocklist.block("example.com")
        self.assertTrue(self.blocklist.unblock("example.com"))
        self.assertFalse(self.blocklist.is_blocked("http://example.com/"))
        self.assertFalse(self.blocklist.unblock("example.com"))  # already gone

    def test_list_all(self):
        self.blocklist.block("b.com", reason="b")
        self.blocklist.block("a.com", reason="a")
        rows = self.blocklist.list_all()
        self.assertEqual([r[0] for r in rows], ["a.com", "b.com"])
        self.assertEqual(rows[0][1], "a")


class TestBlocklistPipeline(unittest.TestCase):
    """Integration: the blocklist gate lives inside _crawl, before fetch."""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), AllowAllHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.server_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = f"{self.tmpdir}/frontier.db"
        AllowAllHandler.requests_log.clear()

    def tearDown(self):
        for _ in range(10):
            try:
                shutil.rmtree(self.tmpdir)
                return
            except PermissionError:
                time.sleep(0.05)

    def _pipeline(self, documents):
        return CrawlPipeline(
            db_path=self.db_path,
            allowed_domains=["127.0.0.1"],
            max_pages=10,
            max_depth=1,
            concurrency=1,
            ingest_fn=lambda u, t, x, m: documents.append(u),
            allow_private_hosts=True,
        )

    def test_blocked_host_never_fetched(self):
        # Block via a separate Blocklist on the same DB file — the instance
        # the pipeline constructs itself must be what enforces the skip.
        bl = Blocklist(self.db_path)
        bl.block("127.0.0.1", reason="opt-out email")
        bl.close()

        documents = []
        p = self._pipeline(documents)
        p.seed([f"{self.server_url}/page"])
        result = p.run()
        self.assertEqual(result["crawled"], 0)
        self.assertGreaterEqual(result["skipped"], 1)
        # Not a single HTTP request reached the test server — not even
        # robots.txt: the blocklist gate runs before every fetch.
        self.assertEqual(AllowAllHandler.requests_log, [])
        self.assertEqual(documents, [])

        # Skipped, NOT visited: frontier keeps the URL retryable.
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT status FROM frontier").fetchall()
        conn.close()
        self.assertTrue(rows)
        self.assertTrue(all(r[0] == "skipped" for r in rows))

    def test_unblock_makes_fetchable_again(self):
        bl = Blocklist(self.db_path)
        bl.block("127.0.0.1")
        bl.close()

        documents = []
        p1 = self._pipeline(documents)
        p1.seed([f"{self.server_url}/page"])
        blocked_result = p1.run()
        self.assertEqual(blocked_result["crawled"], 0)

        bl2 = Blocklist(self.db_path)
        self.assertTrue(bl2.unblock("127.0.0.1"))
        bl2.close()

        documents2 = []
        p2 = self._pipeline(documents2)
        p2.seed([f"{self.server_url}/page"])
        unblocked_result = p2.run()
        self.assertGreaterEqual(unblocked_result["crawled"], 1)
        self.assertIn(f"{self.server_url}/page", documents2)

    def test_blocklist_beats_allowing_robots(self):
        # robots.txt says Allow: / — the blocklist must still win
        # (precedence: blocklist > robots.txt > allow-list).
        bl = Blocklist(self.db_path)
        bl.block("127.0.0.1")
        bl.close()

        documents = []
        p = self._pipeline(documents)
        p.seed([f"{self.server_url}/page"])
        result = p.run()
        self.assertEqual(result["crawled"], 0)
        # The PAGE was never fetched; robots needn't be either.
        self.assertNotIn("/page", AllowAllHandler.requests_log)
        self.assertEqual(documents, [])

    def test_unblocked_host_fetches_normally(self):
        documents = []
        p = self._pipeline(documents)
        p.seed([f"{self.server_url}/page"])
        result = p.run()
        self.assertGreaterEqual(result["crawled"], 1)
        self.assertIn(f"{self.server_url}/page", documents)


if __name__ == "__main__":
    unittest.main()
