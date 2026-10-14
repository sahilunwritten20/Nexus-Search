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

    def test_crawl_delay_survives_forward_wall_clock_skew(self):
        """Review POLITENESS-MONO: pacing must run on the monotonic
        clock. A wall-clock step FORWARD (NTP correction after drift)
        made the old time.time() math think the whole delay had elapsed
        and let the next request fire immediately — a politeness
        violation invisible to every test that runs on a healthy clock."""
        from unittest import mock
        pm = PolitenessManager(default_delay=10.0)
        state = {"mono": 1000.0, "wall": 1700000000.0}

        def fake_time():
            return state["wall"]

        def fake_mono():
            return state["mono"]

        with mock.patch("time.time", fake_time), \
             mock.patch("time.monotonic", fake_mono):
            pm.record_request("example.com")
            state["mono"] += 1.0     # ONE real second passed
            state["wall"] += 3600.0  # wall clock jumped an hour forward
            wait = pm.time_until_allowed("example.com")
        self.assertGreater(
            wait, 8.0,
            f"a wall-clock jump must not collapse the crawl delay (got {wait})")

    def test_reserve_slot_survives_forward_wall_clock_skew(self):
        from unittest import mock
        pm = PolitenessManager(default_delay=10.0)
        state = {"mono": 1000.0, "wall": 1700000000.0}

        def fake_time():
            return state["wall"]

        def fake_mono():
            return state["mono"]

        with mock.patch("time.time", fake_time), \
             mock.patch("time.monotonic", fake_mono):
            pm.reserve_slot("example.com")
            state["mono"] += 1.0
            state["wall"] += 3600.0
            wait = pm.reserve_slot("example.com")
        self.assertGreater(
            wait, 8.0,
            f"slot spacing must survive wall-clock skew (got {wait})")


if __name__ == "__main__":
    unittest.main()
