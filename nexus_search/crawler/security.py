"""Security helpers for crawler URL validation (SSRF protection)."""

import contextlib
import ipaddress
import socket
import threading
from urllib.parse import urlsplit


def _blocked_ip(ip) -> bool:
    mapped = getattr(ip, "ipv4_mapped", None)  # ::ffff:127.0.0.1 -> 127.0.0.1
    if mapped is not None:
        ip = mapped
    return (
        not ip.is_global  # private, loopback, link-local, CGNAT, reserved, ...
        or ip.is_multicast
        or ip.is_unspecified
    )


def _normalize_host(hostname: str) -> str:
    return hostname.strip().lower().rstrip(".")


def resolve_validated_addresses(hostname: str):
    """Resolve `hostname` and return its raw `getaddrinfo` result if every
    resolved address is public, else `None`. This is the ONE place DNS
    resolution happens for validation, so the exact addresses checked here
    can also be pinned for the actual connection (see `pinned_resolution`)
    instead of being re-resolved later.
    """
    if not hostname:
        return None
    hostname = _normalize_host(hostname)
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return None

    try:  # literal IP address
        ip = ipaddress.ip_address(hostname)
        if _blocked_ip(ip):
            return None
        family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
        sockaddr = (hostname, 0, 0, 0) if ip.version == 6 else (hostname, 0)
        return [(family, socket.SOCK_STREAM, 0, "", sockaddr)]
    except ValueError:
        pass

    try:  # hostname: EVERY resolved address must be public
        addresses = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return None
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address[4][0].split("%")[0])
        except ValueError:
            return None
        if _blocked_ip(ip):
            return None
    return addresses


def is_private_or_local_host(hostname: str) -> bool:
    return resolve_validated_addresses(hostname) is None


def validate_url(url: str) -> bool:
    """True only for http(s) URLs whose host resolves exclusively to public IPs."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return False
    return not is_private_or_local_host(parts.hostname)


# ---------------------------------------------------------------------------
# DNS-rebinding protection.
#
# validate_url resolves a hostname once to confirm it's public, but the
# HTTP client then resolves the SAME hostname AGAIN to actually connect.
# An attacker's DNS server can return a public IP for lookup #1 (passing
# validation) and a private IP for lookup #2 (the one actually connected
# to). The fix: pin the already-validated addresses so any further
# getaddrinfo() call for that exact host, for the lifetime of the
# connection attempt, returns those same addresses instead of asking DNS
# again.
# ---------------------------------------------------------------------------

_tls = threading.local()
_real_getaddrinfo = socket.getaddrinfo


def _pinned_getaddrinfo(host, port=None, family=0, type=0, proto=0, flags=0):
    pins = getattr(_tls, "pins", None)
    if pins and isinstance(host, str):
        pinned = pins.get(_normalize_host(host))
        if pinned is not None:
            try:
                port_num = int(port) if port is not None else 0
            except (TypeError, ValueError):  # service name like "http": don't guess
                return _real_getaddrinfo(host, port, family, type, proto, flags)
            result = []
            for fam, stype, prot, canon, sa in pinned:
                if (family and fam != family) or (type and stype != type):
                    continue
                entry = (fam, stype, prot, canon, (sa[0], port_num) + tuple(sa[2:]))
                if entry not in result:
                    result.append(entry)
            if not result:
                raise socket.gaierror(
                    socket.EAI_NONAME, "no pinned address for requested family/type"
                )
            return result
    return _real_getaddrinfo(host, port, family, type, proto, flags)


# Installed once, process-wide. Safe: it only changes behavior for a
# hostname the CURRENT thread has explicitly pinned (thread-local), so
# concurrent crawler workers never affect each other's DNS lookups.
socket.getaddrinfo = _pinned_getaddrinfo


@contextlib.contextmanager
def pinned_resolution(hostname: str, addresses):
    """Force getaddrinfo(hostname, ...) to return `addresses`, for the
    current thread only, for the duration of this block."""
    key = _normalize_host(hostname)
    pins = getattr(_tls, "pins", None)
    if pins is None:
        pins = _tls.pins = {}
    previous = pins.get(key)
    pins[key] = addresses
    try:
        yield
    finally:
        if previous is None:
            pins.pop(key, None)
        else:
            pins[key] = previous


def pin_dns_for_url(url: str):
    """Context manager: pins DNS for url's host to the addresses just
    validated as public. No-op if the host can't be validated (that's
    validate_url's job to block, not this function's)."""
    hostname = urlsplit(url).hostname
    if not hostname:
        return contextlib.nullcontext()
    addresses = resolve_validated_addresses(hostname)
    if addresses is None:
        return contextlib.nullcontext()
    return pinned_resolution(hostname, addresses)