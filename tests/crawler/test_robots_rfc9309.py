"""RFC 9309 conformance for robots.txt matching (review ROBOTS-WILDCARD).

The stdlib robotparser treats `*` and `$` as LITERAL characters — sites
using the RFC 9309 wildcard syntax (extremely common: `Disallow: /*.pdf$`)
were crawled far more broadly than their authors intended, and the gap
was only "documented". These tests encode the RFC's own examples plus
the classic conformance cases; they run against PolitenessManager so
the production surface (not a private helper) is what's pinned.

Pre-fix, every wildcard/$/longest-match case below fails.
"""
import unittest

from nexus_search.crawler.politeness import PolitenessManager

UA = "NexusSearchBot"


class RobotsRfc9309Test(unittest.TestCase):
    def _pm(self, robots: str, ua: str = UA):
        pm = PolitenessManager(user_agent=ua, default_delay=0.0)
        pm.register_robots_txt("example.com", robots)
        return pm

    def allowed(self, robots, path):
        pm = self._pm(robots)
        return pm.is_allowed("example.com", f"https://example.com{path}")

    # --------------------------------------------------------- wildcards

    def test_star_matches_any_sequence(self):
        robots = "User-agent: *\nDisallow: /*.pdf$\n"
        self.assertFalse(self.allowed(robots, "/doc.pdf"))
        self.assertFalse(self.allowed(robots, "/a/b/c/report.pdf"))
        self.assertTrue(self.allowed(robots, "/doc.pdfx"))   # $ anchors the end
        self.assertTrue(self.allowed(robots, "/doc.pdf?x=1"))  # ...query breaks the anchor
        self.assertTrue(self.allowed(robots, "/plain"))

    def test_star_matches_empty_sequence(self):
        robots = "User-agent: *\nDisallow: /foo*bar\n"
        self.assertFalse(self.allowed(robots, "/foobar"))
        self.assertFalse(self.allowed(robots, "/foo-x-bar"))
        self.assertTrue(self.allowed(robots, "/fooba"))

    def test_dollar_anchors_exact_end(self):
        robots = "User-agent: *\nDisallow: /private$\n"
        self.assertFalse(self.allowed(robots, "/private"))
        self.assertTrue(self.allowed(robots, "/private/"))
        self.assertTrue(self.allowed(robots, "/privatex"))

    def test_dollar_only_special_at_end(self):
        # a $ in the middle is a literal (RFC 9309 §2.2.4: only a
        # TRAILING $ is the anchor)
        robots = "User-agent: *\nDisallow: /a$b\n"
        self.assertFalse(self.allowed(robots, "/a$b/c"))

    # ------------------------------------------------------ longest match

    def test_longest_rule_wins_allow_over_shorter_disallow(self):
        # RFC 9309 §2.2.4 example: the longer Allow rule wins
        robots = ("User-agent: *\n"
                  "Disallow: /p\n"
                  "Allow: /p/\n")
        self.assertTrue(self.allowed(robots, "/p/here"))
        self.assertFalse(self.allowed(robots, "/px"))          # disallow still applies
        self.assertFalse(self.allowed(robots, "/p"))           # equal? no: /p matches Disallow only

    def test_longest_rule_wins_disallow_over_shorter_allow(self):
        robots = ("User-agent: *\n"
                  "Allow: /p/\n"
                  "Disallow: /p/secret\n")
        self.assertFalse(self.allowed(robots, "/p/secret/x"))
        self.assertTrue(self.allowed(robots, "/p/other"))

    def test_equal_specificity_allows(self):
        # RFC 9309: tie -> the least restrictive rule (Allow) wins
        robots = ("User-agent: *\n"
                  "Disallow: /page\n"
                  "Allow: /page\n")
        self.assertTrue(self.allowed(robots, "/page"))

    # ------------------------------------------------------- group choice

    def test_exact_ua_group_beats_star_group(self):
        robots = ("User-agent: *\n"
                  "Disallow: /\n"
                  f"User-agent: {UA}\n"
                  "Allow: /\n")
        self.assertTrue(self.allowed(robots, "/anything"))

    def test_star_group_applies_to_unknown_ua(self):
        robots = ("User-agent: *\n"
                  "Disallow: /private\n")
        pm = PolitenessManager(user_agent="OtherBot", default_delay=0.0)
        pm.register_robots_txt("example.com", robots)
        self.assertFalse(pm.is_allowed("example.com",
                                       "https://example.com/private/x"))

    def test_ua_match_is_case_insensitive(self):
        robots = ("User-agent: nexussearchbot\n"
                  "Disallow: /\n")
        self.assertFalse(self.allowed(robots, "/x"))

    def test_crawl_delay_from_matched_group(self):
        robots = ("User-agent: *\n"
                  "Crawl-delay: 9\n"
                  f"User-agent: {UA}\n"
                  "Crawl-delay: 3\n")
        pm = self._pm(robots)
        self.assertEqual(pm.crawl_delay("example.com"), 3.0)
        pm2 = PolitenessManager(user_agent="OtherBot", default_delay=0.0)
        pm2.register_robots_txt("example.com", robots)
        self.assertEqual(pm2.crawl_delay("example.com"), 9.0)

    # ---------------------------------------------------------- hygiene

    def test_comments_and_blank_lines_ignored(self):
        robots = ("# top comment\n"
                  "User-agent: *  # trailing comment\n"
                  "\n"
                  "Disallow: /private # not part of the path\n")
        self.assertFalse(self.allowed(robots, "/private/x"))
        # the trailing comment must NOT extend the pattern to "/private #
        # not part of the path" (which would change nothing here) — but
        # more importantly the pattern must be exactly "/private": a
        # prefix of "/privateX" (still blocked by prefix semantics) vs
        # "/other" (allowed) proves the pattern text itself
        self.assertTrue(self.allowed(robots, "/other"))

    def test_bom_is_ignored(self):
        robots = "﻿User-agent: *\nDisallow: /private\n"
        self.assertFalse(self.allowed(robots, "/private"))

    def test_empty_disallow_means_allow_all(self):
        robots = "User-agent: *\nDisallow:\n"
        self.assertTrue(self.allowed(robots, "/anything"))

    def test_empty_file_allows_everything(self):
        self.assertTrue(self.allowed("", "/anything"))
        self.assertTrue(self.allowed("\n\n", "/anything"))

    def test_crlf_line_endings(self):
        robots = "User-agent: *\r\nDisallow: /private\r\n"
        self.assertFalse(self.allowed(robots, "/private"))

    def test_robots_txt_itself_always_allowed(self):
        robots = "User-agent: *\nDisallow: /\n"
        self.assertTrue(self.allowed(robots, "/robots.txt"))

    def test_rule_matching_includes_query_string(self):
        # RFC 9309 §2.2.4: "if a robots.txt file includes rules keyed by
        # query strings, crawlers should consider the query string"
        robots = "User-agent: *\nDisallow: /*?print=\n"
        self.assertFalse(self.allowed(robots, "/page?print=yes"))
        self.assertTrue(self.allowed(robots, "/page?view=print"))


if __name__ == "__main__":
    unittest.main()
