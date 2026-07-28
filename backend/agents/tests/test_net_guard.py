"""Unit tests for the SSH connection-time egress gate (feature 063, shared/net_guard.py).

Covers FR-019: refuse loopback/link-local/metadata/reserved, PERMIT RFC1918 (on-prem
clusters — the DGX resolves to 10.33.77.11). Stdlib-only; no network (IP literals
resolve to themselves via getaddrinfo).
"""
import pytest

from shared import net_guard

# Must be refused.
BLOCKED = [
    "127.0.0.1",            # loopback v4
    "::1",                  # loopback v6
    "169.254.1.1",          # link-local v4
    "169.254.169.254",      # cloud metadata (link-local)
    "fe80::1",              # link-local v6
    "224.0.0.1",            # multicast
    "0.0.0.0",              # unspecified
    "240.0.0.1",            # reserved (class E)
    "::ffff:127.0.0.1",     # IPv4-mapped loopback (SSRF-bypass hardening)
    "2002:7f00:1::",        # 6to4 wrapping 127.0.0.1 (loopback)
    "2002:a9fe:a9fe::",     # 6to4 wrapping 169.254.169.254 (metadata)
    "64:ff9b::7f00:1",      # NAT64 well-known prefix wrapping 127.0.0.1
    "not-an-ip",            # unparseable => fail-closed
]

# Must be permitted (RFC1918 on-prem + public).
ALLOWED = [
    "10.33.77.11",          # the real DGX private address
    "192.168.1.1",
    "172.16.0.1",
    "128.163.37.132",       # the real DGX public address
    "8.8.8.8",
    "2606:4700:4700::1111",  # public IPv6
    "2002:808:808::",       # 6to4 wrapping 8.8.8.8 (public) — proves the fix is surgical
]


@pytest.mark.parametrize("addr", BLOCKED)
def test_blocked_addresses(addr):
    assert net_guard.is_blocked_ssh_address(addr) is True


@pytest.mark.parametrize("addr", ALLOWED)
def test_allowed_addresses(addr):
    assert net_guard.is_blocked_ssh_address(addr) is False


def test_assert_permits_rfc1918_literal():
    assert net_guard.assert_ssh_target_allowed("10.33.77.11", 22) == ["10.33.77.11"]


def test_assert_refuses_loopback_literal():
    with pytest.raises(net_guard.BlockedTargetError):
        net_guard.assert_ssh_target_allowed("127.0.0.1", 22)


def test_assert_refuses_metadata_literal():
    with pytest.raises(net_guard.BlockedTargetError):
        net_guard.assert_ssh_target_allowed("169.254.169.254", 22)


@pytest.mark.parametrize("port", [0, -1, 70000, "22", None])
def test_assert_rejects_bad_port(port):
    with pytest.raises(net_guard.BlockedTargetError):
        net_guard.assert_ssh_target_allowed("10.0.0.1", port)


def test_assert_raises_on_unresolvable():
    with pytest.raises(net_guard.HostResolutionError):
        net_guard.assert_ssh_target_allowed("no.such.host.invalid.", 22)


def test_assert_raises_when_the_resolver_answers_with_no_records(monkeypatch):
    # A successful lookup that yields zero addresses leaves nothing to check
    # against the block list, so it is a resolution failure — never an open target.
    monkeypatch.setattr(net_guard, "resolve_host_addresses", lambda host: [])
    with pytest.raises(net_guard.HostResolutionError):
        net_guard.assert_ssh_target_allowed("cluster.example.edu", 22)
