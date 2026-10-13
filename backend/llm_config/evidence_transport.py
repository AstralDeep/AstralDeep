"""Confines context-enabled SDK requests to the current owner's configured model endpoint.
Each physical attempt uses shared egress validation and a separate verified transport whose lifetime includes streamed responses.
"""

from dataclasses import dataclass
import ipaddress
import os
import re
from types import SimpleNamespace
from urllib.parse import urlsplit

from openai import DefaultHttpxClient, OpenAI

from llm_config.client_factory import KEYLESS_API_KEY_SENTINEL
from shared import external_http


class EvidenceTransportError(ValueError):
    pass


@dataclass(frozen=True)
class _Endpoint:
    scheme: str
    host: str
    port: int
    path: str


_PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def _private_address(value):
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback or isinstance(address, ipaddress.IPv4Address) and any(
        address in network for network in _PRIVATE_NETWORKS)


def _private_hosts():
    return frozenset(value.strip() for value in os.getenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS", "").split(",") if value.strip())


def _endpoint(value):
    try:
        if (type(value) is not str or not value or len(value.encode("utf-8")) > 2048
                or any(ord(character) < 33 or ord(character) == 127 for character in value)):
            raise ValueError
        parts = urlsplit(value)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None or parts.query or parts.fragment
                or not re.fullmatch(r"[A-Za-z0-9._~!$&'()*+,;=:@/-]*", parts.path)
                or "//" in parts.path or any(segment in {".", ".."} for segment in parts.path.split("/"))):
            raise ValueError
        host = parts.hostname.lower()
        host.encode("ascii")
        port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
        if not 1 <= port <= 65535:
            raise ValueError
        if parts.scheme == "http" and host not in _private_hosts():
            raise ValueError
        return _Endpoint(parts.scheme, host, port, parts.path.rstrip("/") + "/chat/completions")
    except (AttributeError, TypeError, ValueError, UnicodeError):
        raise EvidenceTransportError("evidence_provider_endpoint_refused") from None


class _GuardedStream:
    def __init__(self, stream, client):
        self._stream = stream
        self._iterator = iter(stream)
        self._client = client
        self._closed = False

    def __iter__(self):
        try:
            yield from self._iterator
        finally:
            self.close()

    def __next__(self):
        try:
            return next(self._iterator)
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                self._stream.close()
            finally:
                self._client.close()

    def __getattr__(self, name):
        return getattr(self._stream, name)


class _GuardedModelClient:
    def __init__(self, client, endpoint):
        self._template = client
        self._endpoint = endpoint
        self._closed = False
        self._keyless = not client.api_key or client.api_key == KEYLESS_API_KEY_SENTINEL
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        client.close()

    def _guard(self, request):
        try:
            parts = urlsplit(str(request.url))
            port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
            if (request.method != "POST" or (parts.scheme, parts.hostname, port)
                    != (self._endpoint.scheme, self._endpoint.host, self._endpoint.port)
                    or parts.path != self._endpoint.path or parts.query or parts.fragment
                    or parts.username is not None or parts.password is not None
                    or request.headers.get("host") != request.url.netloc.decode("ascii")):
                raise ValueError
            external_http.validate_egress_url(str(request.url))
            if parts.scheme == "http":
                if parts.hostname not in _private_hosts():
                    raise ValueError
                addresses = tuple(external_http._resolve_host_addresses(parts.hostname))
                if not addresses or any(not _private_address(address) for address in addresses):
                    raise ValueError
            if self._keyless:
                request.headers.pop("Authorization", None)
        except (external_http.ExternalHttpError, OSError, AttributeError, TypeError, ValueError, UnicodeError):
            raise EvidenceTransportError("evidence_provider_egress_refused") from None

    def _create(self, **request):
        if self._closed:
            raise EvidenceTransportError("evidence_provider_unavailable")
        transport = DefaultHttpxClient(timeout=self._template.timeout, verify=True, trust_env=False,
                                      follow_redirects=False, event_hooks={"request": [self._guard]})
        derived = None
        try:
            derived = self._template.with_options(http_client=transport, max_retries=0)
            response = derived.chat.completions.create(**request)
            if request.get("stream"):
                return _GuardedStream(response, derived)
            derived.close()
            return response
        except BaseException:
            if derived is None:
                transport.close()
            else:
                derived.close()
            raise

    def is_closed(self):
        return self._closed

    def close(self):
        self._closed = True
        self._template.close()

    def __getattr__(self, name):
        return getattr(self._template, name)


def guard_model_client(client, *, base_url):
    if not isinstance(client, OpenAI):
        raise EvidenceTransportError("evidence_provider_unavailable")
    try:
        endpoint = _endpoint(base_url)
        if _endpoint(str(client.base_url)) != endpoint:
            raise EvidenceTransportError("evidence_provider_endpoint_refused")
        return _GuardedModelClient(client, endpoint)
    except BaseException:
        client.close()
        raise
