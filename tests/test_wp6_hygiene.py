"""WP6 hygiene regression tests:
BUG-07 (terminal 4xx), BUG-08 (debug gating), BUG-09 (record_edges caps +
first_seen), BUG-13 (duplicate test names — covered by CI lint + renames),
NEXUS_TRUST_PROXY rate-limit keying, explicit install_dns_pinning,
IPv6-literal SSRF, markdown reader, MIME sniffing, A/B purge CLI.
"""
import importlib
import os
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("NEXUS_EMBEDDER", "hash:384")

from nexus_search.crawler.frontier import Frontier  # noqa: E402
from nexus_search.links.graph import LinkGraph  # noqa: E402


class TestTerminal4xx(unittest.TestCase):
    """BUG-07: a permanent 4xx leaves the frontier after ONE attempt."""

    def test_mark_error_permanent_deletes_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Frontier(os.path.join(tmp, "f.db"))
            try:
                f.add("http://site.com/gone", depth=0)
                self.assertEqual(f.pending_count(), 1)
                f.mark_error("http://site.com/gone", "HTTP 404", permanent=True)
                self.assertEqual(f.pending_count(), 0,
                                 "permanent 4xx must not occupy a retry budget")
                # and the failure history is still in crawl_errors
                rows = f.conn.execute(
                    "SELECT COUNT(*) FROM crawl_errors WHERE url = ?",
                    ("http://site.com/gone",)).fetchone()[0]
                self.assertEqual(rows, 1)
            finally:
                f.close()

    def test_transient_error_still_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Frontier(os.path.join(tmp, "f.db"))
            try:
                f.add("http://site.com/flaky", depth=0)
                f.mark_error("http://site.com/flaky", "HTTP 503")
                row = f.conn.execute(
                    "SELECT status, retry_count FROM frontier "
                    "WHERE url = 'http://site.com/flaky'").fetchone()
                self.assertEqual(row[0], "error")
                self.assertEqual(row[1], 1)
            finally:
                f.close()


class _CountingHandler(BaseHTTPRequestHandler):
    hits = {}

    def do_GET(self):
        _CountingHandler.hits[self.path] = _CountingHandler.hits.get(self.path, 0) + 1
        if self.path == "/robots.txt":
            body = b"User-agent: *\nAllow: /\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/linker":
            body = (b"<html><head><title>L</title></head><body>"
                    b'<a href="/missing">dead link</a></body></html>')
        elif self.path == "/page":
            body = b"<html><head><title>P</title></head><body>live</body></html>"
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class TestDeadLinkFetchedOnceLive(unittest.TestCase):
    """Live local server: /missing 404s. Pre-BUG-07 the frontier retried it
    3 times with backoff; now exactly ONE fetch happens."""

    def setUp(self):
        _CountingHandler.hits = {}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _CountingHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for _ in range(10):
            try:
                shutil.rmtree(self.tmpdir)
                break
            except PermissionError:
                time.sleep(0.05)

    def test_404_link_fetched_exactly_once(self):
        from nexus_search.crawler.pipeline import CrawlPipeline
        pipeline = CrawlPipeline(
            db_path=os.path.join(self.tmpdir, "f.db"),
            allowed_domains=["127.0.0.1"],
            max_pages=5, max_depth=1, concurrency=1,
            ingest_fn=lambda *a: None, allow_private_hosts=True,
            default_crawl_delay=0.0)
        pipeline.seed([f"{self.base}/linker"])
        stats = pipeline.run()
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(_CountingHandler.hits.get("/missing"), 1,
                         "a permanent 404 must be fetched exactly once")


