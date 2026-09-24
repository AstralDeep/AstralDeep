"""Stdlib-only, scheme-neutral connection-time guard for the remote-compute SSH agents:
blocks loopback/link-local/reserved addresses but permits RFC1918, unlike
shared/external_http.py, since on-prem clusters live there.
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
    pass


class HostResolutionError(NetGuardError):
    pass


class BlockedTargetError(NetGuardError):
    def __init__(self, host: str, address: str, reason: str) -> None:
        self.host = host
        self.address = address
        self.reason = reason
        super().__init__(f"SSH target '{host}' -> {address} refused: {reason}")


# Undecoded, 6to4 addresses (e.g. loopback) look global
_SIXTOFOUR = ipaddress.ip_network("2002::/16")
_NAT64_WKP = ipaddress.ip_network("64:ff9b::/96")


def _effective_ip(addr: str) -> ipaddress._BaseAddress:
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
    if not isinstance(port, int) or not (1 <= port <= 65535):
        raise BlockedTargetError(str(host), str(port), f"invalid port {port!r}")
    addresses = resolve_host_addresses(host)
    if not addresses:
        raise HostResolutionError(f"no addresses resolved for '{host}'")
    for addr in addresses:
        if is_blocked_ssh_address(addr):
            raise BlockedTargetError(host, addr, "loopback/link-local/metadata/reserved not permitted")
    return addresses
