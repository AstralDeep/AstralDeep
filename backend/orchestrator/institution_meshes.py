"""One mesh peer per owner for an institution node: each owner id (issuer and subject) gets its own kernel data directory, keys and mesh, so no owner's log, roster or keys are reachable from another's, and an optional shared mailbox serves all of them.
institution_runtime.py attaches a request bridge to each peer started here, and node_mesh.py provides the peer itself.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from orchestrator.node_mailbox import MailboxError, MailboxProcess, MailboxSettings
from orchestrator.node_mesh import IssuerLoader, MeshSettings, NodeMesh, NodeMeshError
from shared import node_identity

logger = logging.getLogger("Orchestrator.InstitutionMeshes")

OWNERS_DIR = "owners"
OWNER_FILE = "owner"
MAX_OWNERS_ENV = "ASTRAL_INSTITUTION_MAX_OWNERS"
DEFAULT_MAX_OWNERS = 1000


@dataclass(frozen=True)
class Owner:
    issuer: str
    subject: str

    @classmethod
    def verified(cls, issuer: Any, subject: Any) -> "Owner":
        try:
            node_identity.canonical_owner_id(issuer, subject)
        except node_identity.NodeIdentityError:
            raise NodeMeshError("owner_invalid", 400) from None
        return cls(node_identity.normalize_issuer(issuer), subject)

    @property
    def partition(self) -> str:
        return node_identity.canonical_owner_id(self.issuer, self.subject)

    @property
    def slot(self) -> str:
        return node_identity.owner_principal_hash(self.partition)[len("sha256:"):][:32]


OwnerHook = Callable[[Owner, NodeMesh], Awaitable[None]]


class InstitutionMeshes:
    def __init__(
        self,
        *,
        base_dir: Path,
        settings: Optional[MeshSettings] = None,
        issuers: Optional[IssuerLoader] = None,
        binding: Any = None,
        mailbox_settings: Optional[MailboxSettings] = None,
        max_owners: Optional[int] = None,
        on_started: Optional[OwnerHook] = None,
    ):
        self._base = Path(base_dir)
        base = settings or MeshSettings()
        self._settings = MeshSettings(
            label=base.label,
            relay=base.relay,
            loopback_only=base.loopback_only,
            local_discovery=base.local_discovery,
            internet_lookup=base.internet_lookup,
            sync_interval_ms=base.sync_interval_ms,
            mailbox=MailboxSettings(enabled=False),
        )
        self._issuers = issuers
        self._binding = binding
        self._mailbox_settings = mailbox_settings
        self._mailbox: Optional[MailboxProcess] = None
        self._max_owners = max_owners or DEFAULT_MAX_OWNERS
        self._on_started = on_started
        self._meshes: Dict[str, NodeMesh] = {}
        self._owners: Dict[str, Owner] = {}
        self._lock = asyncio.Lock()

    @property
    def owners(self) -> List[Owner]:
        return [self._owners[key] for key in sorted(self._owners)]

    def owner_dir(self, owner: Owner) -> Path:
        return self._base / OWNERS_DIR / owner.slot

    def current(self, owner: Owner) -> Optional[NodeMesh]:
        return self._meshes.get(owner.partition)

    async def start(self) -> List[Owner]:
        if self._mailbox_settings is not None and self._mailbox_settings.enabled:
            mailbox = MailboxProcess(self._mailbox_settings, data_dir=self._base / "mailbox")
            try:
                await mailbox.start()
                self._mailbox = mailbox
            except MailboxError as exc:
                logger.warning("institution_mailbox_unavailable: %s", exc)
        restored: List[Owner] = []
        for owner in self._stored_owners():
            try:
                await self.for_owner(owner)
                restored.append(owner)
            except Exception:
                logger.exception("institution_owner_restore_failed")
        return restored

    def _stored_owners(self) -> List[Owner]:
        root = self._base / OWNERS_DIR
        found: List[Owner] = []
        if not root.is_dir():
            return found
        for entry in sorted(root.iterdir()):
            try:
                record = json.loads((entry / OWNER_FILE).read_text(encoding="utf-8"))
                owner = Owner.verified(record["issuer"], record["subject"])
            except (OSError, ValueError, KeyError, TypeError, NodeMeshError):
                continue
            if owner.slot == entry.name:
                found.append(owner)
        return found

    async def for_owner(self, owner: Owner) -> NodeMesh:
        async with self._lock:
            mesh = self._meshes.get(owner.partition)
            if mesh is not None:
                return mesh
            if len(self._meshes) >= self._max_owners:
                raise NodeMeshError("institution_capacity", 429)
            if any(other.subject == owner.subject for other in self._owners.values()):
                raise NodeMeshError("owner_subject_conflict", 409)
            directory = self.owner_dir(owner)
            self._record_owner(directory, owner)
            kwargs: Dict[str, Any] = {"data_dir": directory, "settings": self._settings}
            if self._issuers is not None:
                kwargs["issuers"] = self._issuers
            if self._binding is not None:
                kwargs["binding"] = self._binding
            if self._mailbox is not None:
                kwargs["mailbox_address"] = self._mailbox.address
            mesh = NodeMesh(**kwargs)
            await mesh.start()
            self._meshes[owner.partition] = mesh
            self._owners[owner.partition] = owner
            if self._on_started is not None:
                try:
                    await self._on_started(owner, mesh)
                except BaseException:
                    self._meshes.pop(owner.partition, None)
                    self._owners.pop(owner.partition, None)
                    await mesh.stop()
                    raise
        return mesh

    @staticmethod
    def _record_owner(directory: Path, owner: Owner) -> None:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / OWNER_FILE
        if path.exists():
            return
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"issuer": owner.issuer, "subject": owner.subject}, sort_keys=True))

    async def stop(self) -> None:
        async with self._lock:
            meshes, self._meshes, self._owners = list(self._meshes.values()), {}, {}
            for mesh in meshes:
                try:
                    await mesh.stop()
                except Exception:
                    logger.exception("institution_mesh_stop_failed")
            if self._mailbox is not None:
                await self._mailbox.stop()
                self._mailbox = None