class TestRecordEdgesContract(unittest.TestCase):
    """BUG-09: the bulk path shares caps + recrawl semantics with
    record_edge, and preserves first_seen."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.graph = LinkGraph(os.path.join(self.dir, "g.db"))

    def tearDown(self):
        self.graph.close()
        for _ in range(10):
            try:
                shutil.rmtree(self.dir)
                break
            except PermissionError:
                time.sleep(0.05)

    def test_upsert_preserves_first_seen(self):
        from nexus_search.links.graph import normalize_url
        self.graph.record_edges([("http://a.com", "http://b.com", "v1", "")])
        row = self.graph.conn.execute(
            "SELECT first_seen, last_seen, anchor_text FROM link_edges "
            "WHERE from_url = 'http://a.com/' AND to_url = 'http://b.com/'"
        ).fetchone()
        first_seen = row[0]
        self.assertEqual(row[2], "v1")
        time.sleep(0.02)
        self.graph.record_edges([("http://a.com", "http://b.com", "v2", "")])
        row2 = self.graph.conn.execute(
            "SELECT first_seen, last_seen, anchor_text FROM link_edges "
            "WHERE from_url = 'http://a.com/' AND to_url = 'http://b.com/'"
        ).fetchone()
        self.assertEqual(row2[0], first_seen, "first_seen must survive recrawl")
        self.assertEqual(row2[2], "v2")
        self.assertGreater(row2[1], row2[0])

    def test_bulk_respects_domain_pair_cap(self):
        from nexus_search.links.graph import MAX_EDGES_PER_DOMAIN_PAIR
        edges = [(f"http://src.com/p{i}", f"http://t.com/x{i}", "a", "")
                 for i in range(MAX_EDGES_PER_DOMAIN_PAIR + 20)]
        written = self.graph.record_edges(edges)
        self.assertEqual(written, MAX_EDGES_PER_DOMAIN_PAIR,
                         "the bulk path cannot bypass the domain-pair cap")

    def test_bulk_respects_source_cap(self):
        from nexus_search.links.graph import MAX_EDGES_PER_SOURCE_PAGE
        edges = [(f"http://one.com/x", f"http://h{i}.com/", "a", "")
                 for i in range(MAX_EDGES_PER_SOURCE_PAGE + 5)]
        written = self.graph.record_edges(edges)
        self.assertEqual(written, MAX_EDGES_PER_SOURCE_PAGE)

    def test_bulk_version_bump(self):
        before = self.graph.edges_version()
        self.graph.record_edges([("http://a.com", "http://b.com", "a", "")])
        self.assertGreater(self.graph.edges_version(), before)


class TestRateLimitProxyKeying(unittest.TestCase):
    """NEXUS_TRUST_PROXY: off by default (spoofed XFF ignored); on = key on
    the Nth-from-right forwarded entry (validated as an IP)."""

    class _FakeRequest:
        def __init__(self, xff):
            self.headers = {"X-Forwarded-For": xff} if xff else {}
            client = type("C", (), {"host": "127.0.0.1"})()
            self.client = client

    def test_off_by_default_ignores_xff(self):
        import nexus_search.core.api as api_mod
        saved = os.environ.get("NEXUS_TRUST_PROXY")
        os.environ.pop("NEXUS_TRUST_PROXY", None)
        try:
            key = api_mod._rate_limit_key(self._FakeRequest("1.2.3.4, 5.6.7.8"))
            self.assertFalse(key.startswith("ip:1.2.3.4"),
                             "untrusted XFF must never forge a bucket")
        finally:
            if saved is not None:
                os.environ["NEXUS_TRUST_PROXY"] = saved

    def test_on_keys_on_rightmost_trusted_hop(self):
        import nexus_search.core.api as api_mod
        saved = os.environ.get("NEXUS_TRUST_PROXY")
        os.environ["NEXUS_TRUST_PROXY"] = "1"
        try:
            key = api_mod._rate_limit_key(self._FakeRequest("9.9.9.9, 10.0.0.1"))
            self.assertEqual(key, "ip:10.0.0.1")
            junk = api_mod._rate_limit_key(self._FakeRequest("not-an-ip"))
            self.assertFalse(junk.startswith("ip:"),
                             "junk XFF falls back to the socket address")
        finally:
            if saved is None:
                os.environ.pop("NEXUS_TRUST_PROXY", None)
            else:
                os.environ["NEXUS_TRUST_PROXY"] = saved


class TestExplicitDnsPinningInstall(unittest.TestCase):
    """security.py no longer patches socket.getaddrinfo at import time."""

    def test_import_has_no_side_effect(self):
        # a clean interpreter: importing security alone must NOT patch the
        # global resolver (other tests in this process install pinning via
        # CrawlPipeline, so the check cannot run in-process)
        import subprocess, sys, textwrap
        code = textwrap.dedent("""
            import socket
            import nexus_search.crawler.security as security
            assert socket.getaddrinfo is not security._pinned_getaddrinfo, \
                "import must not patch getaddrinfo"
            assert not security._installed
            security.install_dns_pinning()
            assert socket.getaddrinfo is security._pinned_getaddrinfo
            security.install_dns_pinning()  # idempotent
            print("ok")
        """)
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, cwd=os.getcwd())
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("ok", out.stdout)

class TestIPv6SSRF(unittest.TestCase):
    """IPv6-literal SSRF: loopback, v4-mapped, link-local, ULA and the
    documentation range are all blocked; a global IPv6 literal passes."""

    def test_blocked_literals(self):
        from nexus_search.crawler.security import validate_url
        for url in ("http://[::1]/", "http://[::ffff:127.0.0.1]/",
                    "http://[fe80::1]/", "http://[fc00::1]/",
                    "http://[fd12::1]/", "http://[2001:db8::1]/"):
            self.assertFalse(validate_url(url), f"{url} must be blocked")

    def test_public_literal_passes_without_network(self):
        from nexus_search.crawler.security import validate_url
        # literal IPs skip DNS entirely — no network needed
        self.assertTrue(validate_url("http://[2606:4700:4700::1111]/"))


try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None


@unittest.skipIf(TestClient is None, "fastapi/httpx not installed")
class TestDebugGating(unittest.TestCase):
    """BUG-08: /search?debug=true gates behind the same key as
    /search/explain when one is configured."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._saved = {k: os.environ.get(k) for k in
                       ("NEXUS_ENV", "NEXUS_API_KEY", "NEXUS_DB",
                        "NEXUS_RATE_LIMIT", "NEXUS_CACHE_TTL")}
        os.environ["NEXUS_RATE_LIMIT"] = "10000/minute"
        os.environ["NEXUS_CACHE_TTL"] = "0"

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        from nexus_search.core import api
        importlib.reload(api)
        for _ in range(10):
            try:
                shutil.rmtree(self.tmpdir)
                break
            except PermissionError:
                time.sleep(0.05)

    def _boot(self, env_key=None):
        # (fixture: production-mode writes need the key too)
        os.environ["NEXUS_DB"] = os.path.join(self.tmpdir, "api.db")
        if env_key:
            os.environ["NEXUS_ENV"] = "production"
            os.environ["NEXUS_API_KEY"] = env_key
        else:
            os.environ["NEXUS_ENV"] = "dev"
            os.environ.pop("NEXUS_API_KEY", None)
        from nexus_search.core import api
        self.api = importlib.reload(api)
        self.client = TestClient(self.api.app)
        headers = {"X-API-Key": env_key} if env_key else {}
        self.client.post("/documents", json={
            "doc_id": "d1", "content": "python search", "title": "D1"},
            headers=headers)

    def test_debug_gated_in_production(self):
        self._boot("dbg-secret")
        r = self.client.get("/search", params={"q": "python", "debug": "true"})
        self.assertEqual(r.status_code, 401,
                         "debug score contributions are key-gated internals")
        r = self.client.get("/search", params={"q": "python", "debug": "true"},
                            headers={"X-API-Key": "dbg-secret"})
        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(r.json()["results"][0]["bm25_normalized"])

    def test_debug_open_in_dev(self):
        self._boot()
        r = self.client.get("/search", params={"q": "python", "debug": "true"})
        self.assertEqual(r.status_code, 200)
        # plain (non-debug) reads stay open in production too
        self._boot("dbg-secret")
        r = self.client.get("/search", params={"q": "python"})
        self.assertEqual(r.status_code, 200)


