"""Builds real PostgreSQL mesh and session custody with a local signing authority.
Only the external Keycloak HTTP boundary is replaced; JWT validation, Plane
transitions, durable audits, REST authentication and device proofs remain real.
"""

from __future__ import annotations

import base64
import importlib
from pathlib import Path
import time
from types import SimpleNamespace
import uuid

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt

from audit.repository import AuditRepository
from orchestrator import delegation, mesh_api, mesh_admission as ma, web_auth
from orchestrator.mesh_enrollment import MeshEnrollmentStore
from orchestrator.plane_repository_context import ApplicationPlaneSource
from orchestrator.session_store import WebSessionStore
from orchestrator.tool_permissions import ToolPermissionManager


class MeshRuntime:
    def __init__(self, runtime, monkeypatch):
        self.runtime = runtime
        self.owner = "mesh-owner-" + uuid.uuid4().hex
        self.issuer = "https://iam.mesh.invalid/realms/mesh"
        for name, value in {
            "ASTRAL_ENV": "development",
            "USE_MOCK_AUTH": "false",
            "KEYCLOAK_AUTHORITY": self.issuer,
            "KEYCLOAK_CLIENT_ID": "astral-frontend",
            "KEYCLOAK_CLIENT_SECRET": "synthetic-exchange-secret",
            "KEYCLOAK_ALLOWED_AZP": "astral-frontend",
            "AGENT_SERVICE_CLIENT_ID": "astral-agent-service",
            "PUBLIC_BASE_URL": "https://mesh.invalid",
            "WEB_SESSION_ENC_KEY": Fernet.generate_key().decode(),
            "MEMORY_HMAC_KEY": uuid.uuid4().hex,
            "DELEGATION_CHILD_SIGNING_KEY": uuid.uuid4().hex,
            "CREDENTIAL_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        }.items():
            monkeypatch.setenv(name, value)
        monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "sdk"))
        self.sdk = importlib.import_module("astral_sdk.mesh")
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.private = private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        public = private.public_key().public_numbers()

        def integer(value):
            return (
                base64.urlsafe_b64encode(
                    value.to_bytes((value.bit_length() + 7) // 8, "big")
                )
                .decode()
                .rstrip("=")
            )

        self.jwks = {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": "synthetic",
                    "use": "sig",
                    "alg": "RS256",
                    "n": integer(public.n),
                    "e": integer(public.e),
                }
            ]
        }

        async def keys(*args, **kwargs):
            return self.jwks

        monkeypatch.setattr("shared.jwks_cache.get_jwks", keys)
        self.audit = AuditRepository(
            plane_runtime=runtime, plane_repositories=runtime.repositories
        )
        self.sessions = WebSessionStore(
            plane_runtime=runtime, plane_repositories=runtime.repositories
        )
        self.sid = uuid.uuid4().hex
        self.owner_token = self.token()
        self.owner_session = self.sessions.create(
            self.sid,
            user_id=self.owner,
            access_token=self.owner_token,
            refresh_token="synthetic-refresh-" + uuid.uuid4().hex,
            hard_max_seconds=3600,
            issuing_issuer=self.issuer,
            issuing_client_id="astral-frontend",
            request_execution=True,
        )
        self.exchange_mode = None
        self.refresh_mode = None
        self.exchanges = []
        self.exchange_forms = []

        async def refresh(refresh_token, issuing_identity):
            if self.refresh_mode == "offline":
                raise ConnectionError("synthetic unavailable authority")
            if self.refresh_mode == "logout":
                self.sessions.delete(self.sid)
            return {
                "access_token": self.token(),
                "refresh_token": "synthetic-rotated-" + uuid.uuid4().hex,
            }

        def exchange_body(form):
            if self.exchange_mode == "error":
                return {"error": "access_denied"}
            claims = {
                "aud": form["audience"],
                "scope": form["scope"],
                "azp": form["client_id"],
            }
            if self.exchange_mode == "broad":
                claims["scope"] += " tools:write"
            if self.exchange_mode == "admin":
                claims["realm_access"] = {"roles": ["user", "admin"]}
            if self.exchange_mode == "role":
                claims["realm_access"] = {"roles": ["user", "custom-authority"]}
            if self.exchange_mode == "extra_audience":
                claims["aud"] = ["astral-agent-service", "account"]
            if self.exchange_mode == "exchange_client":
                claims["azp"] = "foreign-client"
            if self.exchange_mode == "tools":
                claims["scope"] += " tool:write"
            if self.exchange_mode == "missing_tools":
                claims["scope"] = " ".join(
                    scope
                    for scope in form["scope"].split()
                    if not scope.startswith("tool:")
                )
            if self.exchange_mode == "foreign":
                claims["sub"] = "foreign-owner"
            if self.exchange_mode == "audience":
                claims["aud"] = "foreign-resource"
            if self.exchange_mode == "expiry":
                claims["exp"] = int(time.time()) - 1
            return {"access_token": self.token(**claims)}

        fixture = self

        class ExchangeResponse:
            def __init__(self, form):
                self.status = 403 if fixture.exchange_mode == "error" else 200
                self.body = exchange_body(form)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def json(self):
                return self.body

        class ExchangeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            def post(self, url, *, data):
                fixture.exchange_forms.append((url, dict(data)))
                return ExchangeResponse(data)

        monkeypatch.setattr(delegation.aiohttp, "ClientSession", ExchangeSession)
        delegation_service = delegation.DelegationService()
        real_exchange = delegation_service.exchange_token_for_agent

        async def exchange(
            subject_token, agent_id, allowed_tools, user_id, enabled_scopes
        ):
            self.exchanges.append((agent_id, list(allowed_tools), list(enabled_scopes)))
            return await real_exchange(
                subject_token, agent_id, allowed_tools, user_id, enabled_scopes
            )

        monkeypatch.setattr(delegation_service, "exchange_token_for_agent", exchange)

        monkeypatch.setattr(web_auth, "_exchange_bound_session_refresh", refresh)
        self.permissions = ToolPermissionManager(
            plane_runtime=runtime, plane_repositories=runtime.repositories
        )
        self.permissions.register_tool_scopes(
            "reader", {"read": "tools:read", "write": "tools:write"}
        )
        self.permissions.set_agent_scopes(
            self.owner, "reader", {"tools:read": True, "tools:write": False}
        )
        self.host = SimpleNamespace(
            plane_repository_source=ApplicationPlaneSource(
                runtime, runtime.repositories
            ),
            audit_repo=self.audit,
            web_sessions=self.sessions,
            delegation=delegation_service,
            agent_cards={
                "reader": SimpleNamespace(
                    skills=[SimpleNamespace(id="read"), SimpleNamespace(id="write")]
                )
            },
            tool_permissions=self.permissions,
        )
        self.store = MeshEnrollmentStore(
            self.host.plane_repository_source, audit_repository=self.audit
        )
        self.service = ma.MeshAdmission(self.host, store=self.store)
        self.app = FastAPI()
        self.app.state.orchestrator = self.host
        self.app.include_router(mesh_api.mesh_router)
        self.client = TestClient(self.app, base_url="https://mesh.invalid")
        self.client.cookies.set(web_auth.COOKIE_NAME, web_auth._sign(self.sid))
        self.owner_headers = {
            "Authorization": "Bearer " + self.owner_token,
            "Origin": "https://mesh.invalid",
        }

    def token(self, **overrides):
        now = int(time.time())
        claims = {
            "sub": self.owner,
            "iss": self.issuer,
            "azp": "astral-frontend",
            "aud": "account",
            "iat": now,
            "exp": now + 1800,
            "realm_access": {"roles": ["user"]},
            "scope": "openid profile",
        }
        claims.update(overrides)
        return jwt.encode(
            claims, self.private, algorithm="RS256", headers={"kid": "synthetic"}
        )

    def invitation(self, device=None, *, scopes=None):
        device = device or self.sdk.MeshDevice.generate()
        response = self.client.post(
            "/api/mesh/invitations",
            headers=self.owner_headers,
            json={
                "label": "Synthetic device",
                "device_key": device.public_key,
                "scopes": scopes
                if scopes is not None
                else ["tools:read", "mesh:confirm"],
            },
        )
        assert response.status_code == 200, response.text
        record = response.json()
        return device, record

    def enroll(self, *, scopes=None, agent_id="reader"):
        device, invitation = self.invitation(scopes=scopes)
        confirmed = self.client.post(
            "/api/mesh/invitations/"
            + invitation["invitation"]["invite_id"]
            + "/confirm",
            headers=self.owner_headers,
        )
        assert confirmed.status_code == 200, confirmed.text
        client = self.sdk.MeshClient("https://mesh.invalid", device, client=self.client)
        client.redeem(invitation["payload"], agent_id=agent_id)
        return client, invitation

    def close(self):
        self.client.close()
