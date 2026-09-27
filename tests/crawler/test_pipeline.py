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


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    unittest.main()
