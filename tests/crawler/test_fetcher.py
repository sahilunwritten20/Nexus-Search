from unittest.mock import Mock

from nexus_search.crawler.fetcher import Fetcher


def test_etag_header_is_sent():

    fetcher = Fetcher()

    response = Mock()

    response.status_code = 200

    response.text = "<html>Hello</html>"

    response.headers = {
        "content-type": "text/html",
        "ETag": '"abc123"',
    }

    fetcher.session.get = Mock(
        return_value=response
    )

    result = fetcher.fetch(
        "https://example.com",
        etag='"old123"',
    )

    headers = (
        fetcher.session.get.call_args.kwargs[
            "headers"
        ]
    )

    assert (
        headers["If-None-Match"]
        == '"old123"'
    )

    assert result.html == (
        "<html>Hello</html>"
    )

    fetcher.close()


def test_last_modified_header_is_sent():

    fetcher = Fetcher()

    response = Mock()

    response.status_code = 200

    response.text = "<html>Hello</html>"

    response.headers = {
        "content-type": "text/html",
    }

    fetcher.session.get = Mock(
        return_value=response
    )

    fetcher.fetch(
        "https://example.com",
        last_modified=(
            "Wed, 01 Jan 2025 00:00:00 GMT"
        ),
    )

    headers = (
        fetcher.session.get.call_args.kwargs[
            "headers"
        ]
    )

    assert (
        headers["If-Modified-Since"]
        == "Wed, 01 Jan 2025 00:00:00 GMT"
    )

    fetcher.close()


def test_304_is_not_modified():

    fetcher = Fetcher()

    response = Mock()

    response.status_code = 304

    response.text = ""

    response.headers = {
        "ETag": '"abc123"',
    }

    fetcher.session.get = Mock(
        return_value=response
    )

    result = fetcher.fetch(
        "https://example.com",
        etag='"abc123"',
    )

    assert result.not_modified is True

    assert result.status_code == 304

    assert result.html is None

    fetcher.close()


def test_render_js_fallback_without_playwright(caplog, monkeypatch):
    """render_js=True must degrade to the plain HTTP body when Playwright
    isn't installed — log a warning, never crash (regression: logger was
    undefined in fetcher.py). P1-7: the browser path needs NEXUS_RENDER_JS=1;
    the env is set here so this test still exercises the import-fallback."""
    import logging

    monkeypatch.setenv("NEXUS_RENDER_JS", "1")
    try:
        import playwright  # noqa: F401
        import pytest
        pytest.skip("playwright installed; fallback path not reachable here")
    except ImportError:
        pass

    fetcher = Fetcher(render_js=True)

    response = Mock()
    response.status_code = 200
    response.text = "<html>Hello</html>"
    response.headers = {"content-type": "text/html"}
    fetcher.session.get = Mock(return_value=response)

    with caplog.at_level(logging.WARNING, logger="nexus_search.crawler.fetcher"):
        result = fetcher.fetch("https://example.com")

    assert result.html == "<html>Hello</html>"  # plain body, no crash
    assert any("playwright" in r.getMessage().lower() for r in caplog.records)
    fetcher.close()


def test_render_js_browser_failure_falls_back(caplog, monkeypatch):
    """If Playwright IS present but the browser launch fails, we still fall
    back to the HTTP body (warning, not exception). P1-7: NEXUS_RENDER_JS=1
    unlocks the path."""
    import logging
    import sys
    import types

    monkeypatch.setenv("NEXUS_RENDER_JS", "1")

    boom = types.ModuleType("playwright.sync_api")

    def _raise_playwright(*a, **k):
        raise RuntimeError("no browser here")

    boom.sync_playwright = _raise_playwright
    pkg = types.ModuleType("playwright")
    pkg.sync_api = boom

    fetcher = Fetcher(render_js=True)
    response = Mock()
    response.status_code = 200
    response.text = "<html>Hello</html>"
    response.headers = {"content-type": "text/html"}
    fetcher.session.get = Mock(return_value=response)

    saved = sys.modules.get("playwright"), sys.modules.get("playwright.sync_api")
    sys.modules["playwright"] = pkg
    sys.modules["playwright.sync_api"] = boom
    try:
        with caplog.at_level(logging.WARNING, logger="nexus_search.crawler.fetcher"):
            result = fetcher.fetch("https://example.com")
    finally:
        for name, old in (("playwright", saved[0]), ("playwright.sync_api", saved[1])):
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old

    assert result.html == "<html>Hello</html>"
    assert any("render failed" in r.getMessage().lower() for r in caplog.records)
    fetcher.close()


def test_response_etag_is_returned():

    fetcher = Fetcher()

    response = Mock()

    response.status_code = 200

    response.text = "<html>Hello</html>"

    response.headers = {
        "content-type": "text/html",
        "ETag": '"new123"',
        "Last-Modified": (
            "Wed, 01 Jan 2025 00:00:00 GMT"
        ),
    }

    fetcher.session.get = Mock(
        return_value=response
    )

    result = fetcher.fetch(
        "https://example.com"
    )

    assert result.etag == '"new123"'

    assert (
        result.last_modified
        == "Wed, 01 Jan 2025 00:00:00 GMT"
    )

    fetcher.close()


