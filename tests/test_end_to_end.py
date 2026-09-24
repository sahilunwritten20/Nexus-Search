"""End-to-end crawler tests for Nexus Search.

Plain unittest.TestCase: must load and run under BOTH
`python -m pytest` and `python -m unittest discover -s tests -v`
without pytest (or any optional dependency) installed.
"""

import shutil
import tempfile
import threading
import time
import unittest
from http.server import (
    BaseHTTPRequestHandler,
    HTTPServer,
)

from nexus_search.crawler.pipeline import (
    CrawlPipeline,
)


# ============================================================
# TEST HTTP SERVER
# ============================================================


class TestHandler(BaseHTTPRequestHandler):

    pages = {
        "/": """
        <html>
        <head>
            <title>Nexus Home</title>
        </head>
        <body>
            <h1>Nexus Search</h1>
            <p>
                Welcome to the Nexus Search
                crawler test website.
            </p>
            <a href="/page1">
                Page One
            </a>
            <a href="/page2">
                Page Two
            </a>
        </body>
        </html>
        """,

        "/page1": """
        <html>
        <head>
            <title>Page One</title>
        </head>
        <body>
            <h1>Page One</h1>
            <p>
                This is the first test page.
            </p>
            <a href="/page3">
                Page Three
            </a>
        </body>
        </html>
        """,

        "/page2": """
        <html>
        <head>
            <title>Page Two</title>
        </head>
        <body>
            <h1>Page Two</h1>
            <p>
                This is the second test page.
            </p>
        </body>
        </html>
        """,

        "/page3": """
        <html>
        <head>
            <title>Page Three</title>
        </head>
        <body>
            <h1>Page Three</h1>
            <p>
                This is the third test page.
            </p>
        </body>
        </html>
        """,
    }

    etag = '"test-etag-v1"'

    last_modified = (
        "Wed, 01 Jan 2025 00:00:00 GMT"
    )

    def do_GET(self):

        # robots.txt
        if self.path == "/robots.txt":

            body = (
                "User-agent: *\n"
                "Allow: /\n"
            ).encode("utf-8")

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "text/plain",
            )

            self.send_header(
                "Content-Length",
                str(len(body)),
            )

            self.end_headers()

            self.wfile.write(body)

            return

        # Unknown page
        if self.path not in self.pages:

            self.send_response(404)

            self.end_headers()

            return

        # Conditional request
        request_etag = self.headers.get(
            "If-None-Match"
        )

        request_modified = self.headers.get(
            "If-Modified-Since"
        )

        if (
            request_etag == self.etag
            or request_modified
            == self.last_modified
        ):

            self.send_response(304)

            self.send_header(
                "ETag",
                self.etag,
            )

            self.send_header(
                "Last-Modified",
                self.last_modified,
            )

            self.end_headers()

            return

        body = self.pages[
            self.path
        ].encode("utf-8")

        self.send_response(200)

        self.send_header(
            "Content-Type",
            "text/html; charset=utf-8",
        )

        self.send_header(
            "Content-Length",
            str(len(body)),
        )

        self.send_header(
            "ETag",
            self.etag,
        )

        self.send_header(
            "Last-Modified",
            self.last_modified,
        )

        self.end_headers()

        self.wfile.write(body)

    def log_message(
        self,
        format,
        *args,
    ):
        # Keep test output clean.
        return


# ============================================================
# END-TO-END CRAWL TEST CASE
# ============================================================


