"""DNS pinning must (a) not break real connections and (b) defeat DNS rebinding."""
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from nexus_search.crawler import security
from nexus_search.crawler.fetcher import Fetcher
from nexus_search.crawler.pipeline import CrawlPipeline


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b"<html><head><title>ok</title></head><body>reached</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TestDnsPinning(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.port = cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _fake_dns(self, answers):
        """DNS that answers `answers[i]` on lookup #i (last answer repeats), like a rebinding attacker."""
        calls = {"n": 0}
        real = security._real_getaddrinfo

        def fake(host, port=None, *a, **k):
            if host == "pinme.test":
                ip = answers[min(calls["n"], len(answers) - 1)]
                calls["n"] += 1
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, int(port or 0)))]
            return real(host, port, *a, **k)

        return fake

    def test_pinned_lookup_keeps_requested_port_and_type(self):
        addrs = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
                 (socket.AF_INET, socket.SOCK_DGRAM, 17, "", ("93.184.216.34", 0))]
        with security.pinned_resolution("pinme.test", addrs):
            result = socket.getaddrinfo("pinme.test", 8080, 0, socket.SOCK_STREAM)
        self.assertEqual([(r[1], r[4]) for r in result], [(socket.SOCK_STREAM, ("93.184.216.34", 8080))])

    def test_real_fetch_works_with_pinning_enabled(self):
        # 127.0.0.1 plays "a public site" here; only the public/private check is relaxed.
        with mock.patch.object(security, "_real_getaddrinfo", self._fake_dns(["127.0.0.1"])), \
             mock.patch.object(security, "_blocked_ip", lambda ip: False):
            f = Fetcher(dns_pin=security.pin_dns_for_url)
            r = f.fetch(f"http://pinme.test:{self.port}/")
        self.assertEqual((r.status_code, r.error), (200, None))

    def test_rebinding_is_defeated(self):
        # Lookup #1 (validation) -> 127.0.0.1 = "public". Every later lookup -> 127.0.0.2 = "private".
        # The server only listens on 127.0.0.1, so success proves the connection used the pinned IP.
        blocked = lambda ip: str(ip) != "127.0.0.1"
        with mock.patch.object(security, "_real_getaddrinfo", self._fake_dns(["127.0.0.1", "127.0.0.2"])), \
             mock.patch.object(security, "_blocked_ip", blocked):
            pinned = Fetcher(dns_pin=security.pin_dns_for_url, max_retries=0).fetch(f"http://pinme.test:{self.port}/")
        # Control: with no pin, the connection-time lookup is the flipped answer.
        with mock.patch.object(security, "_real_getaddrinfo", self._fake_dns(["127.0.0.2"])):
            unpinned = Fetcher(max_retries=0).fetch(f"http://pinme.test:{self.port}/")
        self.assertEqual(pinned.status_code, 200)
        self.assertIsNotNone(unpinned.error)

    def test_production_path_crawls_with_ssrf_protection_on(self):
        """The default config (allow_private_hosts=False) is what real crawls use;
        every other test runs with it True, which is how the pinning bug got through."""
        import os, tempfile
        titles = []
        pipe = CrawlPipeline(
            db_path=os.path.join(tempfile.mkdtemp(), "f.db"), allow_private_hosts=False,
            max_pages=3, max_depth=0, concurrency=1, default_crawl_delay=0.0,
            ingest_fn=lambda u, t, x, m: titles.append(t),
        )
        with mock.patch.object(security, "_real_getaddrinfo", self._fake_dns(["127.0.0.1"])), \
             mock.patch.object(security, "_blocked_ip", lambda ip: False):
            pipe.seed([f"http://pinme.test:{self.port}/"])
            stats = pipe.run()
        self.assertEqual((stats["crawled"], stats["errors"]), (1, 0))
        self.assertEqual(titles, ["ok"])


if __name__ == "__main__":
    unittest.main()