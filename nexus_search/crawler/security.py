"""Security helpers for crawler URL validation."""

import ipaddress
import socket
from urllib.parse import urlsplit


def is_private_or_local_host(hostname: str) -> bool:

    if not hostname:
        return True

    hostname = hostname.strip().lower()

    if hostname == "localhost":
        return True

    if hostname.endswith(".localhost"):
        return True

    try:
        ip = ipaddress.ip_address(hostname)

        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        )

    except ValueError:
        pass

    try:

        addresses = socket.getaddrinfo(
            hostname,
            None,
        )

        for address in addresses:

            ip = ipaddress.ip_address(
                address[4][0]
            )

            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
                or ip.is_unspecified
            ):
                return True

    except socket.gaierror:
        return True

    return False


def validate_url(url: str) -> bool:

    parts = urlsplit(url)

    if parts.scheme not in {
        "http",
        "https",
    }:
        return False

    if not parts.hostname:
        return False

    return not is_private_or_local_host(
        parts.hostname
    )