# ---------------------------------------------------------------------------
# P1-7: render_js SSRF hardening
# ---------------------------------------------------------------------------

class _FakeRoute:
    def __init__(self, url):
        self.request = Mock(url=url)
        self.aborted = False
        self.continued = False

    def abort(self):
        self.aborted = True

    def continue_(self):
        self.continued = True


class _FakePage:
    """Records the route handler; plays back a final URL and content."""

    def __init__(self, final_url, content):
        self.final_url = final_url
        self._content = content
        self.route_handler = None
        self.went_to = None

    def route(self, pattern, handler):
        assert pattern == "**/*"  # EVERY subrequest must be intercepted
        self.route_handler = handler

    def goto(self, url, timeout=None, wait_until=None):
        self.went_to = url

    @property
    def url(self):
        return self.final_url

    def content(self):
        return self._content


class _FakeBrowser:
    def __init__(self, page):
        self.page = page
        self.closed = False

    def new_page(self, user_agent=None):
        return self.page

    def close(self):
        self.closed = True


class _FakeChromium:
    def __init__(self, browser):
        self._browser = browser

    def launch(self, headless=True):
        return self._browser


class _FakeSyncPlaywright:
    def __init__(self, browser):
        self.chromium = _FakeChromium(browser)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install_fake_playwright(monkeypatch, page):
    import sys
    import types

    sync_api = types.ModuleType("playwright.sync_api")
    browser = _FakeBrowser(page)
    sync_api.sync_playwright = lambda: _FakeSyncPlaywright(browser)
    pkg = types.ModuleType("playwright")
    pkg.sync_api = sync_api
    monkeypatch.setitem(sys.modules, "playwright", pkg)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    return browser


def test_render_js_gated_off_without_env(monkeypatch, caplog):
    """P1-7: render_js=True without NEXUS_RENDER_JS=1 is DISABLED — the
    browser path must be an explicit operator opt-in, and playwright must
    never even be imported."""
    import logging
    import sys

    monkeypatch.delenv("NEXUS_RENDER_JS", raising=False)
    sys.modules.pop("playwright", None)
    sys.modules.pop("playwright.sync_api", None)

    fetcher = Fetcher(render_js=True)
    assert fetcher.render_js is False  # constructor warns + disables

    response = Mock()
    response.status_code = 200
    response.text = "<html>plain body</html>"
    response.headers = {"content-type": "text/html"}
    fetcher.session.get = Mock(return_value=response)

    with caplog.at_level(logging.WARNING, logger="nexus_search.crawler.fetcher"):
        result = fetcher.fetch("https://example.com")

    assert result.html == "<html>plain body</html>"
    assert "playwright" not in sys.modules  # never imported
    assert any("NEXUS_RENDER_JS" in r.getMessage() for r in caplog.records)
    fetcher.close()


def test_render_js_never_imports_playwright_on_default_config(monkeypatch):
    """P1-7: the default config (render_js unset) never imports playwright,
    even when a fetch completes successfully."""
    import sys

    monkeypatch.delenv("NEXUS_RENDER_JS", raising=False)
    sys.modules.pop("playwright", None)
    sys.modules.pop("playwright.sync_api", None)

    fetcher = Fetcher()
    response = Mock()
    response.status_code = 200
    response.text = "<html>body</html>"
    response.headers = {"content-type": "text/html"}
    fetcher.session.get = Mock(return_value=response)
    assert fetcher.fetch("https://example.com").html == "<html>body</html>"

    assert "playwright" not in sys.modules
    fetcher.close()


def test_render_js_private_ip_subrequest_is_aborted(monkeypatch):
    """P1-7: every browser subrequest goes through validate_url — a private
    target is aborted, a public one continues."""
    from nexus_search.crawler.security import validate_url

    monkeypatch.setenv("NEXUS_RENDER_JS", "1")
    page = _FakePage("https://example.com/loaded",
                     "<html>rendered content</html>")
    _install_fake_playwright(monkeypatch, page)

    fetcher = Fetcher(render_js=True, url_validator=validate_url)
    rendered = fetcher._render_with_browser("https://example.com")
    assert rendered == "<html>rendered content</html>"
    assert page.went_to == "https://example.com"
    assert page.route_handler is not None, "page.route interception missing"

    private = _FakeRoute("http://127.0.0.1:8080/admin")
    page.route_handler(private)
    assert private.aborted and not private.continued

    private2 = _FakeRoute("http://169.254.169.254/latest/meta-data")
    page.route_handler(private2)
    assert private2.aborted and not private2.continued

    public = _FakeRoute("https://example.com/app.js")
    page.route_handler(public)
    assert public.continued and not public.aborted
    fetcher.close()


def test_render_js_final_url_is_validated(monkeypatch):
    """P1-7: a navigation that lands on a private URL (client-side redirect)
    must yield nothing — the plain HTTP body is used instead."""
    from nexus_search.crawler.security import validate_url

    monkeypatch.setenv("NEXUS_RENDER_JS", "1")
    page = _FakePage("http://192.168.0.1/compromised",
                     "<html>private content</html>")
    _install_fake_playwright(monkeypatch, page)

    fetcher = Fetcher(render_js=True, url_validator=validate_url)
    assert fetcher._render_with_browser("https://example.com") is None
    fetcher.close()