class TestMarkdownReader(unittest.TestCase):
    def _read(self, text: str) -> str:
        from nexus_search.ingestion.connectors.files import read_markdown_file
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "t.md")
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
            return read_markdown_file(type("P", (), {"__lagraph__": 0}) and
                                      __import__("pathlib").Path(p))

    def test_front_matter_dropped(self):
        out = self._read("---\ntitle: X\n---\n\n# Real Title\n\nbody text")
        self.assertNotIn("title: X", out)
        self.assertIn("Real Title", out)

    def test_fences_drop_markers_keep_code(self):
        out = self._read("intro\n```python\nprint('hi')\n```\nafter")
        self.assertNotIn("```", out)
        self.assertIn("print('hi')", out)

    def test_headings_links_emphasis(self):
        out = self._read("# H1\n\n[docs here](http://x.com) and "
                         "![alt img](http://i.png) with **bold** and *em*")
        self.assertNotIn("#", out)
        self.assertIn("H1", out)
        self.assertIn("docs here", out)
        self.assertNotIn("http://x.com", out)
        self.assertIn("alt img", out)
        self.assertIn("bold", out)
        self.assertNotIn("**", out)

    def test_everything_stays_searchable(self):
        out = self._read("- bullet one\n> quoted line\n| a | b |\n|---|---|\n| 1 | 2 |")
        self.assertIn("bullet one", out)
        self.assertIn("quoted line", out)
        self.assertIn("a", out) and self.assertIn("2", out)


