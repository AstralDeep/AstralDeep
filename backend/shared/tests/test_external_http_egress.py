"""Unit tests for shared.external_http SSRF / egress guard."""
import socket
from unittest.mock import patch

import pytest

from shared.external_http import (
    EgressBlockedError,
    _is_private_address,
    validate_egress_url,
)


def _fake_resolve(host_to_addr):
    """Return a stand-in for socket.getaddrinfo that maps hosts to a fixed IP."""
    def _resolver(host, *args, **kwargs):
        if host not in host_to_addr:
            raise socket.gaierror(f"unknown host: {host}")
        addr = host_to_addr[host]
        family = socket.AF_INET6 if ":" in addr else socket.AF_INET
        sockaddr = (addr, 0, 0, 0) if family == socket.AF_INET6 else (addr, 0)
        return [(family, socket.SOCK_STREAM, 6, "", sockaddr)]
    return _resolver


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/",
        "file:///etc/passwd",
        "gopher://example.com/",
        "javascript:alert(1)",
    ],
)
def test_rejects_non_http_schemes(url: str) -> None:
    with pytest.raises(EgressBlockedError):
        validate_egress_url(url)


def test_rejects_loopback_v4() -> None:
    with patch("socket.getaddrinfo", _fake_resolve({"localhost": "127.0.0.1"})):
        with pytest.raises(EgressBlockedError, match="loopback|private"):
            validate_egress_url("http://localhost:8080/")


def test_rejects_loopback_v6() -> None:
    with patch("socket.getaddrinfo", _fake_resolve({"localhost": "::1"})):
        with pytest.raises(EgressBlockedError):
            validate_egress_url("http://localhost/")


@pytest.mark.parametrize("addr", ["10.0.0.5", "172.16.42.1", "192.168.1.7"])
def test_rejects_rfc1918(addr: str) -> None:
    with patch("socket.getaddrinfo", _fake_resolve({"internal.local": addr})):
        with pytest.raises(EgressBlockedError):
            validate_egress_url("http://internal.local/")


def test_rejects_link_local_metadata() -> None:
    with patch("socket.getaddrinfo", _fake_resolve({"metadata": "169.254.169.254"})):
        with pytest.raises(EgressBlockedError):
            validate_egress_url("http://metadata/")


def test_allows_public_address() -> None:
    with patch("socket.getaddrinfo", _fake_resolve({"public.example.com": "8.8.8.8"})):
        validate_egress_url("https://public.example.com/")  # should not raise


def test_allow_list_overrides_block() -> None:
    with patch("socket.getaddrinfo", _fake_resolve({"internal.local": "10.0.0.5"})):
        validate_egress_url(
            "http://internal.local/",
            allowed_private_hosts=["internal.local"],
        )  # should not raise


def test_dns_failure_blocks_egress() -> None:
    def _failing(*_a, **_kw):
        raise socket.gaierror("Name or service not known")
    with patch("socket.getaddrinfo", _failing):
        with pytest.raises(EgressBlockedError):
            validate_egress_url("https://does-not-resolve.example/")


def test_url_without_host_is_blocked() -> None:
    with pytest.raises(EgressBlockedError):
        validate_egress_url("https:///path-only")


# ---------------------------------------------------------------------------
# IPv4-in-IPv6 wrappers (the classic SSRF-bypass class net_guard already names)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "addr",
    [
        "::ffff:127.0.0.1",        # IPv4-mapped loopback
        "::ffff:169.254.169.254",  # IPv4-mapped cloud metadata
        "::ffff:10.0.0.5",         # IPv4-mapped RFC1918
        "2002:7f00:0001::",        # 6to4 wrapping 127.0.0.1
        "2002:a9fe:a9fe::",        # 6to4 wrapping 169.254.169.254
        "64:ff9b::7f00:1",         # NAT64 well-known prefix wrapping 127.0.0.1
        "64:ff9b::a9fe:a9fe",      # NAT64 wrapping the metadata address
    ],
)
def test_rejects_ipv4_embedded_in_ipv6(addr: str) -> None:
    with patch("socket.getaddrinfo", _fake_resolve({"wrapped.example": addr})):
        with pytest.raises(EgressBlockedError):
            validate_egress_url("http://wrapped.example/")


@pytest.mark.parametrize(
    "addr,embedded",
    [
        ("::ffff:127.0.0.1", "127.0.0.1"),
        ("::ffff:169.254.169.254", "169.254.169.254"),
        ("2002:7f00:0001::", "127.0.0.1"),
        ("2002:a9fe:a9fe::", "169.254.169.254"),
        ("64:ff9b::7f00:1", "127.0.0.1"),
    ],
)
def test_guard_decodes_the_embedded_ipv4_itself(addr: str, embedded: str) -> None:
    """The block must come from OUR decode, not from the stdlib happening to
    classify the wrapper. CPython only classifies these correctly from 3.11.10
    (post CVE-2024-4032), so pin the decode explicitly — a base-image downgrade
    then fails CI instead of silently reopening the bypass."""
    from shared import external_http

    assert str(external_http._decode_embedded_ipv4(addr)) == embedded
    assert external_http._is_private_address(addr) is True


@pytest.mark.parametrize("addr", ["64:ff9b::8.8.8.8", "2002:0808:0808::"])
def test_decoding_never_widens_the_guard(addr: str) -> None:
    """A wrapper around a PUBLIC IPv4 is still a non-global IPv6 literal in its
    own right, so it stays blocked — decoding may only ever tighten."""
    assert _is_private_address(addr) is True


@pytest.mark.parametrize("addr", ["8.8.8.8", "2606:4700:4700::1111", "::ffff:8.8.8.8"])
def test_public_addresses_still_allowed(addr: str) -> None:
    assert _is_private_address(addr) is False
