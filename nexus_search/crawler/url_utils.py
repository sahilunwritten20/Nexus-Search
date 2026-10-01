"""URL normalization utilities for the Nexus Search crawler (Phase 3)."""
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, urljoin

# BUG-02: percent-escapes of UNRESERVED characters (RFC 3986: ALPHA / DIGIT
# / "-" / "." / "_" / "~") are equivalent to their decoded form, so
# /p%61ge and /page are the same node. Reserved escapes (%2F, %26, %3D ...)
# are left untouched — decoding those changes meaning.
_PCT_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def _decode_unreserved(s: str) -> str:
    if "%" not in s:
        return s
    def repl(match):
        ch = chr(int(match.group(1), 16))
        return ch if ch in _UNRESERVED else match.group(0)
    return _PCT_ESCAPE.sub(repl, s)


def normalize_url(url: str, base: str | None = None) -> str:
    """Normalize a URL for consistent frontier/dedup storage.

    - Resolves relative URLs against `base` if given.
    - Drops the fragment.
    - Lowercases scheme and host (path/query case is preserved — it can be
      meaningful on case-sensitive servers).
    - Decodes percent-escapes of unreserved characters (BUG-02: /p%61ge and
      /page are the same node; reserved escapes are never decoded).
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

    path = _decode_unreserved(parts.path or "/")
    if len(path) > 1 and path.endswith("/"):
        # "//" must not collapse to "" — the root path is "/", always
        path = path.rstrip("/") or "/"

    # Tracking params are pure noise — they change nothing server-side but
    # would each queue a "new" URL otherwise (post-fetch hash dedup was the
    # only guard, after paying for the fetch). Strip them at canonicalization.
    query_pairs = [
        (k, v) for k, v in parse_qsl(_decode_unreserved(parts.query),
                                     keep_blank_values=True)
        if not _is_tracking_param(k)
    ]
    query_pairs.sort()
    query = urlencode(query_pairs)

    return urlunsplit((scheme, netloc, path, query, ""))


_TRACKING_PREFIXES = ("utm_",)
_TRACKING_EXACT = {"gclid", "fbclid", "dclid", "msclkid", "mc_cid", "mc_eid",
                   "igshid", "ref", "spm", "yclid", "wickedid", "ttclid"}


def _is_tracking_param(name: str) -> bool:
    n = name.lower()
    return n.startswith(_TRACKING_PREFIXES) or n in _TRACKING_EXACT


def get_domain(url: str) -> str:
    """Host without port, for per-domain rate limiting and allow-list checks."""
    return urlsplit(url).hostname or ""