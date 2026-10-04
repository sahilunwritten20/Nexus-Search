"""CrawlPipeline constructor guards (user-agent placeholder refusal)."""
import os
import shutil
import tempfile
import unittest
import logging

from nexus_search.crawler.pipeline import CrawlPipeline


class TestUserAgentGuard(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "frontier.db")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_placeholder_ua_rejected_on_public_crawl(self):
        for ua in ("NexusSearchBot/0.1 (+https://example.com/bot)",
                   "NexusSearchBot/0.1 (+https://YOUR-SITE/bot)",   # shipped config default
                   "NexusSearchBot/0.1 (+https://your-site/bot)",
                   None, ""):
            with self.assertRaises(ValueError):
                CrawlPipeline(db_path=self.db, user_agent=ua)

    def test_real_ua_accepted(self):
        crawler = CrawlPipeline(db_path=self.db,
                                user_agent="MyBot/1.0 (+https://my.example.net/bot)")
        crawler.close()

    def test_placeholder_allowed_with_private_hosts_but_loud(self):
        logger_name = "nexus_search.crawler.pipeline"
        with self.assertLogs(logger_name, level="WARNING") as logs:
            crawler = CrawlPipeline(db_path=self.db, user_agent="", allow_private_hosts=True)
            crawler.close()
        self.assertTrue(any("placeholder" in m for m in logs.output))


class TestCloseNeverSilentlySwallows(unittest.TestCase):
    """P1-12b: pipeline.close() used to `except Exception: pass` per closer —
    a real shutdown error (frontier commit failure, locked DB) vanished. It
    must now be logged while the remaining closers still run."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "frontier.db")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_failing_closer_is_logged_and_rest_still_closed(self):
        crawler = CrawlPipeline(db_path=self.db, user_agent="",
                                allow_private_hosts=True)
        closed = []

        def boom():
            raise RuntimeError("simulated shutdown failure")

        crawler.fetcher.close = boom
        crawler.frontier.close = lambda: closed.append("frontier")
        crawler.blocklist.close = lambda: closed.append("blocklist")

        with self.assertLogs("nexus_search.crawler.pipeline",
                             level="WARNING") as logs:
            crawler.close()  # must NOT raise
        self.assertIn("frontier", closed)   # later closers still ran
        self.assertIn("blocklist", closed)
        self.assertTrue(any("simulated shutdown failure" in m for m in logs.output),
                        logs.output)


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    unittest.main()
