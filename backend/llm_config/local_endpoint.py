"""Purely lexical loopback/RFC1918/allowlist classification of a base URL, with no DNS
resolution. Shared by research_profile.py and client_factory.py to tag local
inference without exposing a hostname in audit records.
"""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlsplit

LOCAL_ENDPOINT_ALLOWLIST_ENV = "RESEARCH_LOCAL_ENDPOINT_ALLOWLIST"

ENDPOINT_LOOPBACK = "loopback"
ENDPOINT_PRIVATE = "private"
ENDPOINT_ALLOWLISTED = "allowlisted"
ENDPOINT_REMOTE = "remote"

_LOCAL_CLASSES = frozenset({ENDPOINT_LOOPBACK, ENDPOINT_PRIVATE, ENDPOINT_ALLOWLISTED})
_LITERAL_LOOPBACK_HOSTS = frozenset({"localhost", "localhost."})
# Only RFC1918 — link-local/CGNAT intentionally stay remote
_RFC1918_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


@dataclass(frozen=True, slots=True)
class EndpointClass:
    endpoint_class: str
    scheme: str

    @property
    def local(self) -> bool:
        return self.endpoint_class in _LOCAL_CLASSES

    @property
    def redacted_base_url(self) -> str:
        if self.local:
            return f"{self.scheme}://<{self.endpoint_class}>"
        return ""


def _split_origin(base_url):
    if type(base_url) is not str or not base_url or len(base_url.encode("utf-8")) > 32768:
        return None
    try:
        parts = urlsplit(base_url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    try:
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if not host:
        return None
    return parts.scheme, host, port


def _origin_key(scheme, host, port):
    default = 443 if scheme == "https" else 80
    return f"{scheme}://{host}:{default if port is None else port}"


def _allowlisted_origins(explicit: Iterable[str]) -> frozenset:
    entries = list(explicit)
    raw = os.environ.get(LOCAL_ENDPOINT_ALLOWLIST_ENV, "")
    entries.extend(item for item in raw.split(",") if item.strip())
    keys = set()
    for entry in entries:
        split = _split_origin(entry.strip())
        if split is not None:
            keys.add(_origin_key(*split))
    return frozenset(keys)


def classify_endpoint(base_url, *, allowlist: Iterable[str] = ()) -> EndpointClass:
    split = _split_origin(base_url)
    if split is None:
        return EndpointClass(ENDPOINT_REMOTE, "")
    scheme, host, port = split
    if _origin_key(scheme, host, port) in _allowlisted_origins(allowlist):
        return EndpointClass(ENDPOINT_ALLOWLISTED, scheme)
    if host.lower() in _LITERAL_LOOPBACK_HOSTS:
        return EndpointClass(ENDPOINT_LOOPBACK, scheme)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return EndpointClass(ENDPOINT_REMOTE, scheme)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_loopback:
        return EndpointClass(ENDPOINT_LOOPBACK, scheme)
    if any(address in network for network in _RFC1918_NETWORKS):
        return EndpointClass(ENDPOINT_PRIVATE, scheme)
    return EndpointClass(ENDPOINT_REMOTE, scheme)


def is_local_endpoint(base_url, *, allowlist: Iterable[str] = ()) -> bool:
    return classify_endpoint(base_url, allowlist=allowlist).local
