"""RED tests for the ROUND-2 SKEPTIC findings (fresh-context review of
the review's own crawler fixes).

SKEP-1 (P1): robots.py compiled attacker-controlled patterns into
  `.*`-chains — `/a*a*a*a*a*a*a*a*b` against a long path NEVER RETURNS
  (measured: 8 stars x 5000 chars = hang). The matcher must be linear.
SKEP-2 (P2): Crawl-delay was consumed unvalidated: 99999999 -> ~3.16
  YEAR sleep per worker; nan/-5 -> pacing silently OFF; 1e999 -> inf ->
  OverflowError. Must clamp [0, 60] and require finite.
SKEP-3 (P2): redirect hops passed the permission gate but bypassed
  crawl-delay PACING for the target domain — a crawled site could 302
  the crawler into hammering a third party at full speed.
SKEP-4 (P3): sitemap index children and loc URLs skipped the blocklist
  (a blocklisted host listed in an index was still fetched).
SKEP-5 (P3): _pinned_getaddrinfo fell back to REAL DNS for service-name
  ports on pinned hosts — the exact rebind window the pin exists to
  close, one library refactor away. Must fail closed.
"""
import os
import socket
import tempfile
import time
import unittest
from unittest import mock

from nexus_search.crawler.politeness import PolitenessManager
from nexus_search.crawler.robots import RobotsRules
from nexus_search.crawler import security
from nexus_search.crawler.security import (
    install_dns_pinning, pinned_resolution,
)


class TestSkep1RobotsReDoS(unittest.TestCase):
    EVIL_RULE = "/a*a*a*a*a*a*a*a*b"

    def _evil_robots(self):
        return RobotsRules("User-agent: *\nDisallow: %s\n" % self.EVIL_RULE)

    def test_evil_pattern_is_linear_not_hanging(self):
        rules = self._evil_robots()
        path = "/a" + "a" * 5000
        started = time.monotonic()
        allowed = rules.is_allowed("AnyBot", "https://x.test" + path)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0,
                        f"evil robots pattern took {elapsed:.2f}s — "
                        "catastrophic backtracking (SKEP-1)")
        # and the semantics are unchanged: no 'b' -> the rule still blocks
        # a prefix match... '/a...a' with no b: pattern needs trailing b.
        self.assertTrue(allowed, "path without the trailing b is not matched")

    def test_evil_pattern_with_b_still_blocks(self):
        rules = self._evil_robots()
        self.assertFalse(rules.is_allowed(
            "AnyBot", "https://x.test/a" + "a" * 5000 + "b"))

    def test_many_wildcards_parse_and_match_fast(self):
        # ~200 wildcards within the 512KB robots cap: parse + match must
        # stay well under a second
        rule = "/" + "a*"*200 + "b"
        started = time.monotonic()
        rules = RobotsRules("User-agent: *\nDisallow: %s\n" % rule)
        rules.is_allowed("AnyBot", "https://x.test/" + "a" * 300)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0, f"200-wildcard rule: {elapsed:.2f}s")


class TestSkep2CrawlDelayClamp(unittest.TestCase):
    def _delay(self, value):
        robots = f"User-agent: *\nCrawl-delay: {value}\nDisallow: /x\n"
        pm = PolitenessManager(user_agent="AnyBot", default_delay=1.0)
        pm.register_robots_txt("d.test", robots)
        return pm.crawl_delay("d.test")

    def test_huge_delay_is_clamped(self):
        self.assertLessEqual(self._delay("99999999"), 60.0,
                             "multi-year worker sleep (SKEP-2)")

    def test_infinite_delay_is_clamped(self):
        self.assertLessEqual(self._delay("1e999"), 60.0)

    def test_nan_delay_falls_back_to_default(self):
        self.assertEqual(self._delay("nan"), 1.0,
                         "NaN crawl-delay silently disabled pacing")

    def test_negative_delay_falls_back_to_default(self):
        self.assertEqual(self._delay("-5"), 1.0)

    def test_reasonable_delay_passes_through(self):
        self.assertEqual(self._delay("7"), 7.0)

    def test_reserve_slot_first_contact_with_clamped_delay_is_sane(self):
        robots = "User-agent: *\nCrawl-delay: 99999999\n"
        pm = PolitenessManager(user_agent="AnyBot", default_delay=1.0)
        pm.register_robots_txt("d.test", robots)
        wait = pm.reserve_slot("d.test")
        self.assertLessEqual(wait, 60.5,
                             "first-contact wait must be bounded too")


