"""Connection-time network guard for the remote-compute agents (feature 063).

This is the product's first NON-HTTP outbound path (SSH), which the HTTP egress
guard in ``shared/external_http.py`` does not and cannot cover (it scheme-locks to
http/https). This module provides a scheme-neutral, stdlib-only target guard.

Policy difference vs. ``external_http`` (deliberate, see spec research R5):
``external_http._is_private_address`` rejects **all** RFC1918 (``ip.is_private``),
which is correct for arbitrary user-supplied web URLs but WRONG here — legitimate
on-prem clusters live in RFC1918 (``dgx.ai.uky.edu`` resolves to both a public
address and ``10.33.77.11``). This guard therefore refuses loopback, link-local
(including the ``169.254.169.254`` cloud-metadata address), multicast, unspecified,
and reserved addresses, but **permits** RFC1918. Reaching an RFC1918 host is gated
by four other controls that must all hold before a byte is sent: the target must be
in the invoking user's own inventory, address/port come from the stored record, the
recorded host key must match, and SSH auth must succeed (spec FR-018/FR-019/FR-020).

Kept pure-stdlib on purpose: it runs on the connection hot path and is imported by
the transport, so it must not drag ``requests`` or any heavy dependency, and it must
be unit-testable with no network.
"""
from __future__ import annotations

import ipaddress
import socket
from typing import List

__all__ = [
    "NetGuardError",
    "HostResolutionError",
    "BlockedTargetError",
    "is_blocked_ssh_address",
    "resolve_host_addresses",
    "assert_ssh_target_allowed",
]


class NetGuardError(Exception):
    """Base class for connection-guard failures."""


class HostResolutionError(NetGuardError):
    """DNS resolution failed for the target host."""


class BlockedTargetError(NetGuardError):
    """The target resolves to (or is) an address SSH is not permitted to reach."""

    def __init__(self, host: str, address: str, reason: str) -> None:
        self.host = host
        self.address = address
        self.reason = reason
        super().__init__(f"SSH target '{host}' -> {address} refused: {reason}")


# IPv6 encodings that embed an IPv4 address. If we don't decode these, an address
# like 2002:7f00:1:: (6to4 for 127.0.0.1) reports is_loopback == False and slips
# past the check — the same SSRF-bypass class as ``::ffff:127.0.0.1``.
_SIXTOFOUR = ipaddress.ip_network("2002::/16")      # RFC 3056 6to4 (deprecated, RFC 7526)
_NAT64_WKP = ipaddress.ip_network("64:ff9b::/96")   # RFC 6052 NAT64 well-known prefix


def _effective_ip(addr: str) -> ipaddress._BaseAddress:
    """Parse ``addr`` and collapse any IPv4-in-IPv6 encoding to its embedded IPv4.

    Handles IPv4-mapped (``::ffff:a.b.c.d``), 6to4 (``2002:AABB:CCDD::``), and the
    NAT64 well-known prefix (``64:ff9b::a.b.c.d``). Each would otherwise evade the
    loopback/link-local/metadata/reserved checks by wrapping a blocked IPv4 in a
    superficially-"global" IPv6 literal — a classic SSRF bypass. Evaluate the
    embedded IPv4 instead.
    """
    ip = ipaddress.ip_address(addr)
    if ip.version == 6:
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            return mapped
        if ip in _SIXTOFOUR:
            return ipaddress.IPv4Address((int(ip) >> 80) & 0xFFFFFFFF)
        if ip in _NAT64_WKP:
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


def is_blocked_ssh_address(addr: str) -> bool:
    """Return True if ``addr`` (an IP literal) must not be reached over SSH.

    Blocks loopback, link-local (incl. ``169.254.169.254`` metadata via
    ``is_link_local``), multicast, unspecified, and reserved. **Permits** RFC1918
    private ranges (on-prem clusters). An unparseable literal is blocked
    (fail-closed).
    """
    try:
        ip = _effective_ip(addr)
    except ValueError:
        return True
    return bool(
        ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    )


def resolve_host_addresses(host: str) -> List[str]:
    """Return every unique address (IPv4 + IPv6) ``host`` resolves to.

    Mirrors ``external_http._resolve_host_addresses`` (resolve-all-records, the
    anti-DNS-rebinding step) but stays stdlib-only. Raises ``HostResolutionError``
    on DNS failure. An IP literal resolves to itself without a network round trip.
    """
    try:
        info = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise HostResolutionError(f"DNS resolution failed for '{host}': {e}") from e
    seen: List[str] = []
    for *_head, sockaddr in info:
        addr = sockaddr[0]
        if addr not in seen:
            seen.append(addr)
    return seen


def assert_ssh_target_allowed(host: str, port: int) -> List[str]:
    """Validate an SSH target at connection time and return its resolved addresses.

    Resolves **all** A/AAAA records and refuses if **any** is blocked (so a name
    that resolves to a mix of public and blocked addresses is refused — anti
    rebinding). Callers should invoke this immediately before connecting so a name
    that resolves differently after registration cannot redirect the connection
    (FR-019). Raises ``BlockedTargetError`` / ``HostResolutionError``.
    """
    if not isinstance(port, int) or not (1 <= port <= 65535):
        raise BlockedTargetError(str(host), str(port), f"invalid port {port!r}")
    addresses = resolve_host_addresses(host)
    if not addresses:
        raise HostResolutionError(f"no addresses resolved for '{host}'")
    for addr in addresses:
        if is_blocked_ssh_address(addr):
            raise BlockedTargetError(host, addr, "loopback/link-local/metadata/reserved not permitted")
    return addresses
