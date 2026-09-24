"""Tests for shared/net_guard.py: the SSH egress gate refuses
loopback/link-local/metadata/reserved and encoded-bypass addresses while permitting
RFC1918, against the real DGX host.
"""

import pytest

from shared import net_guard

BLOCKED = [
    "127.0.0.1",
    "::1",
    "169.254.1.1",
    "169.254.169.254",
    "fe80::1",
    "224.0.0.1",
    "0.0.0.0",
    "240.0.0.1",
    "::ffff:127.0.0.1",
    "2002:7f00:1::",
    "2002:a9fe:a9fe::",
    "64:ff9b::7f00:1",
    "not-an-ip",
]

ALLOWED = [
    "10.33.77.11",
    "192.168.1.1",
    "172.16.0.1",
    "128.163.37.132",
    "8.8.8.8",
    "2606:4700:4700::1111",
    "2002:808:808::",
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
    monkeypatch.setattr(net_guard, "resolve_host_addresses", lambda host: [])
    with pytest.raises(net_guard.HostResolutionError):
        net_guard.assert_ssh_target_allowed("cluster.example.edu", 22)
