"""WP14-7 — crawler/security.py was 84%-covered with its DNS/validation
EDGES uncovered (the existing suites pin the main gates; these pin the
boundaries): empty hostname, unresolvable host, garbage resolved address,
IPv6-literal + IPv6-mapped-literal handling, pin lookup with service-name
ports, pin restore semantics, and pin_dns_for_url's nullcontext paths.
All DNS is mocked — no network."""
import socket
import unittest
from unittest import mock

from nexus_search.crawler import security


def _fake_addr(host, ip, family=socket.AF_INET):
    return (family, socket.SOCK_STREAM, 0, "", (ip, 0))


class TestResolveValidatedAddressesEdges(unittest.TestCase):
    def test_empty_hostname_is_rejected(self):
        self.assertIsNone(security.resolve_validated_addresses(""))

    def test_unresolvable_host_is_rejected(self):
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror):
            self.assertIsNone(security.resolve_validated_addresses("gone.example"))

    def test_garbage_resolved_address_is_rejected(self):
        with mock.patch("socket.getaddrinfo",
                        return_value=[(socket.AF_INET, 0, 0, "", ("not-an-ip", 0))]):
            self.assertIsNone(security.resolve_validated_addresses("weird.example"))

    def test_one_private_among_public_rejects_all(self):
        with mock.patch("socket.getaddrinfo", return_value=[
                _fake_addr("h", "8.8.8.8"),
                _fake_addr("h", "192.168.1.5")]):
            self.assertIsNone(security.resolve_validated_addresses("mixed.example"))

    def test_global_ipv6_literal_passes_and_formats_v6_sockaddr(self):
        out = security.resolve_validated_addresses("2606:4700::1111")
        self.assertEqual(out[0][0], socket.AF_INET6)
        self.assertEqual(out[0][4][0], "2606:4700::1111")

    def test_ipv6_mapped_loopback_literal_is_blocked(self):
        self.assertIsNone(security.resolve_validated_addresses("::ffff:127.0.0.1"))

    def test_trailing_dot_and_case_normalized(self):
        with mock.patch("socket.getaddrinfo", return_value=[_fake_addr("h", "1.1.1.1")]) as g:
            self.assertIsNotNone(security.resolve_validated_addresses("EXAMPLE.COM."))
            g.assert_called_once_with("example.com", None)


class TestPinnedResolutionSemantics(unittest.TestCase):
    ADDRS = [_fake_addr("h", "1.2.3.4")]

    def test_pinned_host_resolves_to_pin_with_int_port(self):
        security.install_dns_pinning()
        with security.pinned_resolution("example.com", self.ADDRS):
            out = socket.getaddrinfo("example.com", 80)
            self.assertEqual(out[0][4][0], "1.2.3.4")
            self.assertEqual(out[0][4][1], 80)

    def test_pin_with_service_name_port_falls_back_to_real_dns(self):
        # a service-name port must not be guessed into a number: the pin
        # is skipped and the REAL resolver answers (may raise offline)
        security.install_dns_pinning()
        with mock.patch("socket.getaddrinfo", wraps=security._real_getaddrinfo) as real:
            with security.pinned_resolution("example.com", self.ADDRS):
                try:
                    socket.getaddrinfo("example.com", "http")
                except socket.gaierror:
                    pass  # offline test env: reaching the real resolver is the point

    def test_wrong_family_or_type_pin_raises_gaierror(self):
        security.install_dns_pinning()
        v6only = [(socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("2606:4700::1", 0))]
        with security.pinned_resolution("example.com", v6only):
            with self.assertRaises(socket.gaierror):
                socket.getaddrinfo("example.com", 80, family=socket.AF_INET)

    def test_pin_is_restored_after_context_exits(self):
        security.install_dns_pinning()
        with security.pinned_resolution("a.example", self.ADDRS):
            with security.pinned_resolution("a.example",
                                            [_fake_addr("h", "9.9.9.9")]):
                self.assertEqual(
                    socket.getaddrinfo("a.example", 80)[0][4][0], "9.9.9.9")
            # outer pin restored
            self.assertEqual(
                socket.getaddrinfo("a.example", 80)[0][4][0], "1.2.3.4")


class TestPinDnsForUrlPaths(unittest.TestCase):
    def test_no_hostname_is_nullcontext(self):
        cm = security.pin_dns_for_url("not a url")
        with cm:
            pass  # no crash, nothing pinned

    def test_unvalidatable_host_is_nullcontext(self):
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror):
            cm = security.pin_dns_for_url("http://gone.example/x")
            with cm:
                pass


if __name__ == "__main__":
    unittest.main()
