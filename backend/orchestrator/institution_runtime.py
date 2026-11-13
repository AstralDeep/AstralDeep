"""Starts an institution node's per-owner mesh peers and gives each one its own request bridge with the institution's admission policy, so every owner's requests run through the node's own gates and never meet another owner's.
node_mesh_chat.start_for and stop_all call it, institution_api.py asks it to serve an owner when that owner opens a pairing offer, and the wake trigger attaches to each peer here.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

from orchestrator import node_mesh
from orchestrator.institution_meshes import InstitutionMeshes, Owner
from orchestrator.institution_policy import InstitutionPolicy
from orchestrator.push_wake import WakeTriggers, gateway_for
from orchestrator.node_mesh_chat import (
    STATE_FILE,
    BridgeSet,
    MeshChatBridge,
    NodePolicy,
    install,
    orchestrator_runner,
)
from orchestrator.node_mesh_calls import OrchestratorCalls
from shared import institution_profile, node_identity

logger = logging.getLogger("Orchestrator.InstitutionRuntime")


class InstitutionRuntime:
    def __init__(
        self,
        *,
        config: institution_profile.InstitutionConfig,
        base_dir: Path,
        history: Any,
        runner: Any,
        calls: Any,
        meshes: Optional[InstitutionMeshes] = None,
        settings: Optional[node_mesh.MeshSettings] = None,
        issuers: Optional[node_mesh.IssuerLoader] = None,
        binding: Any = None,
        wake: Optional[WakeTriggers] = None,
        policy: Optional[NodePolicy] = None,
        phi_active: Optional[Callable[[str], bool]] = None,
    ):
        self.config = config
        self.bridges = BridgeSet()
        self._base = Path(base_dir)
        self._history = history
        self._runner = runner
        self._calls = calls
        self._wake = wake
        self._policy = policy
        self._phi_active = phi_active
        mesh_settings = settings or node_mesh.MeshSettings.from_env()
        self.meshes = meshes or InstitutionMeshes(
            base_dir=self._base,
            settings=mesh_settings,
            issuers=issuers,
            binding=binding,
            mailbox_settings=mesh_settings.mailbox,
            on_started=self._serve,
        )

    @property
    def descriptor_fields(self) -> dict:
        return {
            "profile": node_identity.INSTITUTION,
            "institution_key": self.config.key,
            "phi_approval": self.config.current_approval(),
        }

    async def start(self) -> list:
        return await self.meshes.start()

    async def serve_owner(self, owner: Owner) -> node_mesh.NodeMesh:
        return await self.meshes.for_owner(owner)

    async def _serve(self, owner: Owner, mesh: node_mesh.NodeMesh) -> None:
        if not mesh.requests_supported:
            raise node_mesh.MeshUnavailable("requests_unsupported")
        kwargs: dict = {}
        if self._phi_active is not None:
            kwargs["phi_active"] = self._phi_active
        bridge = MeshChatBridge(
            mesh,
            owner_id=owner.partition,
            history=self._history,
            runner=self._runner,
            calls=self._calls,
            state_path=self.meshes.owner_dir(owner) / STATE_FILE,
            policy=self._policy,
            admission=InstitutionPolicy(mesh).check,
            **kwargs,
        )
        await bridge.start()
        self.bridges.add(owner.partition, bridge)
        if self._wake is not None:
            self._wake.attach(mesh, lambda: owner.partition)

    async def stop(self) -> None:
        if self._wake is not None:
            await self._wake.stop()
        await self.bridges.stop()
        await self.meshes.stop()


_current: Optional[InstitutionRuntime] = None


def current() -> Optional[InstitutionRuntime]:
    return _current


def set_current(runtime: Optional[InstitutionRuntime]) -> None:
    global _current
    _current = runtime


async def start_for(orch: Any) -> Optional[InstitutionRuntime]:
    config = institution_profile.enforce()
    try:
        node_mesh.load_binding()
    except node_mesh.MeshUnavailable as exc:
        logger.warning("institution_mesh_unavailable: %s", exc.code)
        return None
    runtime = InstitutionRuntime(
        config=config,
        base_dir=node_identity.data_dir(),
        history=orch.history,
        runner=orchestrator_runner(orch),
        calls=OrchestratorCalls(orch),
        wake=WakeTriggers(gateway) if (gateway := gateway_for(orch)) is not None else None,
    )
    await runtime.start()
    set_current(runtime)
    install(runtime.bridges)
    return runtime


async def shutdown() -> None:
    runtime = _current
    set_current(None)
    if runtime is not None:
        await runtime.stop()
