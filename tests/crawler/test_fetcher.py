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
    the env is set here so this test still exercises the import-fallback.
    WP12-B7: render_js with NO url_validator needs the explicit
    render_js_allow_private=True opt-in so this fallback scenario stays
    reachable (assertions unchanged)."""
    import logging

    monkeypatch.setenv("NEXUS_RENDER_JS", "1")
    try:
        import playwright  # noqa: F401
        import pytest
        pytest.skip("playwright installed; fallback path not reachable here")
    except ImportError:
        pass

    fetcher = Fetcher(render_js=True, render_js_allow_private=True)

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
    unlocks the path. WP12-B7: render_js_allow_private=True keeps this
    unvalidated-browser scenario testable (assertions unchanged)."""
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

    fetcher = Fetcher(render_js=True, render_js_allow_private=True)
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
    """Records the route handlers; plays back a final URL and content."""

    def __init__(self, final_url, content):
        self.final_url = final_url
        self._content = content
        self.route_handler = None
        self.ws_route_handler = None
        self.supports_websocket_routing = False
        self.went_to = None

    def route(self, pattern, handler):
        assert pattern == "**/*"  # EVERY subrequest must be intercepted
        self.route_handler = handler

    def route_web_socket(self, pattern, handler):
        assert pattern == "**/*"
        self.ws_route_handler = handler

    def goto(self, url, timeout=None, wait_until=None):
        self.went_to = url

    @property
    def url(self):
        return self.final_url

    def content(self):
        return self._content


class _FakeContext:
    def __init__(self, page):
        self.page = page

    def new_page(self, user_agent=None):
        return self.page


class _FakeBrowser:
    def __init__(self, page):
        self.page = page
        self.closed = False
        self.context_kwargs = {}

    def new_context(self, **kwargs):
        self.context_kwargs = dict(kwargs)
        return _FakeContext(self.page)

    def new_page(self, user_agent=None):
        # legacy path (pre-B7 direct page creation), kept for old callers
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


def test_render_js_refused_without_validator(monkeypatch, caplog):
    """WP12-B7 (audit R7a): render_js=True with url_validator=None validates
    NOTHING (the route handler and final-URL check both no-op). It must be
    refused with a warning unless the operator explicitly opts into the
    unvalidated path with render_js_allow_private=True."""
    import logging
    import sys

    monkeypatch.setenv("NEXUS_RENDER_JS", "1")
    sys.modules.pop("playwright", None)
    sys.modules.pop("playwright.sync_api", None)

    with caplog.at_level(logging.WARNING, logger="nexus_search.crawler.fetcher"):
        fetcher = Fetcher(render_js=True, url_validator=None)
    assert fetcher.render_js is False
    assert any("url_validator" in r.getMessage() for r in caplog.records)

    # the explicit private-allow flag keeps the browser path armed
    fetcher2 = Fetcher(render_js=True, url_validator=None,
                       render_js_allow_private=True)
    assert fetcher2.render_js is True
    fetcher.close()
    fetcher2.close()


def test_render_js_service_workers_blocked(monkeypatch):
    """WP12-B7 (audit R7b): the browser context is created with
    service_workers="block" — page.route cannot see SW fetches, so they
    must be disabled at the context level."""
    from nexus_search.crawler.security import validate_url

    monkeypatch.setenv("NEXUS_RENDER_JS", "1")
    page = _FakePage("https://example.com/loaded", "<html>x</html>")
    browser = _install_fake_playwright(monkeypatch, page)

    fetcher = Fetcher(render_js=True, url_validator=validate_url)
    assert fetcher._render_with_browser("https://example.com") == "<html>x</html>"
    assert browser.context_kwargs.get("service_workers") == "block"
    fetcher.close()


def test_render_js_websockets_blocked_when_supported(monkeypatch):
    """WP12-B7 (audit R7b): WebSocket upgrades bypass page.route; when the
    playwright build offers route_web_socket it must be wired with the same
    validator (private WS aborted, public continued)."""
    from nexus_search.crawler.security import validate_url

    monkeypatch.setenv("NEXUS_RENDER_JS", "1")

    class _FakeWebSocketRoute:
        def __init__(self, url):
            self.url = url
            self.aborted = False
            self.continued = False

        def abort(self):
            self.aborted = True

        def continue_(self):
            self.continued = True

    page = _FakePage("https://example.com/loaded", "<html>x</html>")
    page.supports_websocket_routing = True
    _install_fake_playwright(monkeypatch, page)

    fetcher = Fetcher(render_js=True, url_validator=validate_url)
    assert fetcher._render_with_browser("https://example.com") == "<html>x</html>"
    assert page.ws_route_handler is not None

    ws_private = _FakeWebSocketRoute("ws://127.0.0.1:9000/socket")
    page.ws_route_handler(ws_private)
    assert ws_private.aborted and not ws_private.continued

    ws_public = _FakeWebSocketRoute("wss://example.com/live")
    page.ws_route_handler(ws_public)
    assert ws_public.continued and not ws_public.aborted
    fetcher.close()


def test_render_js_websocket_absence_is_not_fatal(monkeypatch):
    """WP12-B7: an older playwright build without route_web_socket must
    still render (residual WS risk is documented, not a crash)."""
    from nexus_search.crawler.security import validate_url

    monkeypatch.setenv("NEXUS_RENDER_JS", "1")
    page = _FakePage("https://example.com/loaded", "<html>x</html>")
    # default fake: no supports_websocket_routing flag
    _install_fake_playwright(monkeypatch, page)

    fetcher = Fetcher(render_js=True, url_validator=validate_url)
    assert fetcher._render_with_browser("https://example.com") == "<html>x</html>"
    fetcher.close()