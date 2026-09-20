"""Regression tests for the Phase 1-3 hardening pass (plain unittest, no pytest needed)."""
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from nexus_search.core.bm25 import BM25Search
from nexus_search.core.indexer import Indexer
from nexus_search.core.storage import Storage
from nexus_search.core.tokenizer import tokenize
from nexus_search.crawler.fetcher import Fetcher
from nexus_search.crawler.frontier import Frontier
from nexus_search.crawler.pipeline import CrawlPipeline
from nexus_search.crawler.politeness import PolitenessManager
from nexus_search.crawler.sitemap import parse_sitemap, parse_sitemap_index
from nexus_search.ingestion.dedup import Deduplicator
from nexus_search.ingestion.pipeline import ingest_documents, make_crawler_ingest_fn
from nexus_search.ingestion.types import IngestDoc

HITS: list = []


def _page(title, body="", links=()):
    a = "".join(f'<a href="{l}">x</a>' for l in links)
    return f"<html><head><title>{title}</title></head><body><p>{body} {title}</p>{a}</body></html>"


class SiteHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        HITS.append((self.path, time.monotonic()))
        port = self.server.server_port
        routes = {
            "/robots.txt": (200, "User-agent: *\nAllow: /\n", "text/plain"),
            "/": (200, _page("Home", "welcome", ["/p1", "/p2", "/p3", "/missing"]), "text/html"),
            "/p1": (200, _page("P1"), "text/html"),
            "/p2": (200, _page("P2"), "text/html"),
            "/p3": (200, _page("P3"), "text/html"),
            "/missing": (404, _page("Not Found"), "text/html"),
            "/boom": (503, _page("Down"), "text/html"),
            "/redir": (302, "", "text/html"),
            "/secret": (200, _page("SECRET"), "text/html"),
            "/big": (200, "<html>" + "x" * 5000 + "</html>", "text/html"),
        }
        status, body, ctype = routes.get(self.path, (404, "nope", "text/plain"))
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if self.path == "/redir":
            self.send_header("Location", f"http://127.0.0.1:{port}/secret")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), SiteHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        HITS.clear()
        self.tmp = tempfile.mkdtemp()

    def pipeline(self, name="f.db", **kw):
        kw.setdefault("max_pages", 20)
        kw.setdefault("max_depth", 1)
        kw.setdefault("concurrency", 1)
        kw.setdefault("default_crawl_delay", 0.0)
        return CrawlPipeline(db_path=os.path.join(self.tmp, name), allow_private_hosts=True, **kw)


class TestFetcher(ServerCase):
    def test_http_error_status_is_an_error_not_content(self):
        r = Fetcher().fetch(self.base + "/missing")
        self.assertIsNone(r.html)
        self.assertEqual(r.error, "HTTP 404")

    def test_5xx_is_retried(self):
        with mock.patch("nexus_search.crawler.fetcher.time.sleep"):
            r = Fetcher(max_retries=2).fetch(self.base + "/boom")
        self.assertEqual(len([h for h in HITS if h[0] == "/boom"]), 3)
        self.assertEqual(r.error, "HTTP 503")

    def test_redirect_hops_are_validated(self):
        f = Fetcher(url_validator=lambda u: u.endswith("/redir"))  # /secret is "internal"
        r = f.fetch(self.base + "/redir")
        self.assertIsNone(r.html)
        self.assertIn("disallowed", r.error)
        self.assertFalse([h for h in HITS if h[0] == "/secret"])

    def test_safe_redirect_is_followed(self):
        r = Fetcher(url_validator=lambda u: True).fetch(self.base + "/redir")
        self.assertIn("SECRET", r.html)

    def test_oversized_body_is_rejected(self):
        r = Fetcher(max_bytes=1000).fetch(self.base + "/big")
        self.assertIsNone(r.html)
        self.assertIn("exceeds", r.error)


class TestPoliteness(unittest.TestCase):
    def test_reserve_slot_spaces_out_concurrent_callers(self):
        pm = PolitenessManager(default_delay=0.5)
        waits = sorted(pm.reserve_slot("a.com") for _ in range(4))
        gaps = [b - a for a, b in zip(waits, waits[1:])]
        self.assertLess(waits[0], 0.05)
        self.assertTrue(all(g >= 0.45 for g in gaps), gaps)


