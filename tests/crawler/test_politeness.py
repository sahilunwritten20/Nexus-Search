import time
import unittest

from nexus_search.crawler.politeness import PolitenessManager

SAMPLE_ROBOTS = """User-agent: *
Disallow: /private/
Crawl-delay: 2
"""


class TestPoliteness(unittest.TestCase):
    def setUp(self):
        self.pm = PolitenessManager(default_delay=1.0)
        self.pm.register_robots_txt("example.com", SAMPLE_ROBOTS)

    def test_disallowed_path_blocked(self):
        self.assertFalse(self.pm.is_allowed("example.com", "https://example.com/private/secret"))

    def test_allowed_path_permitted(self):
        self.assertTrue(self.pm.is_allowed("example.com", "https://example.com/public/page"))

    def test_crawl_delay_parsed_from_robots_txt(self):
        self.assertEqual(self.pm.crawl_delay("example.com"), 2.0)

    def test_unregistered_domain_defaults_open(self):
        pm = PolitenessManager()
        self.assertTrue(pm.is_allowed("unknown.com", "https://unknown.com/x"))

    def test_unregistered_domain_uses_default_delay(self):
        pm = PolitenessManager(default_delay=3.0)
        self.assertEqual(pm.crawl_delay("unknown.com"), 3.0)

    def test_no_wait_needed_before_first_request(self):
        self.assertEqual(self.pm.time_until_allowed("example.com"), 0.0)

    def test_wait_required_immediately_after_a_request(self):
        self.pm.record_request("example.com")
        wait = self.pm.time_until_allowed("example.com")
        self.assertGreater(wait, 0)
        self.assertLessEqual(wait, 2.0)

    def test_wait_clears_after_delay_passes(self):
        pm = PolitenessManager(default_delay=0.05)
        pm.record_request("example.com")
        time.sleep(0.06)
        self.assertEqual(pm.time_until_allowed("example.com"), 0.0)


if __name__ == "__main__":
    unittest.main()
