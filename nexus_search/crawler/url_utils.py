"""URL normalization utilities for the Nexus Search crawler (Phase 3)."""
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, urljoin


def normalize_url(url: str, base: str | None = None) -> str:
    """Normalize a URL for consistent frontier/dedup storage.

    - Resolves relative URLs against `base` if given.
    - Drops the fragment.
    - Lowercases scheme and host (path/query case is preserved — it can be
      meaningful on case-sensitive servers).
    - Sorts query parameters so ?a=1&b=2 and ?b=2&a=1 normalize identically.
    - Strips a trailing slash on the path, except the bare root "/".
    - Drops default ports (80 for http, 443 for https).
    """
    if base:
        url = urljoin(base, url)

    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()

    if scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[: -len(":80")]
    elif scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[: -len(":443")]

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    query_pairs = sorted(parse_qsl(parts.query, keep_blank_values=True))
    query = urlencode(query_pairs)

    return urlunsplit((scheme, netloc, path, query, ""))


def get_domain(url: str) -> str:
    """Host without port, for per-domain rate limiting and allow-list checks."""
    netloc = urlsplit(url).netloc.lower()
    return netloc.split(":")[0]
