import unittest

from nexus_search.crawler.url_utils import get_domain, normalize_url


class TestNormalizeUrl(unittest.TestCase):
    def test_strips_fragment(self):
        self.assertEqual(
            normalize_url("https://example.com/page#section"),
            "https://example.com/page",
        )

    def test_sorts_query_params(self):
        a = normalize_url("https://example.com/page?b=2&a=1")
        b = normalize_url("https://example.com/page?a=1&b=2")
        self.assertEqual(a, b)

    def test_strips_trailing_slash(self):
        self.assertEqual(
            normalize_url("https://example.com/page/"),
            "https://example.com/page",
        )

    def test_keeps_root_slash(self):
        self.assertEqual(normalize_url("https://example.com/"), "https://example.com/")

    def test_lowercases_scheme_and_host_only(self):
        self.assertEqual(
            normalize_url("HTTPS://Example.COM/Page"),
            "https://example.com/Page",
        )

    def test_resolves_relative_with_base(self):
        result = normalize_url("/about", base="https://example.com/blog/post")
        self.assertEqual(result, "https://example.com/about")

    def test_drops_default_https_port(self):
        self.assertEqual(
            normalize_url("https://example.com:443/page"),
            "https://example.com/page",
        )

    def test_keeps_non_default_port(self):
        self.assertEqual(
            normalize_url("https://example.com:8443/page"),
            "https://example.com:8443/page",
        )


class TestGetDomain(unittest.TestCase):
    def test_strips_port(self):
        self.assertEqual(get_domain("https://sub.example.com:8080/x"), "sub.example.com")

    def test_lowercases(self):
        self.assertEqual(get_domain("https://Example.COM/x"), "example.com")


if __name__ == "__main__":
    unittest.main()
