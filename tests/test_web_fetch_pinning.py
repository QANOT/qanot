"""SSRF hardening tests — IP pinning, IPv6/IPv4-mapped, hostname suffixes.

These cover the rebinding-window attack the OpenClaw port closes:
- Hostname resolves to a public IP at validate time, then a private IP at
  connect time (low-TTL DNS rebinding).
- IPv4-mapped IPv6 addresses (::ffff:169.254.169.254) used to wrap private
  ranges and bypass the IPv4 check.
- IPv6 ULA (fc00::/7) and NAT64 ranges that the previous blocklist missed.
- Hostname suffix matching (*.local, *.internal, *.corp).
"""

from __future__ import annotations

import socket

import pytest

from qanot.tools.web import (
    _BLOCKED_NETWORKS,
    _PinnedResolver,
    _is_blocked_hostname,
    _is_ip_blocked,
    _resolve_safe_addrs,
    _validate_url,
    _validate_url_metadata,
)


class TestExpandedIpBlocklist:
    """IP ranges added in the OpenClaw port."""

    @pytest.mark.parametrize("ip", [
        "100.64.0.1",       # CGNAT (RFC 6598)
        "100.127.255.255",  # CGNAT high
        "0.0.0.1",          # "this network" / RFC 1122
        "255.255.255.255",  # broadcast (reserved)
    ])
    def test_blocks_extended_ipv4_ranges(self, ip):
        assert _is_ip_blocked(ip) is True

    @pytest.mark.parametrize("ip", [
        "fc00::1",          # IPv6 ULA
        "fd00::1",           # IPv6 ULA
        "fdff:ffff::1",     # IPv6 ULA high
        "64:ff9b::1.2.3.4", # NAT64 well-known
        "2001:db8::1",      # IPv6 documentation
        "ff00::1",          # IPv6 multicast
    ])
    def test_blocks_extended_ipv6_ranges(self, ip):
        assert _is_ip_blocked(ip) is True, f"{ip} should be blocked"

    @pytest.mark.parametrize("mapped", [
        "::ffff:127.0.0.1",       # loopback via IPv4-mapped
        "::ffff:10.0.0.1",        # private via IPv4-mapped
        "::ffff:169.254.169.254", # AWS metadata via IPv4-mapped
        "::ffff:192.168.1.1",     # private via IPv4-mapped
    ])
    def test_blocks_ipv4_mapped_ipv6(self, mapped):
        """::ffff:0:0/96 — the historic SSRF bypass for naive IPv4 checks."""
        assert _is_ip_blocked(mapped) is True, f"{mapped} should unwrap to private IPv4"

    @pytest.mark.parametrize("ip", [
        "8.8.8.8",
        "1.1.1.1",
        "93.184.216.34",
        "2606:4700::1",  # public IPv6
    ])
    def test_allows_public_ips(self, ip):
        assert _is_ip_blocked(ip) is False


class TestHostnameSuffixBlocking:
    """Suffix patterns added to catch *.local, *.internal, etc."""

    @pytest.mark.parametrize("hostname", [
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",
        "metadata",
        "router.local",
        "printer.local",
        "myhost.localhost",
        "service.internal",
        "api.intranet",
        "web.corp",
        "thing.lan",
        "device.home",
    ])
    def test_blocks_internal_hostnames(self, hostname):
        assert _is_blocked_hostname(hostname) is True

    @pytest.mark.parametrize("hostname", [
        "example.com",
        "google.com",
        "api.github.com",
        "subdomain.example.org",
    ])
    def test_allows_public_hostnames(self, hostname):
        assert _is_blocked_hostname(hostname) is False


class TestUrlValidationEdgeCases:
    """Validate_url should reject literal-IP URLs targeting private ranges."""

    @pytest.mark.parametrize("url", [
        "http://169.254.169.254/latest/meta-data/",  # AWS metadata literal IPv4
        "http://[::ffff:169.254.169.254]/",           # AWS metadata via IPv4-mapped IPv6
        "http://[fc00::1]/",                          # IPv6 ULA literal
        "http://10.0.0.1/admin",                      # private IPv4 literal
        "http://[::1]/",                              # IPv6 loopback literal
        "http://127.0.0.1:8080/",                     # IPv4 loopback literal
    ])
    def test_blocks_literal_private_ips(self, url):
        err = _validate_url(url)
        assert err is not None, f"Should block literal-IP URL: {url}"

    @pytest.mark.parametrize("url", [
        "http://[2606:4700::1]/",  # public IPv6 literal
        "http://8.8.8.8/dns",       # public IPv4 literal
    ])
    def test_allows_literal_public_ips(self, url):
        # Note: real DNS not needed — these are literal IPs, validated directly.
        err = _validate_url(url)
        assert err is None, f"Should allow public literal-IP URL: {url}, got: {err!r}"

    @pytest.mark.parametrize("url", [
        "http://localhost/",
        "http://router.local/",
        "http://service.internal/",
    ])
    def test_blocks_internal_hostname_urls(self, url):
        assert _validate_url(url) is not None