class TestCrawler(ServerCase):
    def test_crawl_delay_holds_with_many_workers(self):
        docs = []
        pl = self.pipeline(concurrency=4, default_crawl_delay=0.3,
                           ingest_fn=lambda u, t, x, m: docs.append(u))
        pl.seed([self.base + "/"])
        pl.run()
        times = sorted(t for p, t in HITS if p in ("/", "/p1", "/p2", "/p3"))
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertTrue(all(g >= 0.25 for g in gaps), gaps)

    def test_error_pages_are_not_ingested(self):
        titles = []
        pl = self.pipeline(ingest_fn=lambda u, t, x, m: titles.append(t))
        pl.seed([self.base + "/"])
        stats = pl.run()
        self.assertNotIn("Not Found", titles)
        self.assertEqual(stats["errors"], 1)

    def test_domain_limit_skips_are_not_marked_visited(self):
        db = "lim.db"
        pl = self.pipeline(db, max_pages_per_domain=2)
        pl.seed([self.base + "/"])
        first = pl.run()
        self.assertEqual(first["crawled"], 2)
        pl2 = self.pipeline(db)  # limit lifted
        second = pl2.run()
        self.assertGreater(second["crawled"], 0)  # previously skipped URLs get crawled

    def test_ingest_failure_does_not_abort_or_mark_visited(self):
        def bad(u, t, x, m):
            raise RuntimeError("index down")
        pl = self.pipeline("bad.db", ingest_fn=bad)
        pl.seed([self.base + "/"])
        stats = pl.run()  # must not raise
        self.assertGreaterEqual(stats["errors"], 1)
        self.assertEqual(stats["crawled"], 0)
        fr = Frontier(os.path.join(self.tmp, "bad.db"))
        self.assertEqual(fr.visited_count(), 0)
        fr.close()

    def test_in_progress_urls_recover_after_crash(self):
        db = os.path.join(self.tmp, "crash.db")
        fr = Frontier(db)
        fr.add("http://x.test/1", 0)
        fr.next_batch(1)  # claimed, then the process "dies"
        fr.close()
        fr = Frontier(db)
        self.assertEqual(len(fr.next_batch(5)), 1)
        fr.close()

    def test_sitemap_index_is_followed(self):
        idx = '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>https://x.test/s.xml</loc></sitemap></sitemapindex>'
        self.assertEqual(parse_sitemap_index(idx), ["https://x.test/s.xml"])
        self.assertEqual(parse_sitemap(idx), [])  # child sitemaps are not page URLs


class TestDedupLifecycle(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.storage = Storage(self.path)
        self.indexer = Indexer(self.storage)
        self.dedup = Deduplicator(self.path)
        self.ingest = make_crawler_ingest_fn(self.indexer, self.dedup)

    def tearDown(self):
        self.storage.close()
        self.dedup.close()

    def test_reverting_a_page_reindexes_it(self):
        u = "http://x.test/a"
        self.ingest(u, "T", "version one text", {})
        self.ingest(u, "T", "version two text", {})
        self.ingest(u, "T", "version one text", {})
        self.assertEqual(self.storage.get_document("web:" + u).content, "version one text")

    def test_deleted_document_can_be_reingested(self):
        u = "http://x.test/a"
        self.ingest(u, "T", "some content", {})
        self.indexer.delete_document("web:" + u)
        self.ingest(u, "T", "some content", {})
        self.assertIsNotNone(self.storage.get_document("web:" + u))

    def test_chunking_and_quality_gate(self):
        docs = [IngestDoc("d1", "Long", "word " * 400, "file"), IngestDoc("d2", "Junk", "hi", "file")]
        stats = ingest_documents(docs, self.indexer, self.dedup, min_quality=0.3, chunk_size=300)
        self.assertEqual((stats["indexed"], stats["low_quality"]), (1, 1))
        chunks = self.storage.doc_ids_with_prefix("d1#chunk")
        self.assertGreater(len(chunks), 1)
        # unchanged re-ingest is still recognised as a duplicate even though only chunks exist
        again = ingest_documents([docs[0]], self.indexer, self.dedup, chunk_size=300)
        self.assertEqual(again["duplicates"], 1)


class TestSearchUpgrades(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.storage = Storage(self.path)
        self.ix = Indexer(self.storage)
        self.search = BM25Search(self.storage)

    def tearDown(self):
        self.storage.close()

    def test_unicode_tokenization(self):
        self.assertEqual(tokenize("café नमस्ते दुनिया"), ["café", "नमस्ते", "दुनिया"])

    def test_pagination_reports_true_total(self):
        for i in range(25):
            self.ix.add_document(f"d{i}", "common word", f"t{i}")
        p1 = self.search.search_page("common", top_k=10, offset=0)
        p3 = self.search.search_page("common", top_k=10, offset=20)
        self.assertEqual((p1.total, len(p1.results), len(p3.results)), (25, 10, 5))
        self.assertFalse({r.doc_id for r in p1.results} & {r.doc_id for r in p3.results})

    def test_quoted_phrase_is_required(self):
        self.ix.add_document("d1", "machine learning is useful")
        self.ix.add_document("d2", "machine systems and learning systems")
        self.assertEqual([r.doc_id for r in self.search.search('"machine learning"')], ["d1"])

    def test_filter_only_query(self):
        self.ix.add_document("a", "report", doc_type="pdf")
        self.ix.add_document("b", "report", doc_type="web")
        self.assertEqual([r.doc_id for r in self.search.search("type:pdf")], ["a"])


if __name__ == "__main__":
    unittest.main()