"""Drives real MCP, A2A, and accepted client-local voice ingress through governed
dispatch and a generated protected executor onto one isolated Plane database.
Only provider resolution and model tool selection are simulated.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import sys
import time
import unicodedata
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from jose import jwt

from astralplane.authority import (
    AgentAuthorityBinding,
    AuthorityBindingState,
    AuthorityPopulation,
)
from lets.canonical import b64url_encode, canonical_digest, canonical_json
from lets.crypto import Ed25519Signer
from lets.manifest import (
    ClusterManifest,
    ManifestPublicKey,
    ManifestSignature,
    WardenManifest,
)
from lets.models import Receipt
from lets.policy import MachineSpec, PolicySpec, ResourceDimension, TransitionSpec

from orchestrator.generated_lets_executor import _SCOPE_BINDINGS
from orchestrator.governed_dispatch import DispatchRuntime
from orchestrator.lets_client import LetsClientBoundaryError
from orchestrator.lets_config import LetsHostConfig
from orchestrator.lets_effects import PlaneProtectedEffectCoordinator
from orchestrator.lets_gateway import LetsAuthorizationGateway
from orchestrator.orchestrator import Orchestrator
from orchestrator.voice_bootstrap import build_voice_services
from shared.protocol import MCP_PROTOCOL_VERSION, MCPRequest
from tests.helpers.plane_template import database_dsn
from tests.helpers.voice_plane_runtime import (
    build_test_database_url,
    isolated_voice_plane_runtime,
)

TENANT_ID = "tenant-ingress"
ENVELOPE_ID = "envelope-ingress"
CONFIG_EPOCH = 7
TOOL_NAME = "clinical_lookup"
FINAL_ARGUMENTS = {"query": "private patient value", "limit": "5"}
PUBLIC_BASE_URL = "http://localhost:18001"


def _unique(prefix: str) -> str:
    return f"{prefix.replace('_', '-')}-{uuid.uuid4().hex[:12]}"


def _lease_for(binding_id: str) -> str:
    return f"lease-{binding_id}"


def _lineage_for(binding_id: str) -> str:
    return f"lineage-{binding_id}"


def _create_binding(plane: Any, repository: Any, binding: Any) -> None:
    with plane.transaction() as transaction:
        repository.create_binding(transaction, binding)


def _signed_manifest(
    warden: Ed25519Signer, operator: Ed25519Signer
) -> tuple[ClusterManifest, PolicySpec]:
    resources = tuple(
        ResourceDimension(name, "count")
        for name in ("read", "write", "search", "system", "files", "execute")
    )
    transitions = tuple(
        TransitionSpec(
            name=transition,
            source="ready",
            target="ready",
            cost=tuple(1 if index == dimension else 0 for index in range(6)),
            capability=capability,
        )
        for _scope, capability, transition, dimension in _SCOPE_BINDINGS
    )
    policy = PolicySpec(
        policy_id="astral-ingress-tools",
        policy_version="v1",
        dimensions=resources,
        machine=MachineSpec(
            machine_id="astral-ingress-effects",
            initial_state="ready",
            transitions=transitions,
        ),
        max_lease_ttl_ns=60_000_000_000,
        receipt_ttl_ns=30_000_000_000,
        max_clock_uncertainty_ns=2_000_000_000,
        transfer_gap_window=64,
    )
    manifest = ClusterManifest(
        tenant_id=TENANT_ID,
        envelope_id=ENVELOPE_ID,
        config_epoch=CONFIG_EPOCH,
        created_at="2026-09-01T00:00:00Z",
        resources=resources,
        initial_budget=(100, 100, 100, 100, 100, 100),
        wardens=(
            WardenManifest(
                warden_id=warden.warden_id,
                peer_endpoint="https://warden-ingress.example",
                client_endpoint="https://warden-ingress.example",
                initial_share=(100, 100, 100, 100, 100, 100),
                keys=(ManifestPublicKey(warden.key_id, warden.public_key_bytes),),
                extensions={},
            ),
        ),
        policies=(policy,),
        extensions={},
    )
    manifest = replace(
        manifest,
        signatures=(
            ManifestSignature(
                operator.key_id,
                operator.sign(canonical_json(manifest.unsigned_dict())),
            ),
        ),
    )
    return manifest, policy


class IngressWarden:
    def __init__(self, signer: Ed25519Signer, policy: PolicySpec) -> None:
        self.signer = signer
        self.policy = policy
        self.calls: list[dict[str, object]] = []
        self.failure: Exception | None = None

    def authorize_tool(self, **values: object) -> Receipt:
        self.calls.append(dict(values))
        if self.failure is not None:
            raise self.failure
        evidence = values["evidence"]
        assert isinstance(evidence, dict)
        now_ns = time.time_ns()
        receipt = Receipt(
            tenant_id=TENANT_ID,
            envelope_id=ENVELOPE_ID,
            config_epoch=CONFIG_EPOCH,
            receipt_id=f"receipt-{values['operation_id']}",
            request_id=str(values["operation_id"]),
            warden_id=self.signer.warden_id,
            key_id=self.signer.key_id,
            policy_id=self.policy.policy_id,
            policy_version=self.policy.policy_version,
            policy_digest=self.policy.digest,
            machine_digest=self.policy.machine.digest,
            lease_id=str(values["lease_id"]),
            lineage_id=_lineage_for(str(values["lease_id"]).removeprefix("lease-")),
            subject_id=str(values["agent_id"]),
            executor_audience=str(values["executor_audience"]),
            transition="tool_read",
            source_state="ready",
            target_state="ready",
            cost=(1, 0, 0, 0, 0, 0),
            resulting_sequence=int(values["expected_sequence"]) + 1,
            evidence_digest=canonical_digest(dict(evidence)),
            nonce=str(values["nonce"]),
            issued_at_ns=now_ns,
            expires_at_ns=now_ns + self.policy.receipt_ttl_ns,
        )
        return replace(
            receipt,
            signature=b64url_encode(
                self.signer.sign(canonical_json(receipt.unsigned_payload()))
            ),
        )


class IngressIdentity:
    def __init__(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from jose import jwk
        from shared import jwks_cache

        self.authority = f"https://ingress-{uuid.uuid4().hex}.invalid/realms/astral"
        self.url = f"{self.authority}/protocol/openid-connect/certs"
        self.kid = f"ingress-{uuid.uuid4().hex}"
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.private_key = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        public = key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self.jwks = {
            "keys": [
                jwk.construct(public, algorithm="RS256").to_dict()
                | {"kid": self.kid, "use": "sig"}
            ]
        }
        self.cache = jwks_cache._cache
        self.cache[self.url] = {"jwks": self.jwks, "fetched_at": time.time()}

    def _encode(self, claims: dict[str, Any]) -> str:
        return jwt.encode(
            claims, self.private_key, algorithm="RS256", headers={"kid": self.kid}
        )

    def mcp_token(self, sub: str) -> str:
        now = int(time.time())
        return self._encode(
            {
                "sub": sub,
                "preferred_username": sub,
                "iss": self.authority,
                "azp": "astral-mcp-client",
                "aud": "astral-mcp",
                "scope": "openid mcp:tools:invoke",
                "realm_access": {"roles": ["user"]},
                "iat": now,
                "exp": now + 300,
            }
        )

    def user_token(self, sub: str) -> str:
        now = int(time.time())
        return self._encode(
            {
                "sub": sub,
                "preferred_username": sub,
                "iss": self.authority,
                "azp": "astral-frontend",
                "aud": "account",
                "realm_access": {"roles": ["user"]},
                "resource_access": {"astral-frontend": {"roles": ["user"]}},
                "iat": now,
                "exp": now + 300,
            }
        )

    def close(self) -> None:
        self.cache.pop(self.url, None)


@pytest.fixture
def ingress_identity(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv(
        "VOICE_UI_BINDING_SECRET", "ingress-fixture-only-binding-secret-0001"
    )
    identity = IngressIdentity()
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", identity.authority)
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")
    monkeypatch.delenv("KEYCLOAK_ALLOWED_AZP", raising=False)
    yield identity


@pytest.fixture(scope="module")
def ingress_plane():
    with isolated_voice_plane_runtime("lets_ingress") as runtime:
        yield runtime


@pytest.fixture(autouse=True)
async def ingress_database(ingress_plane, monkeypatch):
    database = await asyncio.to_thread(
        ingress_plane.fetch_one, "SELECT current_database() AS name"
    )
    monkeypatch.setenv(
        "DATABASE_URL", database_dsn(build_test_database_url(), database["name"])
    )
    monkeypatch.setenv("ASTRALPLANE_IDENTITY", _unique("ingress-composition"))


class GeneratedExecutor:
    def __init__(self, root: Path, slug: str, agent_id: str) -> None:
        self.root = root
        self.slug = slug
        self.agent_id = agent_id
        self.captured: list[MCPRequest] = []
        self.bundle_dir = root / "agents" / slug
        self.bundle_dir.mkdir(parents=True)
        self.replay_root = root / "executor-state"
        self.replay_root.mkdir()
        self.effect_log = root / f"effects-{slug}.jsonl"
        self.module: Any = None
        self.server: Any = None

    def install(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        mode_name: str,
        warden: IngressWarden,
        operator: Ed25519Signer,
        manifest: ClusterManifest,
        policy: PolicySpec,
        owner_id: str,
        binding_id: str,
        runtime_id: str,
        runtime_generation: int,
        audience: str,
    ) -> None:
        from orchestrator.agent_generator import AgentCodeGenerator

        generator = AgentCodeGenerator(llm_client=object(), llm_model="unused")
        files = generator.generate_template_files(
            "Ingress Agent", "composed ingress probe", self.slug, agent_id=self.agent_id
        )
        (self.bundle_dir / "mcp_server.py").write_text(files["mcp_server.py"])
        (self.bundle_dir / "protected_executor.py").write_text(
            files["protected_executor.py"]
        )
        (self.bundle_dir / "mcp_tools.py").write_text(self._tools_source())
        manifest_path = self.root / f"trust-manifest-{self.slug}.json"
        manifest_path.write_text(
            json.dumps(manifest.to_dict(), sort_keys=True), encoding="utf-8"
        )
        database_root = self.replay_root / f"db-{self.slug}"
        authority_root = self.replay_root / f"authority-{self.slug}"
        database_root.mkdir()
        authority_root.mkdir()
        for name, value in {
            "FF_LETS_EXTERNAL_WARDEN": "1",
            "LETS_MODE": mode_name,
            "LETS_SIGNED_TRUST_MANIFEST": str(manifest_path),
            "LETS_MANIFEST_OPERATOR_KEYS_FILE": str(
                self._operator_keys(operator)
            ),
            "LETS_WARDEN_ID": warden.signer.warden_id,
            "LETS_TENANT_ID": TENANT_ID,
            "LETS_ENVELOPE_ID": ENVELOPE_ID,
            "LETS_POLICY_DIGEST": policy.digest,
            "LETS_MACHINE_DIGEST": policy.machine.digest,
            "LETS_EXECUTOR_INSTANCE_ID": audience,
            "LETS_EXECUTOR_DB_ROOT": str(database_root),
            "LETS_EXECUTOR_AUTHORITY_ROOT": str(authority_root),
            "ASTRAL_AUTHORITY_OWNER_ID": owner_id,
            "ASTRAL_AUTHORITY_BINDING_ID": binding_id,
            "ASTRAL_AUTHORITY_LEASE_ID": _lease_for(binding_id),
            "ASTRAL_AUTHORITY_LINEAGE_ID": _lineage_for(binding_id),
            "ASTRAL_RUNTIME_ID": runtime_id,
            "ASTRAL_RUNTIME_GENERATION": str(runtime_generation),
            "LETS_INGRESS_EFFECT_LOG": str(self.effect_log),
        }.items():
            monkeypatch.setenv(name, value)
        self.module = self._import(files["mcp_server.py"])
        self.server = self.module.MCPServer()
        original = self.server.process_request

        def recording(request: MCPRequest) -> Any:
            self.captured.append(request)
            return original(request)

        self.server.process_request = recording

    def _operator_keys(self, operator: Ed25519Signer) -> Path:
        from orchestrator import generated_lets_executor as executor

        path = self.root / f"operators-{self.slug}.json"
        path.write_text(
            json.dumps(
                {
                    "api_version": executor.OPERATOR_TRUST_TYPE,
                    "threshold": 1,
                    "keys": [
                        {
                            "key_id": operator.key_id,
                            "algorithm": "Ed25519",
                            "public_key": b64url_encode(operator.public_key_bytes),
                        }
                    ],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return path

    def _tools_source(self) -> str:
        log = json.dumps(str(self.effect_log))
        return (
            "import json\n\n\n"
            "def _clinical_lookup(*, query, limit):\n"
            f"    with open({log}, 'a', encoding='utf-8') as handle:\n"
            "        handle.write(json.dumps({'query': query, 'limit': limit}) + '\\n')\n"
            "    return {'records': [], 'query': query, 'limit': limit}\n\n\n"
            "TOOL_REGISTRY = {\n"
            f"    {TOOL_NAME!r}: {{\n"
            "        'description': 'bounded benign clinical lookup',\n"
            "        'scope': 'tools:read',\n"
            "        'function': _clinical_lookup,\n"
            "    },\n"
            "}\n"
        )

    def _import(self, source: str) -> Any:
        package_name = f"agents.{self.slug}"
        module_name = f"ingress_mcp_server_{self.slug}"
        if package_name not in sys.modules:
            package = importlib.util.module_from_spec(
                importlib.util.spec_from_loader(package_name, loader=None)
            )
            package.__path__ = [str(self.bundle_dir)]
            sys.modules[package_name] = package
        for filename in ("mcp_tools", "protected_executor"):
            submodule_name = f"{package_name}.{filename}"
            if submodule_name in sys.modules:
                continue
            submodule = importlib.util.module_from_spec(
                importlib.util.spec_from_loader(submodule_name, loader=None)
            )
            submodule.__file__ = str(self.bundle_dir / f"{filename}.py")
            sys.modules[submodule_name] = submodule
            exec(
                compile(
                    (self.bundle_dir / f"{filename}.py").read_text(),
                    submodule.__file__,
                    "exec",
                ),
                submodule.__dict__,
            )
            sys.modules[submodule_name] = submodule
        module = importlib.util.module_from_spec(
            importlib.util.spec_from_loader(module_name, loader=None)
        )
        module.__file__ = str(self.bundle_dir / "mcp_server.py")
        sys.modules[module_name] = module
        exec(compile(source, module.__file__, "exec"), module.__dict__)
        return module

    def build_agent(self) -> Any:
        from unittest.mock import MagicMock

        from shared.base_agent import BaseA2AAgent

        agent = BaseA2AAgent.__new__(BaseA2AAgent)
        agent.agent_id = self.agent_id
        agent.card = SimpleNamespace(version="1.0.0")
        agent.mcp_server = self.server
        agent._logger = MagicMock()
        agent._decrypt_credentials_if_needed = MagicMock()
        agent._stream_wrapper_tasks = set()
        return agent

    def read_effects(self) -> list[dict[str, object]]:
        if not self.effect_log.exists():
            return []
        return [
            json.loads(line)
            for line in self.effect_log.read_text().splitlines()
            if line.strip()
        ]

    def replay_store(self) -> Any:
        return self.server.protected_executor.replay_store


class _RecorderSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_text(self, data: str) -> bool:
        try:
            self.sent.append(json.loads(data))
        except (TypeError, ValueError):
            self.sent.append({"raw": data})
        return True


def _frame_of(socket: _RecorderSocket, frame_type: str) -> dict[str, Any]:
    for frame in socket.sent:
        if frame.get("type") == frame_type:
            return dict(frame)
    raise AssertionError(f"expected a {frame_type} frame")


def _agent_card(agent_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        agent_id=agent_id,
        name="Ingress Agent",
        description="composed ingress probe agent",
        skills=[
            SimpleNamespace(
                id=TOOL_NAME,
                name=TOOL_NAME,
                description="bounded benign clinical lookup",
                scope="tools:read",
                input_schema={"type": "object", "properties": {}},
                output_schema=None,
                metadata={},
            )
        ],
        metadata={},
    )


class IngressRig:
    def __init__(
        self,
        *,
        plane: Any,
        orchestrator: Orchestrator,
        identity: IngressIdentity | None = None,
        warden: IngressWarden,
        gateway: LetsAuthorizationGateway,
        executor: GeneratedExecutor,
        owner_id: str,
        agent_id: str,
        runtime_id: str,
        runtime_generation: int,
        audience: str,
        binding_id: str,
        repository: Any,
    ) -> None:
        self.plane = plane
        self.orchestrator = orchestrator
        self.identity = identity
        self.warden = warden
        self.gateway = gateway
        self.executor = executor
        self.owner_id = owner_id
        self.agent_id = agent_id
        self.runtime_id = runtime_id
        self.runtime_generation = runtime_generation
        self.audience = audience
        self.binding_id = binding_id
        self.repository = repository
        self.agent = executor.build_agent()

    def wire_orchestrator(self) -> None:
        orch = self.orchestrator
        orch.local_agents[self.agent_id] = self.agent
        orch.agents.pop(self.agent_id, None)
        orch.agent_cards[self.agent_id] = _agent_card(self.agent_id)
        orch.security_flags.pop(self.agent_id, None)
        orch.tool_permissions.set_agent_scopes(
            self.owner_id, self.agent_id, {"tools:read": True}
        )
        orch.tool_permissions.register_tool_scopes(self.agent_id, {TOOL_NAME: "tools:read"})
        # Delegation mints locally in the development posture; ingress auth stays real
        orch.delegation.mock_auth = True


async def _build_rig(
    *,
    ingress_plane: Any,
    orchestrator: Orchestrator,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> IngressRig:
    owner_id = _unique("ingress-owner")
    agent_id = _unique("ingress-agent")
    runtime_id = _unique("ingress-runtime")
    generation = 3
    audience = _unique("ingress-executor")
    binding_id = _unique("binding")

    signer = Ed25519Signer.generate("warden-ingress")
    operator = Ed25519Signer.generate("operator-ingress")
    manifest, policy = _signed_manifest(signer, operator)
    warden = IngressWarden(signer, policy)

    repository = ingress_plane.repositories.authority
    effects = PlaneProtectedEffectCoordinator(
        plane=ingress_plane,
        repository=repository,
    )
    config = LetsHostConfig(
        master_enabled=mode != "off",
        mode=mode,  # type: ignore[arg-type]
        environment="test",
        governed_cohorts=("server_dynamic",),
        governed_agent_allowlist=(),
    )
    gateway = LetsAuthorizationGateway(
        config,
        None if mode == "off" else warden,  # type: ignore[arg-type]
        effect_coordinator=effects if mode == "enforce" else None,
    )
    if mode in {"enforce", "shadow"}:
        now = datetime.now(UTC)
        binding = AgentAuthorityBinding(
            binding_id=binding_id,
            owner_id=owner_id,
            agent_id=agent_id,
            runtime_id=runtime_id,
            runtime_generation=generation,
            population=AuthorityPopulation.SERVER_DYNAMIC,
            tenant_id=TENANT_ID,
            envelope_id=ENVELOPE_ID,
            warden_id=signer.warden_id,
            lease_id=_lease_for(binding_id),
            lineage_id=_lineage_for(binding_id),
            subject_id=agent_id,
            policy_digest=policy.digest,
            machine_digest=policy.machine.digest,
            config_epoch=CONFIG_EPOCH,
            capabilities=("astral.tools.read",),
            lease_sequence=0,
            lease_expires_at_ns=time.time_ns() + 600_000_000_000,
            state=AuthorityBindingState.ACTIVE,
            created_at=now,
            updated_at=now,
            version=1,
        )
        await asyncio.to_thread(
            _create_binding, ingress_plane, repository, binding
        )

    executor = GeneratedExecutor(tmp_path, f"ingress_{uuid.uuid4().hex[:8]}", agent_id)
    executor.install(
        monkeypatch,
        mode_name=mode,
        warden=warden,
        operator=operator,
        manifest=manifest,
        policy=policy,
        owner_id=owner_id,
        binding_id=binding_id,
        runtime_id=runtime_id,
        runtime_generation=generation,
        audience=audience,
    )

    if mode in {"enforce", "shadow"}:
        orchestrator.bind_governed_final_dispatch(
            gateway=gateway,
            plane=ingress_plane,
            authority_repository=repository,
        )
        orchestrator.register_governed_dispatch_runtime(
            DispatchRuntime(
                owner_id=owner_id,
                agent_id=agent_id,
                population="server_dynamic",
                runtime_id=runtime_id,
                runtime_generation=generation,
                executor_audience=audience,
                executor_conformant=True,
                dispatch_posture="protected_executor",
            )
        )

    rig = IngressRig(
        plane=ingress_plane,
        orchestrator=orchestrator,
        warden=warden,
        gateway=gateway,
        executor=executor,
        owner_id=owner_id,
        agent_id=agent_id,
        runtime_id=runtime_id,
        runtime_generation=generation,
        audience=audience,
        binding_id=binding_id,
        repository=repository,
    )
    rig.wire_orchestrator()
    return rig


@pytest.fixture
async def rig_enforce(
    ingress_plane,
    ingress_identity,
    tmp_path,
    monkeypatch,
    orchestrator_factory,
):
    identity = ingress_identity
    orchestrator = await asyncio.to_thread(orchestrator_factory)
    rig = await _build_rig(
        ingress_plane=orchestrator.runtime_composition.plane.runtime,
        orchestrator=orchestrator,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        mode="enforce",
    )
    rig.identity = identity
    try:
        yield rig
    finally:
        for context in tuple(orchestrator._connection_contexts.values()):
            await orchestrator._drain_connection_context(context)


@pytest.fixture
async def rig_off(
    ingress_plane, ingress_identity, tmp_path, monkeypatch, orchestrator_factory
):
    identity = ingress_identity
    orchestrator = await asyncio.to_thread(orchestrator_factory)
    rig = await _build_rig(
        ingress_plane=orchestrator.runtime_composition.plane.runtime,
        orchestrator=orchestrator,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        mode="off",
    )
    rig.identity = identity
    yield rig


def _claims(sub: str) -> dict[str, Any]:
    return {
        "sub": sub,
        "preferred_username": sub,
        "realm_access": {"roles": ["user"]},
    }


def _mcp_app(rig: IngressRig) -> FastAPI:
    from orchestrator.mcp_server_endpoint import create_mcp_router

    app = FastAPI()
    app.include_router(
        create_mcp_router(rig.orchestrator, public_base_url=PUBLIC_BASE_URL)
    )
    return app


async def _post_mcp(rig: IngressRig, arguments: dict[str, object]) -> dict[str, Any]:
    app = _mcp_app(rig)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url=PUBLIC_BASE_URL
    ) as client:
        response = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": TOOL_NAME,
                    "arguments": arguments,
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,
                        "io.modelcontextprotocol/clientCapabilities": {},
                    },
                },
            },
            headers={
                "Authorization": f"Bearer {rig.identity.mcp_token(rig.owner_id)}",
                "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
                "Mcp-Method": "tools/call",
                "Mcp-Name": TOOL_NAME,
            },
        )
    assert response.status_code == 200, response.text
    return response.json()


def _a2a_app(rig: IngressRig) -> FastAPI:
    from orchestrator.a2a_orchestrator_executor import setup_orchestrator_a2a

    app = FastAPI()
    setup_orchestrator_a2a(app, rig.orchestrator)
    return app


async def _post_a2a(rig: IngressRig, arguments: dict[str, object]) -> dict[str, Any]:
    app = _a2a_app(rig)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url=PUBLIC_BASE_URL
    ) as client:
        response = await client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "message/send",
                "params": {
                    "message": {
                        "messageId": str(uuid.uuid4()),
                        "role": "user",
                        "parts": [
                            {
                                "data": {
                                    "method": "tools/call",
                                    "name": TOOL_NAME,
                                    "arguments": arguments,
                                }
                            }
                        ],
                    }
                },
            },
            headers={
                "Authorization": f"Bearer {rig.identity.user_token(rig.owner_id)}"
            },
        )
    assert response.status_code == 200, response.text
    return response.json()


def _a2a_error(payload: dict[str, Any]) -> str:
    result = payload.get("result") or {}
    history = result.get("history") or []
    if history:
        for part in history[-1].get("parts", []):
            text = part.get("text")
            if isinstance(text, str):
                return text
    return json.dumps(payload)


def _voice_app(rig: IngressRig) -> FastAPI:
    from orchestrator.voice_api import router as voice_router

    app = FastAPI()
    app.state.orchestrator = rig.orchestrator
    app.include_router(voice_router)
    return app


async def _ensure_history_chat(orch: Orchestrator, owner: str, chat_id: str) -> str:
    existing = await asyncio.to_thread(orch.history.get_chat, chat_id, user_id=owner)
    if existing is None:
        await asyncio.to_thread(orch.history.create_chat, chat_id, user_id=owner)
    return chat_id


async def _create_voice_session(
    rig: IngressRig,
    *,
    device_id: str,
    connection_generation: str,
    binding_bearer: str,
    chat_id: str,
) -> str:
    app = _voice_app(rig)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url=PUBLIC_BASE_URL
    ) as client:
        response = await client.post(
            "/api/voice/v2/sessions",
            json={
                "schema_version": "2",
                "activation_id": str(uuid.uuid4()),
                "device_id": device_id,
                "device_kind": "web",
                "visible_chat_id": chat_id,
                "foreground_active": True,
                "client_capability": {
                    "contract": "client_local/v1",
                    "transport": "client_local",
                    "configured_locale": "en-US",
                    "full_duplex": False,
                    "has_microphone": True,
                    "has_audio_output": True,
                    "microphone_permission": "authorized",
                    "recognition_permission": "authorized",
                    "recognition_processing": "guaranteed_local",
                    "recognition_locale": "ready",
                    "recognition_installation": "ready",
                    "synthesis_processing": "guaranteed_local",
                    "synthesis_locale": "ready",
                },
            },
            headers={
                "Authorization": f"Bearer {rig.identity.user_token(rig.owner_id)}",
                "X-Astral-Device-Id": device_id,
                "X-Astral-Connection-Generation": connection_generation,
                "X-Astral-Voice-Control-Binding": binding_bearer,
            },
        )
    assert response.status_code == 201, response.text
    session = (response.json() or {}).get("session") or response.json()
    session_id = str(session.get("session_id") or "")
    assert session_id, response.text
    return session_id


async def _submit_voice_frame(
    rig: IngressRig, socket: _RecorderSocket, frame: dict[str, Any]
) -> None:
    orch = rig.orchestrator
    context = orch._connection_contexts.get(id(socket))
    if context is None:
        context = orch._new_connection_context(socket)
    assert await orch._route_ui_frame(context, json.dumps(frame))
    async with asyncio.timeout(10):
        if context.admission_task is not None:
            await context.admission_task
        tasks = [
            work.task for work in context.operations.values() if work.task is not None
        ]
        if tasks:
            await asyncio.gather(*tasks)


async def _accepted_voice_turn(
    rig: IngressRig,
) -> tuple[_RecorderSocket, str, Any, dict[str, Any]]:
    orch = rig.orchestrator
    owner = rig.owner_id
    device_id = str(uuid.uuid4())
    connection_generation = str(uuid.uuid4())
    chat_id = str(uuid.uuid4())

    services = build_voice_services(
        plane_runtime=rig.plane,
        plane_repositories=rig.plane.repositories,
        environ={
            "VOICE_SPEECH_BACKEND": "client_local",
            "VOICE_COORDINATOR_REPLICA_ID": "voice-coordinator-local-1",
        },
    )
    orch.voice_services = services
    orch.voice_runtime = services.runtime

    await _ensure_history_chat(orch, owner, chat_id)

    socket = _RecorderSocket()
    await _submit_voice_frame(
        rig,
        socket,
        {
            "type": "register_ui",
            "token": rig.identity.user_token(owner),
            "device_id": device_id,
            "connection_generation": connection_generation,
            "device": {"device_type": "web"},
            "capabilities": [],
        },
    )
    binding_frame = _frame_of(socket, "voice_control_binding")
    session_id = await _create_voice_session(
        rig,
        device_id=device_id,
        connection_generation=connection_generation,
        binding_bearer=binding_frame["binding"],
        chat_id=chat_id,
    )

    common = {
        "schema_version": "2",
        "speech_backend": "client_local",
        "device_id": device_id,
        "connection_generation": connection_generation,
        "session_id": session_id,
        "generation": 1,
        "speech_revision": 1,
    }
    await _submit_voice_frame(
        rig,
        socket,
        {
            "type": "voice_local_ready",
            **common,
            "contract": "client_local/v1",
            "transport": "client_local",
            "configured_locale": "en-US",
            "full_duplex": False,
            "has_microphone": True,
            "has_audio_output": True,
            "microphone_permission": "authorized",
            "recognition_permission": "authorized",
            "recognition_processing": "guaranteed_local",
            "recognition_locale": "ready",
            "recognition_installation": "ready",
            "synthesis_processing": "guaranteed_local",
            "synthesis_locale": "ready",
            "client_sequence": 1,
        },
    )
    client_turn_id = str(uuid.uuid4())
    await _submit_voice_frame(
        rig,
        socket,
        {
            "type": "voice_local_recognition_started",
            **common,
            "client_turn_id": client_turn_id,
            "chat_id": chat_id,
            "chat_context_revision": 1,
            "recognition_sequence": 2,
        },
    )
    bound = _frame_of(socket, "voice_local_turn_bound")
    canonical = unicodedata.normalize("NFC", "lookup patient record 42").strip()
    final = {
        "type": "voice_local_final",
        **common,
        "client_turn_id": client_turn_id,
        "turn_id": bound["turn_id"],
        "submission_id": bound["submission_id"],
        "request_generation": bound["request_generation"],
        "chat_id": chat_id,
        "chat_context_revision": 1,
        "recognition_sequence": bound["recognition_sequence"],
        "final": True,
        "recognized_locale": "en-US",
        "text": canonical,
        "text_digest_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
    }
    await _submit_voice_frame(rig, socket, final)
    turn = await asyncio.to_thread(
        services.repository.get_turn, user_id=owner, turn_id=bound["turn_id"]
    )
    assert turn.detected_language == "en", turn
    assert turn.spoken_output_policy is not None, turn
    return socket, chat_id, turn, final


@pytest.fixture
def voice_model(rig_enforce, monkeypatch):
    from openai.types.chat import ChatCompletion, ChatCompletionMessage
    from llm_config.types import CredentialSource, ResolvedConfig

    selected: list[str] = []
    tool_results: list[str] = []
    model = ResolvedConfig(
        model="synthetic-ingress-model",
        base_url="https://synthetic-provider.invalid/v1",
    )

    def title_completion(**kwargs):
        return ChatCompletion.model_validate(
            {
                "id": "synthetic-chat-title",
                "object": "chat.completion",
                "created": 0,
                "model": model.model,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "Clinical lookup"},
                    }
                ],
            }
        )

    provider = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=title_completion))
    )

    async def resolve(websocket):
        assert rig_enforce.orchestrator._get_user_id(websocket) == rig_enforce.owner_id
        return provider, CredentialSource.USER, model

    async def complete(websocket, messages, tools_desc=None, **kwargs):
        assert rig_enforce.orchestrator._get_user_id(websocket) == rig_enforce.owner_id
        if kwargs.get("feature") == "tool_dispatch":
            tool_results[:] = [
                message["content"]
                for message in messages
                if isinstance(message, dict) and message.get("role") == "tool"
            ]
        if kwargs.get("feature") == "tool_dispatch" and not selected:
            available = {
                definition["function"]["name"] for definition in tools_desc or []
            }
            assert TOOL_NAME in available
            selected.append(TOOL_NAME)
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "synthetic-voice-lookup",
                        "type": "function",
                        "function": {
                            "name": TOOL_NAME,
                            "arguments": json.dumps(FINAL_ARGUMENTS),
                        },
                    }
                ],
            }
        else:
            message = {
                "role": "assistant",
                "content": "The synthetic voice turn finished.",
                "tool_calls": [],
            }
        return ChatCompletionMessage.model_validate(message), None

    monkeypatch.setattr(rig_enforce.orchestrator, "_resolve_llm_client_for", resolve)
    monkeypatch.setattr(rig_enforce.orchestrator, "_call_llm", complete)
    return SimpleNamespace(selected=selected, tool_results=tool_results)


def _rebuild_actuator_request(original: MCPRequest, arguments: dict[str, object]) -> MCPRequest:
    return MCPRequest(
        request_id=original.request_id,
        method=original.method,
        params={"name": TOOL_NAME, "arguments": arguments},
        protocol_version=original.protocol_version,
        caller_capabilities=original.caller_capabilities,
        caller_info=original.caller_info,
    )


async def _replay_captured_request(rig: IngressRig, request: MCPRequest) -> dict[str, Any]:
    socket = _RecorderSocket()
    await rig.agent.handle_mcp_request(socket, request)
    assert socket.sent, "agent produced no response"
    return socket.sent[-1]


async def _drive_mcp_and_capture(rig: IngressRig) -> dict[str, Any]:
    payload = await _post_mcp(rig, FINAL_ARGUMENTS)
    return payload


async def _assert_governed_enforcement(
    rig: IngressRig,
    *,
    channel: str,
    effects: list[dict[str, object]],
    captured: list[MCPRequest],
) -> None:
    assert len(rig.warden.calls) == 1
    evidence = rig.warden.calls[0]["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["channel"] == channel
    assert evidence["agent_id"] == rig.agent_id
    assert evidence["runtime_id"] == rig.runtime_id
    assert "private patient value" not in json.dumps(rig.warden.calls)
    assert effects == [FINAL_ARGUMENTS]
    assert len(captured) == 1
    permit = captured[0].caller_capabilities["astraldeep.lets/v1"]
    assert permit["owner_id"] == rig.owner_id
    assert permit["binding_id"] == rig.binding_id
    assert permit["runtime_generation"] == rig.runtime_generation
    operation_id = rig.warden.calls[0]["operation_id"]
    effect = await asyncio.to_thread(_protected_effect, rig, operation_id)
    assert effect is not None
    assert effect.status.value == "receipt_received"
    assert effect.audit_correlation_id == evidence["audit_correlation_id"]
    assert effect.receipt_digest is not None


def _protected_effect(rig: IngressRig, operation_id: str) -> Any:
    with rig.plane.transaction() as transaction:
        return rig.repository.get_protected_effect(
            transaction,
            owner_id=rig.owner_id,
            operation_id=operation_id,
        )


def _revoke_binding(rig: IngressRig) -> None:
    with rig.plane.transaction() as transaction:
        binding = rig.repository.get_binding(
            transaction, owner_id=rig.owner_id, binding_id=rig.binding_id
        )
        assert binding is not None
        rig.repository.transition_binding(
            transaction,
            replace(
                binding,
                state=AuthorityBindingState.REVOKED,
                updated_at=datetime.now(UTC),
                version=binding.version + 1,
            ),
            expected_state=AuthorityBindingState.ACTIVE,
            expected_version=binding.version,
        )


def _governed_refusal(payload: dict[str, Any]) -> bool:
    result = payload.get("result") or {}
    if result.get("isError") is not True:
        return False
    text = json.dumps(payload)
    return "Governed tool authorization was refused" in text


@pytest.mark.asyncio
async def test_mcp_http_ingress_reaches_receipt_protected_effect(rig_enforce) -> None:
    rig = rig_enforce
    payload = await _drive_mcp_and_capture(rig)
    result = payload.get("result") or {}
    assert result.get("isError") is not True, payload
    await _assert_governed_enforcement(
        rig,
        channel="mcp",
        effects=rig.executor.read_effects(),
        captured=rig.executor.captured,
    )
    assert rig.executor.replay_store().status().claim_sequence == 1


@pytest.mark.asyncio
async def test_a2a_http_ingress_reaches_receipt_protected_effect(rig_enforce) -> None:
    rig = rig_enforce
    payload = await _post_a2a(rig, FINAL_ARGUMENTS)
    assert "Error:" not in _a2a_error(payload), payload
    await _assert_governed_enforcement(
        rig,
        channel="a2a",
        effects=rig.executor.read_effects(),
        captured=rig.executor.captured,
    )
    assert rig.executor.replay_store().status().claim_sequence == 1


@pytest.mark.asyncio
async def test_a2a_transport_numbers_cannot_reach_protected_effects(rig_enforce) -> None:
    rig = rig_enforce
    payload = await _post_a2a(rig, {"query": "private patient value", "limit": 5})
    assert "Error: Governed tool authorization was refused" in _a2a_error(payload), (
        payload
    )
    assert rig.executor.read_effects() == []
    assert rig.executor.captured == []
    assert rig.warden.calls == []


@pytest.mark.asyncio
async def test_accepted_voice_turn_reaches_receipt_protected_effect(
    rig_enforce, user_skills_disabled, voice_model
) -> None:
    rig = rig_enforce
    socket, chat_id, turn, final = await _accepted_voice_turn(rig)
    assert not [
        frame
        for frame in socket.sent
        if frame.get("type") == "voice_local_final_rejected"
    ], socket.sent
    acknowledgement = _frame_of(socket, "user_message_acked")
    assert acknowledgement["voice_turn_id"] == turn.turn_id
    assert acknowledgement["chat_id"] == chat_id
    assert acknowledgement["submission_id"] == final["submission_id"]
    assert turn.message_id is not None
    assert voice_model.selected == [TOOL_NAME]
    await _assert_governed_enforcement(
        rig,
        channel="websocket",
        effects=rig.executor.read_effects(),
        captured=rig.executor.captured,
    )
    assert rig.executor.replay_store().status().claim_sequence == 1


@pytest.mark.asyncio
async def test_replayed_accepted_voice_final_has_no_new_protected_effect(
    rig_enforce, user_skills_disabled, voice_model
) -> None:
    rig = rig_enforce
    socket, _chat_id, _turn, final = await _accepted_voice_turn(rig)
    assert not [
        frame
        for frame in socket.sent
        if frame.get("type") == "voice_local_final_rejected"
    ], socket.sent
    assert len(rig.executor.read_effects()) == 1
    captured = list(rig.executor.captured)
    authorizations = list(rig.warden.calls)

    await _submit_voice_frame(rig, socket, final)

    assert len(rig.executor.read_effects()) == 1
    assert rig.executor.captured == captured
    assert rig.warden.calls == authorizations
    assert rig.executor.replay_store().status().claim_sequence == 1
    assert voice_model.selected == [TOOL_NAME]


@pytest.mark.asyncio
async def test_revoked_authority_denies_accepted_voice_with_zero_effects(
    rig_enforce, user_skills_disabled, voice_model
) -> None:
    rig = rig_enforce
    await asyncio.to_thread(_revoke_binding, rig)

    socket, _chat_id, turn, _final = await _accepted_voice_turn(rig)

    assert not [
        frame
        for frame in socket.sent
        if frame.get("type") == "voice_local_final_rejected"
    ], socket.sent
    assert _frame_of(socket, "user_message_acked")["voice_turn_id"] == turn.turn_id
    assert voice_model.selected == [TOOL_NAME]
    assert len(voice_model.tool_results) == 1
    assert '"status": "error"' in voice_model.tool_results[0]
    assert '"code": "TOOL_FAILED"' in voice_model.tool_results[0]
    assert rig.executor.read_effects() == []
    assert rig.executor.captured == []
    assert rig.warden.calls == []


@pytest.mark.asyncio
async def test_revoked_binding_denies_ingress_with_zero_effects(rig_enforce) -> None:
    rig = rig_enforce
    await asyncio.to_thread(_revoke_binding, rig)
    payload = await _drive_mcp_and_capture(rig)
    assert _governed_refusal(payload), payload
    assert rig.executor.read_effects() == []
    assert rig.executor.captured == []
    assert rig.warden.calls == []


@pytest.mark.asyncio
async def test_stale_runtime_generation_denies_ingress_with_zero_effects(
    rig_enforce,
) -> None:
    rig = rig_enforce
    rig.orchestrator.register_governed_dispatch_runtime(
        DispatchRuntime(
            owner_id=rig.owner_id,
            agent_id=rig.agent_id,
            population="server_dynamic",
            runtime_id=rig.runtime_id,
            runtime_generation=rig.runtime_generation + 1,
            executor_audience=rig.audience,
            executor_conformant=True,
            dispatch_posture="protected_executor",
        )
    )
    payload = await _drive_mcp_and_capture(rig)
    assert _governed_refusal(payload), payload
    assert rig.executor.read_effects() == []
    assert rig.executor.captured == []
    assert rig.warden.calls == []


@pytest.mark.asyncio
async def test_replayed_permit_is_refused_at_executor_with_zero_new_effects(
    rig_enforce,
) -> None:
    rig = rig_enforce
    await _drive_mcp_and_capture(rig)
    effects_after_first = rig.executor.read_effects()
    assert len(effects_after_first) == 1
    original = rig.executor.captured[0]
    replayed_arguments = {
        key: value
        for key, value in original.params["arguments"].items()
        if key != "_runtime"
    }
    replayed = await _replay_captured_request(
        rig, _rebuild_actuator_request(original, replayed_arguments)
    )
    assert replayed.get("error", {}).get("message") == "receipt_replayed", replayed
    assert rig.executor.read_effects() == effects_after_first
    assert rig.executor.replay_store().status().claim_sequence == 1


@pytest.mark.asyncio
async def test_mutated_final_arguments_are_refused_at_executor(rig_enforce) -> None:
    rig = rig_enforce
    await _drive_mcp_and_capture(rig)
    original = rig.executor.captured[0]
    original_arguments = {
        key: value
        for key, value in original.params["arguments"].items()
        if key != "_runtime"
    }
    mutated = _rebuild_actuator_request(
        original,
        {**original_arguments, "limit": "999"},
    )
    tampered = await _replay_captured_request(rig, mutated)
    assert (
        tampered.get("error", {}).get("message") == "executor_arguments_mutated"
    ), tampered
    assert rig.executor.read_effects() == [FINAL_ARGUMENTS]


@pytest.mark.asyncio
async def test_unavailable_warden_denies_ingress_with_zero_effects(rig_enforce) -> None:
    rig = rig_enforce
    rig.warden.failure = LetsClientBoundaryError("warden_unreachable")
    try:
        payload = await _drive_mcp_and_capture(rig)
    finally:
        rig.warden.failure = None
    assert _governed_refusal(payload), payload
    assert rig.executor.read_effects() == []
    assert rig.executor.captured == []
    assert len(rig.warden.calls) == 1


@pytest.mark.asyncio
async def test_off_mode_dispatches_ungoverned_without_warden_or_claims(rig_off) -> None:
    rig = rig_off
    payload = await _drive_mcp_and_capture(rig)
    result = payload.get("result") or {}
    assert result.get("isError") is not True, payload
    assert rig.warden.calls == []
    assert len(rig.executor.captured) == 1
    assert rig.executor.captured[0].caller_capabilities == {}
    assert rig.executor.read_effects() == [FINAL_ARGUMENTS]
    assert rig.orchestrator.governed_final_dispatch.mode == "off"


@pytest.mark.asyncio
async def test_shadow_mode_requests_receipt_but_leaves_effect_ungoverned(
    ingress_plane,
    ingress_identity,
    tmp_path,
    monkeypatch,
    orchestrator_factory,
) -> None:
    identity = ingress_identity
    orchestrator = await asyncio.to_thread(orchestrator_factory)
    rig = await _build_rig(
        ingress_plane=orchestrator.runtime_composition.plane.runtime,
        orchestrator=orchestrator,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        mode="shadow",
    )
    rig.identity = identity
    payload = await _drive_mcp_and_capture(rig)
    result = payload.get("result") or {}
    assert result.get("isError") is not True, payload
    assert len(rig.warden.calls) == 1
    assert len(rig.executor.captured) == 1
    assert rig.executor.captured[0].caller_capabilities == {}
    assert rig.executor.read_effects() == [FINAL_ARGUMENTS]
    effect = await asyncio.to_thread(
        _protected_effect,
        rig,
        str(rig.warden.calls[0]["operation_id"]),
    )
    assert effect is None


@pytest.mark.asyncio
async def test_external_mediated_only_population_is_not_governed(
    ingress_plane,
    ingress_identity,
    tmp_path,
    monkeypatch,
    orchestrator_factory,
) -> None:
    identity = ingress_identity
    orchestrator = await asyncio.to_thread(orchestrator_factory)
    ingress_plane = orchestrator.runtime_composition.plane.runtime
    owner_id = _unique("ingress-owner")
    agent_id = _unique("ingress-agent")
    audience = _unique("ingress-executor")
    signer = Ed25519Signer.generate("warden-ingress")
    operator = Ed25519Signer.generate("operator-ingress")
    manifest, policy = _signed_manifest(signer, operator)
    warden = IngressWarden(signer, policy)
    config = LetsHostConfig(
        master_enabled=True,
        mode="enforce",  # type: ignore[arg-type]
        environment="test",
        governed_cohorts=("server_dynamic",),
        governed_agent_allowlist=(),
    )
    gateway = LetsAuthorizationGateway(
        config,
        warden,  # type: ignore[arg-type]
        effect_coordinator=PlaneProtectedEffectCoordinator(
            plane=ingress_plane,
            repository=ingress_plane.repositories.authority,
        ),
    )
    orchestrator.bind_governed_final_dispatch(
        gateway=gateway,
        plane=ingress_plane,
        authority_repository=ingress_plane.repositories.authority,
    )
    orchestrator.register_governed_dispatch_runtime(
        DispatchRuntime(
            owner_id=owner_id,
            agent_id=agent_id,
            population="external",
            runtime_id=_unique("ingress-runtime"),
            runtime_generation=3,
            executor_audience=None,
            executor_conformant=False,
            dispatch_posture="dispatch_mediated_only",
        )
    )
    executor = GeneratedExecutor(tmp_path, f"ingress_{uuid.uuid4().hex[:8]}", agent_id)
    executor.install(
        monkeypatch,
        mode_name="off",
        warden=warden,
        operator=operator,
        manifest=manifest,
        policy=policy,
        owner_id=owner_id,
        binding_id="unused",
        runtime_id="unused",
        runtime_generation=3,
        audience=audience,
    )
    rig = IngressRig(
        plane=ingress_plane,
        orchestrator=orchestrator,
        identity=identity,
        warden=warden,
        gateway=gateway,
        executor=executor,
        owner_id=owner_id,
        agent_id=agent_id,
        runtime_id="unused",
        runtime_generation=3,
        audience="unused",
        binding_id="unused",
        repository=ingress_plane.repositories.authority,
    )
    rig.wire_orchestrator()
    payload = await _drive_mcp_and_capture(rig)
    result = payload.get("result") or {}
    assert result.get("isError") is not True, payload
    assert rig.warden.calls == []
    assert len(rig.executor.captured) == 1
    assert rig.executor.captured[0].caller_capabilities == {}
    assert rig.executor.read_effects() == [FINAL_ARGUMENTS]
