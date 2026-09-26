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


def test_render_js_fallback_without_playwright(caplog):
    """render_js=True must degrade to the plain HTTP body when Playwright
    isn't installed — log a warning, never crash (regression: logger was
    undefined in fetcher.py)."""
    import logging

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


def test_render_js_browser_failure_falls_back(caplog):
    """If Playwright IS present but the browser launch fails, we still fall
    back to the HTTP body (warning, not exception)."""
    import logging
    import sys
    import types

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