class TestResolveSafeAddrs:
    """_resolve_safe_addrs walks ALL DNS results, blocks if any is private."""

    def test_returns_safe_addrs_for_real_public_host(self):
        # example.com is a real domain that resolves to public IPs.
        err, addrs = _resolve_safe_addrs("example.com")
        assert err is None
        assert addrs is not None
        assert len(addrs) > 0
        for entry in addrs:
            assert entry["hostname"] == "example.com"
            assert "host" in entry
            assert "family" in entry
            assert _is_ip_blocked(entry["host"]) is False

    def test_returns_error_for_unresolvable_host(self, monkeypatch):
        """Some ISPs hijack NXDOMAIN with a search page; mock to be deterministic."""

        def fake_getaddrinfo(*args, **kwargs):
            raise socket.gaierror(-2, "Name or service not known")

        monkeypatch.setattr("qanot.tools.web.socket.getaddrinfo", fake_getaddrinfo)
        err, addrs = _resolve_safe_addrs("does-not-exist.invalid")
        assert err is not None
        assert "DNS" in err
        assert addrs is None

    def test_returns_error_when_any_address_is_private(self, monkeypatch):
        """Simulate a hostname that resolves to a mix of public and private —
        we MUST reject (the rebinding window). Even one private answer fails."""

        def fake_getaddrinfo(host, port, family, socktype):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", 0)),
            ]

        monkeypatch.setattr("qanot.tools.web.socket.getaddrinfo", fake_getaddrinfo)
        err, addrs = _resolve_safe_addrs("hostile.example.com")
        assert err is not None
        assert "blocked" in err.lower()
        assert addrs is None


class TestPinnedResolver:
    """_PinnedResolver returns only pre-validated IPs; refuses unknown hosts."""

    @pytest.mark.asyncio
    async def test_returns_pinned_addrs_for_known_host(self):
        pin = {
            "example.com": [
                {"hostname": "example.com", "host": "93.184.216.34", "port": 0,
                 "family": socket.AF_INET, "proto": 0, "flags": 0}
            ]
        }
        resolver = _PinnedResolver(pin)
        results = await resolver.resolve("example.com", port=443, family=socket.AF_INET)
        assert len(results) == 1
        assert results[0]["host"] == "93.184.216.34"
        assert results[0]["port"] == 443

    @pytest.mark.asyncio
    async def test_refuses_unpinned_host(self):
        resolver = _PinnedResolver({"safe.example.com": []})
        with pytest.raises(OSError, match="not pre-pinned"):
            await resolver.resolve("attacker.example.com", port=443)

    @pytest.mark.asyncio
    async def test_refuses_when_no_family_match(self):
        pin = {
            "example.com": [
                {"hostname": "example.com", "host": "93.184.216.34", "port": 0,
                 "family": socket.AF_INET, "proto": 0, "flags": 0}
            ]
        }
        resolver = _PinnedResolver(pin)
        # Only IPv4 pinned; ask for IPv6 only — should fail
        with pytest.raises(OSError, match="no pinned address"):
            await resolver.resolve("example.com", port=443, family=socket.AF_INET6)

    @pytest.mark.asyncio
    async def test_unspec_family_returns_all(self):
        pin = {
            "example.com": [
                {"hostname": "example.com", "host": "93.184.216.34", "port": 0,
                 "family": socket.AF_INET, "proto": 0, "flags": 0},
                {"hostname": "example.com", "host": "2606:4700::1", "port": 0,
                 "family": socket.AF_INET6, "proto": 0, "flags": 0},
            ]
        }
        resolver = _PinnedResolver(pin)
        results = await resolver.resolve(
            "example.com", port=443, family=socket.AF_UNSPEC
        )
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_case_insensitive_hostname(self):
        pin = {
            "example.com": [
                {"hostname": "example.com", "host": "1.2.3.4", "port": 0,
                 "family": socket.AF_INET, "proto": 0, "flags": 0}
            ]
        }
        resolver = _PinnedResolver(pin)
        results = await resolver.resolve("EXAMPLE.COM", port=80)
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_close_does_not_raise(self):
        resolver = _PinnedResolver({})
        await resolver.close()  # Should be a no-op

    @pytest.mark.asyncio
    async def test_add_extends_pin(self):
        resolver = _PinnedResolver({})
        resolver.add(
            "later.example.com",
            [{"hostname": "later.example.com", "host": "1.2.3.4", "port": 0,
              "family": socket.AF_INET, "proto": 0, "flags": 0}],
        )
        results = await resolver.resolve("later.example.com", port=80)
        assert len(results) == 1


class TestValidateUrlMetadata:
    """The split metadata-only validator (no DNS)."""

    def test_blocks_invalid_scheme(self):
        err, _ = _validate_url_metadata("ftp://example.com")
        assert err is not None
        assert "scheme" in err.lower()

    def test_blocks_no_hostname(self):
        err, _ = _validate_url_metadata("http://")
        assert err is not None

    def test_blocks_blocked_port(self):
        err, _ = _validate_url_metadata("http://example.com:6379/")  # Redis
        assert err is not None
        assert "port" in err.lower()

    def test_blocks_internal_hostname_suffix(self):
        err, _ = _validate_url_metadata("http://api.local/data")
        assert err is not None

    def test_returns_hostname_on_success(self):
        err, hostname = _validate_url_metadata("https://example.com:443/path")
        assert err is None
        assert hostname == "example.com"

    def test_blocks_literal_private_ip(self):
        err, _ = _validate_url_metadata("http://10.0.0.1/")
        assert err is not None


class TestNetworksConstantStillExported:
    """Backward-compat: existing tests import _BLOCKED_NETWORKS."""

    def test_blocked_networks_includes_legacy_entries(self):
        # Ensure original entries still present after expansion
        from ipaddress import ip_network
        legacy = [
            ip_network("127.0.0.0/8"),
            ip_network("10.0.0.0/8"),
            ip_network("172.16.0.0/12"),
            ip_network("192.168.0.0/16"),
            ip_network("169.254.0.0/16"),
        ]
        for n in legacy:
            assert n in _BLOCKED_NETWORKS
