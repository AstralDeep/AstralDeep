#!/usr/bin/env python3
"""Seeds and verifies synthetic encrypted/authenticated state across a backend image
upgrade inside the exact baseline/candidate images, using the product's own
audit/orchestrator stores rather than direct writes.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

ID = re.compile(r"^[0-9a-f]{32}$")
MESSAGE = "Synthetic conversation retained across the baseline upgrade."
BLOB = b"Synthetic paired blob recovery verification.\n"


def require_isolated_target(qualification_id: str, environment: dict[str, str]) -> str:
    if not ID.fullmatch(qualification_id):
        raise ValueError("qualification ID must be exact")
    target = urlsplit(environment.get("DATABASE_URL", ""))
    options = parse_qs(target.query, strict_parsing=True)
    if (target.scheme not in {"postgres", "postgresql"}
            or target.hostname != f"ad-bwq-{qualification_id}-pg"
            or target.path != f"/astralplane_qualification_{qualification_id}"
            or target.port not in {None, 5432} or target.username != "qualification"
            or not target.password or target.fragment
            or options != {"options": [f"-csearch_path=astralplane_fixture_{qualification_id},pg_catalog"]}
            or environment.get("ASTRAL_ENV") != "production"
            or environment.get("USE_MOCK_AUTH", "false").lower() != "false"
            or not environment.get("CREDENTIAL_ENCRYPTION_KEY")
            or not environment.get("AUDIT_HMAC_SECRET")):
        raise ValueError("explicit isolated production-posture target and rehearsal keys required")
    return f"__verif__{qualification_id}_recovery"


def _load_checkpoint(path: Path) -> dict:
    if (path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 65536
            or (os.name != "nt" and path.stat().st_mode & 0o077)):
        raise ValueError("checkpoint must be a bounded private regular file")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("checkpoint has duplicate fields")
            result[key] = value
        return result
    document = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
    if not isinstance(document, dict) or not isinstance(document.get("checkpoint"), dict):
        raise ValueError("checkpoint document has an invalid shape")
    return document["checkpoint"]


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _event(owner: str, phase: str):
    from audit.schemas import AuditEventCreate

    return AuditEventCreate(
        actor_user_id=owner, auth_principal=owner, event_class="settings",
        action_type=f"qualification.{phase}", description="Synthetic recovery verification",
        correlation_id=phase, outcome="success", started_at=datetime.now(timezone.utc),
    )


def _seed_blob(orch, owner: str) -> dict:
    attachment_id = str(uuid4())
    storage_key = attachment_id + "/recovery.txt"
    coordinator = orch.runtime_composition.plane.attachment_materializations
    lease_id = "qualification-" + str(uuid4())
    begun = coordinator.begin_pending_materialization(
        attachment_id=attachment_id, owner_id=owner, filename="recovery.txt", category="text",
        extension="txt", storage_locator=owner + "/" + storage_key, storage_key=storage_key,
        max_bytes=len(BLOB), created_at=int(datetime.now(timezone.utc).timestamp() * 1000),
        lease_id=lease_id, lease_seconds=300,
    )
    if begun.pending is None:
        raise ValueError("synthetic blob was not newly admitted")
    version = begun.pending.lease_version
    staging = coordinator.open_pending_materialization_staging(
        owner_id=owner, attachment_id=attachment_id, lease_id=lease_id, expected_lease_version=version,
    )
    staged = staging.write_chunks((BLOB,))
    try:
        coordinator.publish_pending_materialization(
            staged=staged, owner_id=owner, attachment_id=attachment_id, lease_id=lease_id,
            expected_lease_version=version, content_type="text/plain",
        )
    except BaseException:
        staged.abort()
        raise
    return {"attachment_id": attachment_id, "owner_id": owner, "storage_key": storage_key,
            "size_bytes": len(BLOB), "sha256": hashlib.sha256(BLOB).hexdigest()}


def _verify_blob(orch, owner: str, blob: dict) -> None:
    from astralplane.errors import PlaneError

    try:
        if (not isinstance(blob, dict)
                or set(blob) != {"attachment_id", "owner_id", "storage_key", "size_bytes", "sha256"}
                or blob["owner_id"] != owner or str(UUID(blob["attachment_id"])) != blob["attachment_id"]
                or blob["storage_key"] != blob["attachment_id"] + "/recovery.txt"
                or type(blob["size_bytes"]) is not int or blob["size_bytes"] != len(BLOB)
                or blob["sha256"] != hashlib.sha256(BLOB).hexdigest()):
            raise ValueError
        plane = orch.runtime_composition.plane
        with plane.runtime.transaction() as transaction:
            row = plane.repositories.artifacts.attachments.get(
                transaction, owner_id=owner, attachment_id=blob["attachment_id"],
            )
            foreign = plane.repositories.artifacts.attachments.get(
                transaction, owner_id=owner + "_foreign", attachment_id=blob["attachment_id"],
            )
        if (row is None or foreign is not None or row.owner_id != owner
                or row.filename != "recovery.txt" or row.size_bytes != blob["size_bytes"]
                or row.sha256 != blob["sha256"] or row.storage_locator != owner + "/" + blob["storage_key"]):
            raise ValueError
        observed = b"".join(plane.blobs.iter_chunks(
            owner_id=owner, key=blob["storage_key"], max_bytes=len(BLOB),
            expected_size_bytes=blob["size_bytes"], expected_sha256=blob["sha256"],
        ))
        if observed != BLOB:
            raise ValueError
        try:
            tuple(plane.blobs.iter_chunks(
                owner_id=owner + "_foreign", key=blob["storage_key"], max_bytes=len(BLOB),
                expected_size_bytes=blob["size_bytes"], expected_sha256=blob["sha256"],
            ))
        except PlaneError as exc:
            if exc.code != "blob_not_found":
                raise
        else:
            raise ValueError
    except Exception:
        raise ValueError("retained owner-bound blob failed verification") from None


def exercise(orch, *, owner: str, checkpoint: dict | None, append: bool = False,
             forbidden_audit_event: str | None = None) -> dict:
    plane = orch.runtime_composition.plane
    compatibility = plane.runtime.inspect_baseline_compatibility()
    if not compatibility.compatible or compatibility.empty:
        raise ValueError("rehearsal Plane state is not initialized")
    schema = {"revision": compatibility.observed_revision,
              "migration_sha256": plane.runtime.inspect_compatibility().migration_digest}
    foreign = owner + "_foreign"
    if checkpoint is None:
        if orch._llm_store.get_sync(owner) is not None or orch.audit_repo.list_for_user(owner)[0]:
            raise ValueError("synthetic rehearsal owner already contains state")
        key = secrets.token_urlsafe(48)
        orch._llm_store.set_sync(owner, provider="custom", base_url="https://rehearsal.invalid/v1",
                                 model="synthetic-retention", api_key=key)
        chat_id = orch.history.create_chat(user_id=owner)
        orch.history.add_message(chat_id, "user", MESSAGE, user_id=owner)
        first = orch.audit_repo.insert(_event(owner, "baseline_created"))
        second = orch.audit_repo.insert(_event(owner, "baseline_retained"))
        checkpoint = {"schema_version": 2, "owner": owner, "chat_id": chat_id,
                      "credential_sha256": _digest(key),
                      "audit_event_ids": [first.event_id, second.event_id], "blob": _seed_blob(orch, owner)}
        key = ""
    if (set(checkpoint) != {"schema_version", "owner", "chat_id", "credential_sha256", "audit_event_ids", "blob"}
            or type(checkpoint["schema_version"]) is not int or checkpoint["schema_version"] != 2 or checkpoint["owner"] != owner
            or not isinstance(checkpoint["chat_id"], str) or not checkpoint["chat_id"]
            or not isinstance(checkpoint["credential_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", checkpoint["credential_sha256"])
            or not isinstance(checkpoint["audit_event_ids"], list)
            or not 2 <= len(checkpoint["audit_event_ids"]) <= 100
            or any(not isinstance(event_id, str) or str(UUID(event_id)) != event_id
                   for event_id in checkpoint["audit_event_ids"])
            or len(set(checkpoint["audit_event_ids"])) != len(checkpoint["audit_event_ids"])):
        raise ValueError("checkpoint is not bound to the exact synthetic owner")
    config = orch._llm_store.get_sync(owner)
    if config is None or _digest(config.api_key) != checkpoint["credential_sha256"]:
        raise ValueError("retained credential could not be decrypted with the original rehearsal key")
    if orch._llm_store.get_sync(foreign) is not None:
        raise ValueError("foreign credential owner was admitted")
    chat_id = checkpoint["chat_id"]
    chat = orch.history.get_chat(chat_id, user_id=owner)
    if chat is None or not any(row["role"] == "user" and row["content"] == MESSAGE for row in chat["messages"]):
        raise ValueError("retained conversation changed")
    if orch.history.get_chat(chat_id, user_id=foreign) is not None:
        raise ValueError("foreign conversation owner was admitted")
    _verify_blob(orch, owner, checkpoint["blob"])
    rows, cursor = orch.audit_repo.list_for_user(owner, limit=1000)
    observed = {row.event_id for row in rows}
    if (cursor is not None or not set(checkpoint["audit_event_ids"]) <= observed
            or orch.audit_repo.verify_chain(owner) is not None):
        raise ValueError("retained authenticated audit chain failed verification")
    if any(orch.audit_repo.get_for_user(foreign, event_id) is not None
           for event_id in checkpoint["audit_event_ids"]):
        raise ValueError("foreign audit owner was admitted")
    if forbidden_audit_event is not None:
        if (not isinstance(forbidden_audit_event, str) or str(UUID(forbidden_audit_event)) != forbidden_audit_event
                or forbidden_audit_event in observed or forbidden_audit_event in checkpoint["audit_event_ids"]):
            raise ValueError("restored chain retained the forbidden candidate-only audit event")
    appended = None
    if append:
        appended = orch.audit_repo.insert(_event(owner, "candidate_continuity")).event_id
        after, cursor = orch.audit_repo.list_for_user(owner, limit=1000)
        if (cursor is not None or {row.event_id for row in after} != observed | {appended}
                or orch.audit_repo.verify_chain(owner) is not None):
            raise ValueError("candidate audit append did not preserve the authenticated chain")
    return {"checkpoint": checkpoint, "schema": schema, "credential_decryption": "pass", "blob_retention": "pass",
            "conversation_retention": "pass", "owner_denials": "pass",
            "authenticated_audit_continuity": "pass", "appended_event_id": appended,
            "pre_append_audit_event_ids": sorted(observed),
            "synthetic_only": True, "real_provider_called": False, "release_authorized": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qualification-id", required=True)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--append", action="store_true")
    parser.add_argument("--forbid-audit-event")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    orch = None
    try:
        owner = require_isolated_target(args.qualification_id, dict(os.environ))
        if args.output.exists() or args.output.is_symlink():
            raise ValueError("evidence output must be new")
        checkpoint = None if args.checkpoint is None else _load_checkpoint(args.checkpoint)
        sys.path.insert(0, "/app/backend")
        from orchestrator.orchestrator import Orchestrator

        orch = Orchestrator()
        result = exercise(orch, owner=owner, checkpoint=checkpoint, append=args.append,
                          forbidden_audit_event=args.forbid_audit_event)
        asyncio.run(orch._close_started_services())
        orch = None
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(result, stream, indent=2, sort_keys=True)
            stream.write("\n")
        print(json.dumps({"synthetic_only": True, "status": "passed", "release_authorized": False}))
        return 0
    except Exception:
        print("synthetic state rehearsal failed; inspect its isolated namespace", file=sys.stderr)
        return 1
    finally:
        if orch is not None:
            asyncio.run(orch._close_started_services())


if __name__ == "__main__":
    raise SystemExit(main())