class TestSkep3HopPacing(unittest.TestCase):
    def test_redirect_hop_paces_the_target_domain(self):
        import importlib
        from nexus_search.crawler.pipeline import CrawlPipeline
        waits = []

        class FakePoliteness:
            def __init__(self, real):
                self._real = real

            def has_robots(self, d):
                return True

            def is_allowed(self, d, u):
                return True

            def reserve_slot(self, d):
                waits.append(d)
                return 0.0

        db = os.path.join(tempfile.mkdtemp(), "pace-frontier.db")
        pipe = CrawlPipeline(
            db_path=db, allow_private_hosts=False, max_pages=1,
            max_depth=0, concurrency=1, default_crawl_delay=0.0,
            user_agent="NexusSearchBot/0.1 (+https://nexus-search.example/bot)",
            ingest_fn=lambda *a: None)
        real_politeness = pipe.politeness
        pipe.politeness = FakePoliteness(real_politeness)
        try:
            # simulate a redirect hop through the gate
            with mock.patch("nexus_search.crawler.pipeline.validate_url",
                            return_value=True):
                allowed = pipe._hop_gate("https://target.test/hop")
            self.assertTrue(allowed)
            self.assertIn("target.test", waits,
                          "redirect hops must reserve a crawl-delay slot "
                          "for the TARGET domain (SKEP-3)")
        finally:
            pipe.close()


class TestSkep4SitemapBlocklist(unittest.TestCase):
    def test_blocklisted_sitemap_child_is_not_fetched(self):
        from nexus_search.crawler.pipeline import CrawlPipeline
        db = os.path.join(tempfile.mkdtemp(), "bl-sm-frontier.db")
        pipe = CrawlPipeline(
            db_path=db, allow_private_hosts=True, max_pages=5,
            max_depth=0, concurrency=1, default_crawl_delay=0.0,
            user_agent="NexusSearchBot/0.1 (+https://nexus-search.example/bot)",
            ingest_fn=lambda *a: None)
        try:
            pipe.blocklist.block("blocked.test", reason="opted out")
            index_xml = ('<sitemapindex xmlns="http://www.sitemaps.org/'
                         'schemas/sitemap/0.9"><sitemap><loc>'
                         'http://blocked.test/sitemap.xml</loc></sitemap>'
                         '<sitemap><loc>http://ok.test/sitemap.xml</loc>'
                         '</sitemap></sitemapindex>')
            empty = ('<urlset xmlns="http://www.sitemaps.org/schemas/'
                     'sitemap/0.9"></urlset>')

            def fake_fetch(url):
                if "seed.test" in url:
                    return index_xml.encode()
                return empty.encode()

            fetched = []
            with mock.patch.object(
                    pipe.fetcher, "fetch_bytes",
                    side_effect=lambda url: fetched.append(url)
                    or fake_fetch(url)):
                pipe.seed_from_sitemap("https://seed.test/sitemap.xml")
            self.assertFalse(any("blocked.test" in f for f in fetched),
                             "a blocklisted host listed in a sitemap index "
                             "must not be fetched (SKEP-4)")
        finally:
            pipe.close()

    def test_blocklisted_loc_is_not_queued(self):
        from nexus_search.crawler.pipeline import CrawlPipeline
        db = os.path.join(tempfile.mkdtemp(), "bl-loc-frontier.db")
        pipe = CrawlPipeline(
            db_path=db, allow_private_hosts=True, max_pages=5,
            max_depth=0, concurrency=1, default_crawl_delay=0.0,
            user_agent="NexusSearchBot/0.1 (+https://nexus-search.example/bot)",
            ingest_fn=lambda *a: None)
        try:
            pipe.blocklist.block("blocked.test", reason="opted out")
            xml = ('<urlset xmlns="http://www.sitemaps.org/schemas/'
                   'sitemap/0.9"><url><loc>http://blocked.test/p</loc>'
                   '</url><url><loc>http://ok.test/p</loc></url></urlset>')
            with mock.patch.object(pipe.fetcher, "fetch_bytes",
                                  return_value=xml.encode()):
                added = pipe.seed_from_sitemap("https://seed.test/s.xml")
            self.assertEqual(added, 1,
                             "blocklisted loc URLs must not be queued")
            self.assertEqual(
                pipe.frontier.pending_count(), 1,
                "only the non-blocklisted URL is queued")
        finally:
            pipe.close()


class TestSkep5PinnedServicePortFailsClosed(unittest.TestCase):
    def test_pinned_host_with_service_name_port_never_uses_real_dns(self):
        install_dns_pinning()
        addrs = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        real_calls = []

        def spy(host, port=None, *a, **k):
            real_calls.append((host, port))
            return security._real_getaddrinfo(host, port, *a, **k)
        with mock.patch.object(security, "_real_getaddrinfo", side_effect=spy):
            with pinned_resolution("svc.test", addrs):
                with self.assertRaises(socket.gaierror,
                                      msg="service-name ports on a pinned "
                                          "host must fail closed, not fall "
                                          "back to live DNS (SKEP-5)"):
                    socket.getaddrinfo("svc.test", "http")
        self.assertEqual(real_calls, [],
                         "no real DNS lookup may happen for a pinned host")


if __name__ == "__main__":
    unittest.main()
