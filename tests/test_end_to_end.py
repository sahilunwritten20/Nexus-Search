"""True end-to-end test: a local HTTP server -> the real crawler -> the
real Phase 2 ingestion adapter -> the real Phase 1 index -> real BM25
search. Nothing here is mocked or stubbed — this is what "the system
actually works together" means, proven rather than assumed.
"""
import functools
import http.server
import os
import tempfile
import threading
import unittest
from pathlib import Path

from nexus_search.core.bm25 import BM25Search
from nexus_search.core.indexer import Indexer
from nexus_search.core.storage import Storage
from nexus_search.crawler.pipeline import CrawlPipeline
from nexus_search.ingestion.dedup import Deduplicator
from nexus_search.ingestion.pipeline import make_crawler_ingest_fn


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


class TestFullPipelineEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site_dir = tempfile.mkdtemp()
        site = Path(cls.site_dir)

        (site / "index.html").write_text(
            '<html lang="en"><head><title>Nexus Search Home</title></head><body>'
            "<nav>Nav</nav>"
            "<article><p>Nexus Search unifies web pages, documents, products, "
            "and code into a single search index, long enough content to clear "
            "the extractor's short-content fallback check.</p></article>"
            '<a href="/about.html">About</a>'
            '<a href="/private/hidden.html">Hidden</a>'
            "</body></html>"
        )
        (site / "about.html").write_text(
            '<html lang="en"><head><title>About Nexus</title></head><body>'
            "<article><p>This page describes the BM25 ranking algorithm and "
            "the hybrid vector search planned for Phase 4 of the roadmap.</p></article>"
            "</body></html>"
        )
        private = site / "private"
        private.mkdir()
        (private / "hidden.html").write_text(
            "<html><head><title>Hidden</title></head><body>"
            "<p>Must never be indexed — disallowed by robots.txt.</p></body></html>"
        )
        (site / "robots.txt").write_text("User-agent: *\nDisallow: /private/\nCrawl-delay: 0\n")

        handler = functools.partial(_QuietHandler, directory=cls.site_dir)
        cls.httpd = http.server.HTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        fd, self.index_db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        fd, self.frontier_db = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        self.storage = Storage(self.index_db)
        self.indexer = Indexer(self.storage)
        self.dedup = Deduplicator(self.index_db)
        self.searcher = BM25Search(self.storage)

    def tearDown(self):
        self.storage.close()
        self.dedup.close()
        for path in (self.index_db, self.frontier_db):
            Path(path).unlink(missing_ok=True)

    def _run_full_crawl(self):
        ingest_fn = make_crawler_ingest_fn(self.indexer, self.dedup)
        pipeline = CrawlPipeline(
            db_path=self.frontier_db,
            allowed_domains=["127.0.0.1"],
            max_pages=10,
            max_depth=2,
            ingest_fn=ingest_fn,
        )
        pipeline.seed([f"http://127.0.0.1:{self.port}/"])
        return pipeline.run()

    def test_crawled_pages_are_searchable_via_real_bm25(self):
        self._run_full_crawl()
        results = self.searcher.search("BM25 ranking algorithm")
        self.assertTrue(any("About Nexus" in r.title for r in results))

    def test_robots_txt_disallowed_page_never_reaches_the_index(self):
        self._run_full_crawl()
        self.assertEqual(self.storage.document_count(), 2)  # index + about; hidden is blocked
        results = self.searcher.search("hidden disallowed")
        self.assertEqual(results, [])

    def test_index_page_findable_by_its_own_content(self):
        self._run_full_crawl()
        results = self.searcher.search("unifies web pages documents products")
        self.assertTrue(any("Nexus Search Home" in r.title for r in results))

    def test_search_result_snippet_is_populated(self):
        self._run_full_crawl()
        results = self.searcher.search("BM25")
        self.assertTrue(results[0].snippet)

    def test_recrawl_does_not_duplicate_index_entries(self):
        self._run_full_crawl()
        first_count = self.storage.document_count()
        # Run again with a *different* frontier DB — simulating a second,
        # independent crawl of the same site — dedup should still catch it
        # because it lives in the shared index DB, not the frontier DB.
        fd, second_frontier = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            ingest_fn = make_crawler_ingest_fn(self.indexer, self.dedup)
            pipeline2 = CrawlPipeline(
                db_path=second_frontier, allowed_domains=["127.0.0.1"],
                max_pages=10, max_depth=2, ingest_fn=ingest_fn,
            )
            pipeline2.seed([f"http://127.0.0.1:{self.port}/"])
            pipeline2.run()
        finally:
            Path(second_frontier).unlink(missing_ok=True)
        self.assertEqual(self.storage.document_count(), first_count)


if __name__ == "__main__":
    unittest.main()
