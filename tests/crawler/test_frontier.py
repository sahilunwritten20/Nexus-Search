import os
import tempfile
import unittest

from nexus_search.crawler.frontier import Frontier


class TestFrontier(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.frontier = Frontier(self.db_path)

    def tearDown(self):
        self.frontier.close()
        os.remove(self.db_path)

    def test_add_new_url_returns_true(self):
        self.assertTrue(self.frontier.add("https://example.com/a", depth=0))

    def test_duplicate_url_not_added_twice(self):
        self.frontier.add("https://example.com/a", depth=0)
        added_again = self.frontier.add("https://example.com/a", depth=0)
        self.assertFalse(added_again)
        self.assertEqual(self.frontier.pending_count(), 1)

    def test_visited_url_not_requeued(self):
        self.frontier.add("https://example.com/a", depth=0)
        [entry] = self.frontier.next_batch(1)
        self.frontier.mark_done(entry.url, content_hash="abc123")

        added_again = self.frontier.add("https://example.com/a", depth=1)
        self.assertFalse(added_again)
        self.assertEqual(self.frontier.pending_count(), 0)
        self.assertTrue(self.frontier.is_visited("https://example.com/a"))

    def test_next_batch_respects_priority(self):
        self.frontier.add("https://example.com/low", depth=0, priority=0)
        self.frontier.add("https://example.com/high", depth=0, priority=10)
        batch = self.frontier.next_batch(1)
        self.assertEqual(batch[0].url, "https://example.com/high")

    def test_next_batch_claims_urls_as_in_progress(self):
        self.frontier.add("https://example.com/a", depth=0)
        first_batch = self.frontier.next_batch(5)
        self.assertEqual(len(first_batch), 1)
        second_batch = self.frontier.next_batch(5)
        self.assertEqual(len(second_batch), 0, "in-progress URLs should not be claimed twice")

    def test_mark_error_removes_from_pending_but_not_visited(self):
        self.frontier.add("https://example.com/a", depth=0)
        [entry] = self.frontier.next_batch(1)
        self.frontier.mark_error(entry.url, "timeout")
        self.assertEqual(self.frontier.pending_count(), 0)
        self.assertFalse(self.frontier.is_visited("https://example.com/a"))

    def test_normalization_applied_on_add(self):
        self.frontier.add("https://example.com/a/", depth=0)
        added_again = self.frontier.add("https://example.com/a", depth=0)
        self.assertFalse(added_again, "trailing-slash variant should normalize to the same URL")


if __name__ == "__main__":
    unittest.main()
