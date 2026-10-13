"""Review ROBOTS-GAP: redirect hops must clear the TARGET domain's
robots.txt (and the blocklist / allowed-domains gates) — pre-fix the
fetcher validated only SSRF per hop, so a page could redirect the crawl
into a path the target domain's robots.txt disallows.

Setup: one local server playing two "domains" (Host-header routing) over
fake DNS (both resolve to the loopback listener, blocked-IP check relaxed
exactly like the pinning suite does), public-crawl mode
(allow_private_hosts=False) so the full gate chain runs.
"""
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import nexus_search.crawler.security as security
from nexus_search.crawler.pipeline import CrawlPipeline

UA = "NexusSearchBot/0.1 (+https://nexus-search.example/bot)"


class _Router(BaseHTTPRequestHandler):
    """Host-aware router: a.test allows everything; b.test's robots
    disallows /private. Hits on /private are recorded for assertions."""

    hits = []

    def log_message(self, *a):
        pass

    def _body(self, text, ctype="text/html"):
        data = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        if self.path.endswith("/robots.txt"):
            if host == "b.test":
                self._body("User-agent: *\nDisallow: /private\n",
                           ctype="text/plain")
            else:
                self._body("User-agent: *\nAllow: /\n", ctype="text/plain")
            return
        if self.path == "/redirect-to-disallowed":
            # a.test redirects into a b.test path its robots disallows
            self.send_response(302)
            self.send_header("Location", f"http://b.test:{self.server.server_port}/private")
            self.end_headers()
            return
        if self.path == "/redirect-to-allowed":
            self.send_response(302)
            self.send_header("Location", f"http://b.test:{self.server.server_port}/allowed")
            self.end_headers()
            return
        if self.path == "/private":
            _Router.hits.append("/private")
            self._body("<html><body>forbidden content</body></html>")
            return
        _Router.hits.append(self.path)
        self._body("<html><body>fetched</body></html>")


class TestRedirectRobotsGating(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        security.install_dns_pinning()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Router)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.port = cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _pipeline(self, **kw):
        import os, tempfile
        defaults = dict(
            db_path=os.path.join(tempfile.mkdtemp(), "frontier.db"),
            allow_private_hosts=False,
            max_pages=5, max_depth=0, concurrency=1,
            default_crawl_delay=0.0, user_agent=UA,
            ingest_fn=lambda u, t, x, m: None,
        )
        defaults.update(kw)
        return CrawlPipeline(**defaults)

    def _dns(self):
        """Both fake domains resolve to the loopback listener; the
        public/private check is relaxed so the SSRF gate passes (the
        robots gate under test is orthogonal to the IP gate)."""
        def fake(host, port=None, *a, **k):
            if host in ("a.test", "b.test"):
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                         ("127.0.0.1", int(port or 0)))]
            return security._real_getaddrinfo(host, port, *a, **k)
        return mock.patch.object(security, "_real_getaddrinfo", fake), \
            mock.patch.object(security, "_blocked_ip", lambda ip: False)

    def test_redirect_into_robots_disallowed_path_is_refused(self):
        _Router.hits = []
        pipe = self._pipeline()
        with self._dns()[0], self._dns()[1]:
            pipe.seed([f"http://a.test:{self.port}/redirect-to-disallowed"])
            stats = pipe.run()
        self.assertNotIn("/private", _Router.hits,
                         "the fetcher followed a redirect into a path the "
                         "target domain's robots.txt disallows")
        # the entry ends as an error (refused hop), not a crawl
        self.assertEqual(stats["crawled"], 0)
        self.assertEqual(stats["errors"], 1)

    def test_redirect_into_robots_allowed_path_still_works(self):
        _Router.hits = []
        pipe = self._pipeline()
        with self._dns()[0], self._dns()[1]:
            pipe.seed([f"http://a.test:{self.port}/redirect-to-allowed"])
            stats = pipe.run()
        self.assertEqual(stats["crawled"], 1,
                         "an allowed redirect target must still be crawled")
        self.assertTrue(any(h.endswith("/allowed") for h in _Router.hits))

    def test_redirect_to_blocklisted_host_is_refused(self):
        _Router.hits = []
        import os, tempfile
        db = os.path.join(tempfile.mkdtemp(), "bl-frontier.db")
        pipe = self._pipeline(db_path=db)
        with self._dns()[0], self._dns()[1]:
            pipe.blocklist.block("b.test", reason="opted out")
            pipe.seed([f"http://a.test:{self.port}/redirect-to-allowed"])
            stats = pipe.run()
        # run() closed the pipeline's own connections; a fresh Blocklist
        # over the same frontier DB removes the test's row
        from nexus_search.crawler.blocklist import Blocklist
        bl = Blocklist(db)
        try:
            bl.unblock("b.test")
        finally:
            bl.close()
        self.assertEqual(stats["crawled"], 0,
                         "a redirect to a blocklisted host must be refused")
        self.assertEqual(stats["errors"], 1)

    def test_redirect_outside_allowed_domains_is_refused(self):
        _Router.hits = []
        pipe = self._pipeline(allowed_domains=["a.test"])
        with self._dns()[0], self._dns()[1]:
            pipe.seed([f"http://a.test:{self.port}/redirect-to-allowed"])
            stats = pipe.run()
        self.assertEqual(stats["crawled"], 0,
                         "a redirect outside allowed_domains must be refused")
        self.assertEqual(stats["errors"], 1)


if __name__ == "__main__":
    unittest.main()
