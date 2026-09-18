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