class TestEndToEndCrawler(unittest.TestCase):
    """The 5 end-to-end crawler scenarios, sharing one local HTTP server."""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(
            (
                "127.0.0.1",
                0,
            ),
            TestHandler,
        )

        cls.server_thread = threading.Thread(
            target=cls.server.serve_forever,
            daemon=True,
        )

        cls.server_thread.start()

        cls.server_url = (
            f"http://127.0.0.1:"
            f"{cls.server.server_port}"
        )

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=2)

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        for _ in range(10):
            try:
                shutil.rmtree(self.tmpdir)
                break
            except PermissionError:
                time.sleep(0.05)

    # ------------------------------------------------ basic crawl

    def test_full_crawl(self):

        frontier_db = (
            self.tmpdir
            + "/frontier.db"
        )

        documents = []

        def ingest_fn(
            url,
            title,
            text,
            metadata,
        ):

            documents.append(
                {
                    "url": url,
                    "title": title,
                    "text": text,
                    "metadata": metadata,
                }
            )

        pipeline = CrawlPipeline(
            db_path=str(frontier_db),
            allowed_domains=[
                "127.0.0.1"
            ],
            max_pages=10,
            max_depth=2,
            concurrency=2,
            ingest_fn=ingest_fn,

            # Required because the test server
            # intentionally runs on localhost.
            allow_private_hosts=True,
        )

        pipeline.seed(
            [
                self.server_url
            ]
        )

        result = pipeline.run()

        assert result["crawled"] >= 4

        assert len(documents) >= 4

        urls = {
            document["url"]
            for document in documents
        }

        assert (
            f"{self.server_url}/"
            in urls
        )

        assert (
            f"{self.server_url}/page1"
            in urls
        )

        assert (
            f"{self.server_url}/page2"
            in urls
        )

        assert (
            f"{self.server_url}/page3"
            in urls
        )

        assert all(
            document["title"]
            for document in documents
        )

    # ------------------------------------------------ depth limit

    def test_crawl_depth_limit(self):

        frontier_db = (
            self.tmpdir
            + "/depth.db"
        )

        documents = []

        def ingest_fn(
            url,
            title,
            text,
            metadata,
        ):

            documents.append(
                metadata["depth"]
            )

        pipeline = CrawlPipeline(
            db_path=str(frontier_db),
            allowed_domains=[
                "127.0.0.1"
            ],
            max_pages=10,
            max_depth=0,
            concurrency=2,
            ingest_fn=ingest_fn,
            allow_private_hosts=True,
        )

        pipeline.seed(
            [
                self.server_url
            ]
        )

        result = pipeline.run()

        assert result["crawled"] == 1

        assert documents == [0]

    # ------------------------------------------------ domain limit

    def test_domain_limit(self):

        frontier_db = (
            self.tmpdir
            + "/domain_limit.db"
        )

        documents = []

        def ingest_fn(
            url,
            title,
            text,
            metadata,
        ):

            documents.append(url)

        pipeline = CrawlPipeline(
            db_path=str(frontier_db),
            allowed_domains=[
                "127.0.0.1"
            ],
            max_pages=10,
            max_depth=2,
            concurrency=2,
            ingest_fn=ingest_fn,
            max_pages_per_domain=2,
            allow_private_hosts=True,
        )

        pipeline.seed(
            [
                self.server_url
            ]
        )

        result = pipeline.run()

        assert result["crawled"] <= 2

        assert len(documents) <= 2

    # ------------------------------------- etag / last-modified recrawl

    def test_incremental_recrawl(self):

        frontier_db = (
            self.tmpdir
            + "/recrawl.db"
        )

        first_documents = []

        def first_ingest(
            url,
            title,
            text,
            metadata,
        ):

            first_documents.append(
                url
            )

        pipeline = CrawlPipeline(
            db_path=str(frontier_db),
            allowed_domains=[
                "127.0.0.1"
            ],
            max_pages=1,
            max_depth=0,
            concurrency=1,
            ingest_fn=first_ingest,
            allow_private_hosts=True,
        )

        pipeline.seed(
            [
                self.server_url
            ]
        )

        first_result = pipeline.run()

        assert first_result["crawled"] == 1

        assert len(first_documents) == 1

        # Give SQLite enough time so the
        # second crawl has a different timestamp.
        time.sleep(0.01)

        second_documents = []

        def second_ingest(
            url,
            title,
            text,
            metadata,
        ):

            second_documents.append(
                url
            )

        pipeline = CrawlPipeline(
            db_path=str(frontier_db),
            allowed_domains=[
                "127.0.0.1"
            ],
            max_pages=1,
            max_depth=0,
            concurrency=1,
            ingest_fn=second_ingest,
            recrawl_interval=0.0,
            allow_private_hosts=True,
        )

        # Explicitly queue the URL again.
        pipeline.frontier.add(
            self.server_url,
            depth=0,
            priority=10,
            allow_visited=True,
        )

        second_result = pipeline.run()

        assert (
            second_result["not_modified"]
            == 1
        )

        assert (
            second_result["crawled"]
            == 0
        )

        assert second_documents == []

    # -------------------------------------------- SSRF protection

    def test_private_host_is_blocked(self):

        frontier_db = (
            self.tmpdir
            + "/security.db"
        )

        documents = []

        def ingest_fn(
            url,
            title,
            text,
            metadata,
        ):

            documents.append(url)

        pipeline = CrawlPipeline(
            db_path=str(frontier_db),
            allowed_domains=[
                "127.0.0.1"
            ],
            max_pages=10,
            max_depth=1,
            concurrency=1,
            ingest_fn=ingest_fn,

            # Production/default security behavior.
            allow_private_hosts=False,
        )

        pipeline.seed(
            [
                "http://127.0.0.1:9999"
            ]
        )

        result = pipeline.run()

        assert result["crawled"] == 0

        assert result["skipped"] == 0

        assert len(documents) == 0


if __name__ == "__main__":
    unittest.main()
