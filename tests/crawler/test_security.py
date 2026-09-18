from nexus_search.crawler.security import validate_url


def test_public_http_url():

    assert (
        validate_url(
            "https://example.com"
        )
        is True
    )


def test_localhost_is_blocked():

    assert (
        validate_url(
            "http://localhost"
        )
        is False
    )


def test_loopback_ip_is_blocked():

    assert (
        validate_url(
            "http://127.0.0.1"
        )
        is False
    )


def test_private_ip_is_blocked():

    assert (
        validate_url(
            "http://192.168.1.1"
        )
        is False
    )


def test_file_scheme_is_blocked():

    assert (
        validate_url(
            "file:///etc/passwd"
        )
        is False
    )


def test_missing_hostname_is_blocked():

    assert (
        validate_url(
            "http://"
        )
        is False
    )