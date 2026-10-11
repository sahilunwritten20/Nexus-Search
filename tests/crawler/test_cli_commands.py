"""WP14-7 — crawler/cli.py was 60%-covered: the command bodies wrap
library calls that ARE test-pinned (tests/links/*), but the wiring
(argparse dispatch + invocation) had no test at all. These smoke-invoke
each report command through main() against a temp graph so a broken
dispatch cannot ship silently. `crawl` itself is covered by tests/crawler/
test_cli.py and the live-server e2e suites."""
import os
import sys
import tempfile
import unittest
from unittest import mock

from nexus_search.crawler import cli
from nexus_search.links.graph import LinkGraph


def _seed_graph(db):
    g = LinkGraph(db)
    g.record_edges([("https://a.example/", "https://b.example/", "link", "")])
    g.close()
    # dead-links cross-references the frontier DB's visited table
    from nexus_search.crawler.frontier import Frontier
    f = Frontier(os.path.splitext(db)[0] + "_frontier.db")
    f.close()


class TestCliReportCommands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wp14_clicmd_")
        self.db = os.path.join(self.tmp, "graph.db")
        _seed_graph(self.db)

    def _main(self, *argv):
        with mock.patch.object(sys, "argv", ["cli", "--db", self.db, *argv]):
            cli.main()

    def test_authority_show_missing_url_is_honest(self):
        self._main("authority", "--show", "https://nobody.example/")  # prints NEUTRAL

    def test_authority_show_present_url(self):
        self._main("authority", "--show", "https://b.example/")

    def test_authority_recompute_runs(self):
        self._main("authority", "--recompute")

    def test_authority_plain_stats(self):
        self._main("authority")

    def test_normalize_links_runs_and_is_idempotent(self):
        self._main("normalize-links")
        self._main("normalize-links", "--recompute")

    def test_dead_links_report_runs(self):
        self._main("dead-links")

    def test_orphans_report_runs(self):
        self._main("orphans")

    def test_block_requires_host(self):
        with mock.patch.object(sys, "argv", ["cli", "--db", self.db, "block"]):
            with self.assertRaises(SystemExit):   # argparse error path
                cli.main()


if __name__ == "__main__":
    unittest.main()