class TestMimeSniffing(unittest.TestCase):
    def test_pdf_bytes_named_txt_detected(self):
        from nexus_search.ingestion.mime import sniff_content_type, detect_mime_type
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "actually-a-pdf.txt")
            with open(p, "wb") as f:
                f.write(b"%PDF-1.4 fake but magic\nrest")
            self.assertEqual(sniff_content_type(p), "application/pdf")
            self.assertEqual(detect_mime_type(p), "application/pdf")

    def test_binary_garbage_named_txt_refused_by_reader(self):
        from nexus_search.ingestion.connectors.files import read_file
        import pathlib
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "garbage.txt"
            p.write_bytes(bytes(range(256)) * 16)
            self.assertEqual(read_file(p), "",
                             "binary garbage must not hit the text reader")

    def test_office_zip_named_txt_routes_by_content(self):
        # a REAL docx (python-docx writes valid zips) with a lying extension
        from docx import Document
        from nexus_search.ingestion.mime import detect_mime_type
        from nexus_search.ingestion.connectors.files import read_file
        import pathlib
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "doc.txt"
            document = Document()
            document.add_paragraph("hello docx")
            document.save(str(p))
            self.assertEqual(detect_mime_type(p),
                             "application/vnd.openxmlformats-officedocument"
                             ".wordprocessingml.document")
            self.assertIn("hello docx", read_file(p))

    def test_plain_text_stays_text(self):
        from nexus_search.ingestion.mime import sniff_content_type
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "plain.txt")
            with open(p, "w", encoding="utf-8") as f:
                f.write("just words here\n")
            self.assertEqual(sniff_content_type(p), "text/plain")


class TestAbPurgeCli(unittest.TestCase):
    def test_purge_and_count(self):
        from nexus_search.ranking.ab import ExperimentLog, main
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "ab.db")
            log = ExperimentLog(db)
            log.record("q1", "control", "hybrid")
            log.record("q2", "treatment", "hybrid")
            # age one row beyond the window
            log.conn.execute(
                "UPDATE query_experiments SET created_at = ? WHERE query = 'q1'",
                (time.time() - 40 * 86400.0,))
            log.conn.commit()
            log.close()
            self.assertEqual(main(["count", "--db", db]) in (0,), True)
            self.assertEqual(main(["purge", "--db", db, "--days", "30"]), 0)
            check = ExperimentLog(db)
            try:
                remaining = check.count()
            finally:
                check.close()
            self.assertEqual(remaining, 1, "only the aged row is purged")


if __name__ == "__main__":
    unittest.main()
