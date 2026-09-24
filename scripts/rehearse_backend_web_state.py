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
from uuid import UUID

ID = re.compile(r"^[0-9a-f]{32}$")
MESSAGE = "Synthetic conversation retained across the baseline upgrade."


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


def exercise(orch, *, owner: str, checkpoint: dict | None, append: bool = False) -> dict:
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
        checkpoint = {"schema_version": 1, "owner": owner, "chat_id": chat_id,
                      "credential_sha256": _digest(key),
                      "audit_event_ids": [first.event_id, second.event_id]}
        key = ""
    if (set(checkpoint) != {"schema_version", "owner", "chat_id", "credential_sha256", "audit_event_ids"}
            or type(checkpoint["schema_version"]) is not int or checkpoint["schema_version"] != 1 or checkpoint["owner"] != owner
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
    rows, cursor = orch.audit_repo.list_for_user(owner, limit=1000)
    observed = {row.event_id for row in rows}
    if (cursor is not None or not set(checkpoint["audit_event_ids"]) <= observed
            or orch.audit_repo.verify_chain(owner) is not None):
        raise ValueError("retained authenticated audit chain failed verification")
    if any(orch.audit_repo.get_for_user(foreign, event_id) is not None
           for event_id in checkpoint["audit_event_ids"]):
        raise ValueError("foreign audit owner was admitted")
    appended = None
    if append:
        appended = orch.audit_repo.insert(_event(owner, "candidate_continuity")).event_id
        after, cursor = orch.audit_repo.list_for_user(owner, limit=1000)
        if (cursor is not None or {row.event_id for row in after} != observed | {appended}
                or orch.audit_repo.verify_chain(owner) is not None):
            raise ValueError("candidate audit append did not preserve the authenticated chain")
    return {"checkpoint": checkpoint, "credential_decryption": "pass",
            "conversation_retention": "pass", "owner_denials": "pass",
            "authenticated_audit_continuity": "pass", "appended_event_id": appended,
            "synthetic_only": True, "real_provider_called": False, "release_authorized": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qualification-id", required=True)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--append", action="store_true")
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
        result = exercise(orch, owner=owner, checkpoint=checkpoint, append=args.append)
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
