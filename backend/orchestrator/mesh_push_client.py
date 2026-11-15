"""Delivers content-free node wakes to an explicitly configured HTTPS publisher through approved external HTTP.
Only current native owner/principal and signed device routes can supply a bounded opaque capability."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Callable
from urllib.parse import urlsplit

from orchestrator.push_wake import FAILED, NO_DESTINATION, RATE_LIMITED, SENT, WakeLimiter, WakeOutcome
from shared import external_http, node_identity

PUBLISHER_URL_ENV = "ASTRAL_MESH_PUSH_PUBLISHER_URL"
_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CAPABILITY = re.compile(r"[A-Za-z0-9_-]+={0,2}\Z", re.ASCII)


class RemoteWakeGateway:
    reconsider_pending = True

    def __init__(self, publisher_url: str, *, transport: Callable | None = None,
                 clock: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
                 limiter: WakeLimiter | None = None):
        parsed = urlsplit(publisher_url)
        if (not publisher_url.isascii() or any(char.isspace() for char in publisher_url)
                or "\\" in publisher_url or parsed.scheme != "https" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError("publisher_url_invalid")
        try:
            parsed.port
        except ValueError:
            raise ValueError("publisher_url_invalid") from None
        self._url = publisher_url.rstrip("/") + "/api/push/mesh/wake"
        self._transport = transport or external_http.request
        self._clock = clock
        self._limiter = limiter or WakeLimiter()

    def for_mesh(self, mesh: Any) -> "MeshWakeSender":
        return MeshWakeSender(self, mesh)


class MeshWakeSender:
    reconsider_pending = True

    def __init__(self, gateway: RemoteWakeGateway, mesh: Any):
        self._gateway = gateway
        self._mesh = mesh

    def _snapshot(self, kernel: Any, owner: str, target: str) -> tuple[str, str, int] | None:
        if not self._mesh.started or self._mesh.kernel is not kernel or not _ID.fullmatch(target):
            return None
        principal = getattr(kernel, "mesh_principal", None)
        routes = getattr(kernel, "wake_routes", None)
        if not callable(principal) or not callable(routes):
            return None
        proof = principal()
        mesh_id = proof.mesh_id
        if not _ID.fullmatch(mesh_id):
            return None
        if owner not in {node_identity.canonical_owner_id(identity.issuer, identity.subject) for identity in proof.identities}:
            return None
        matches = [row.route for row in routes() if row.mesh_id == mesh_id and row.device_id == target]
        if len(matches) != 1:
            return None
        route = matches[0]
        expiry, cap = route.expires_at_ms, route.capability
        now = self._gateway._clock()
        if (route.publisher != "astral" or not isinstance(cap, str) or not 1 <= len(cap) <= 2048
                or not _CAPABILITY.fullmatch(cap) or type(expiry) is not int or type(now) is not int
                or not now < expiry <= min(now + 86_400_000, 9_007_199_254_740_991)):
            return None
        captured = (mesh_id, cap, expiry)
        proof = principal()
        if (proof.mesh_id != mesh_id or owner not in {
                node_identity.canonical_owner_id(identity.issuer, identity.subject) for identity in proof.identities}
                or not self._mesh.started or self._mesh.kernel is not kernel):
            return None
        return captured

    async def wake(self, owner_id: str, mesh_device_id: str) -> WakeOutcome:
        try:
            kernel = self._mesh.kernel

            def dispatch():
                route = self._snapshot(kernel, owner_id, mesh_device_id)
                if route is None:
                    return WakeOutcome(NO_DESTINATION)
                key = f"{owner_id}\n{route[0]}\n{mesh_device_id}"
                if not self._gateway._limiter.admit(key):
                    return WakeOutcome(RATE_LIMITED)
                if self._snapshot(kernel, owner_id, mesh_device_id) != route:
                    return WakeOutcome(NO_DESTINATION)
                response = self._gateway._transport(
                    "POST", self._gateway._url, api_key=route[1],
                    json_body={"mesh_id": route[0], "device_id": mesh_device_id},
                    timeout=8, max_response_bytes=1024, allow_redirects=False,
                    trust_environment=False, require_identity_encoding=True)
                if response.status_code == 204:
                    return WakeOutcome(SENT)
                return WakeOutcome(FAILED, "publisher_unavailable")

            return await asyncio.to_thread(dispatch)
        except (external_http.AuthFailedError, external_http.BadRequestError):
            return WakeOutcome(NO_DESTINATION)
        except external_http.RateLimitedError:
            return WakeOutcome(RATE_LIMITED)
        except Exception:
            return WakeOutcome(FAILED, "publisher_unavailable")
