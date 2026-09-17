"""Literal classification of an OpenAI-compatible base URL as local inference.

Feature 088 T016 (FR-019): the fixed research profile may only admit a
local-inference endpoint when the USER configuration's ``base_url`` names a
loopback or RFC1918 host literally, or is an exact operator/caller-allowlisted
origin. Classification is purely lexical — no DNS resolution, no connection —
so a hostname that merely *resolves* to a private address is never treated as
local (DNS rebinding cannot promote a remote endpoint), and a classification
never becomes a fallback to a different provider or credential.

Only stdlib is used; this module is shared by ``research_profile`` (which may
not import the SDK/HTTP layers) and ``client_factory``.
"""
from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlsplit

# Exact origins (scheme://host[:port]) an operator may declare as local
# inference in addition to literal loopback/RFC1918 hosts.
LOCAL_ENDPOINT_ALLOWLIST_ENV = "RESEARCH_LOCAL_ENDPOINT_ALLOWLIST"

ENDPOINT_LOOPBACK = "loopback"
ENDPOINT_PRIVATE = "private"
ENDPOINT_ALLOWLISTED = "allowlisted"
ENDPOINT_REMOTE = "remote"

_LOCAL_CLASSES = frozenset({ENDPOINT_LOOPBACK, ENDPOINT_PRIVATE, ENDPOINT_ALLOWLISTED})
_LITERAL_LOOPBACK_HOSTS = frozenset({"localhost", "localhost."})
# Exactly RFC1918; link-local, CGNAT, TEST-NET and other "not global" ranges
# are NOT local inference and stay remote (fail-closed).
_RFC1918_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


@dataclass(frozen=True, slots=True)
class EndpointClass:
    """Lexical verdict for one base URL; carries no credential material."""

    endpoint_class: str
    scheme: str

    @property
    def local(self) -> bool:
        return self.endpoint_class in _LOCAL_CLASSES

    @property
    def redacted_base_url(self) -> str:
        """Audit-safe rendering: a local endpoint reveals class only, never host."""
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
    """Classify ``base_url`` without resolving or contacting anything.

    ``allowlist`` (and the ``RESEARCH_LOCAL_ENDPOINT_ALLOWLIST`` environment
    variable) name exact origins — scheme, host and port — that count as local.
    Anything unparseable or non-http(s) is ``remote`` (fail-closed).
    """
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
