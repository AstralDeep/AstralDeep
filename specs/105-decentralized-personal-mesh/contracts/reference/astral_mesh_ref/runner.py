"""Runs one vector case through the reference and returns either an ok value or an error code.
The generator, the pytest suite and the kernel's own harness all interpret the same case format through the operations listed here.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

from . import (
    attestation,
    consent,
    crypto,
    event,
    fold,
    keys,
    labels,
    pairing,
    policy,
    roster,
    surface,
    sync,
    ticket,
)
from .cj import b64url_decode, b64url_encode, canonical, strict_loads
from .errors import MeshError

Handler = Callable[[dict[str, Any]], Any]
OPS: dict[str, Handler] = {}


def op(name: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        OPS[name] = fn
        return fn

    return register


def run(kind: str, case_input: dict[str, Any]) -> dict[str, Any]:
    try:
        return {"ok": OPS[kind](case_input)}
    except MeshError as exc:
        return {"error": exc.code}


def _hex(value: str) -> bytes:
    return bytes.fromhex(value)


@op("canonical")
def _canonical(i: dict[str, Any]) -> Any:
    data = canonical(strict_loads(i["json"]))
    return {
        "canonical": data.decode("utf-8"),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


@op("b64url")
def _b64(i: dict[str, Any]) -> Any:
    return {"hex": b64url_decode(i["text"]).hex()}


@op("keypair")
def _keypair(i: dict[str, Any]) -> Any:
    identity = crypto.p256_public(int(i["identity_scalar_hex"], 16))
    return {
        "device_id": crypto.device_id(identity),
        "identity": b64url_encode(identity),
        "ops": b64url_encode(crypto.ed25519_public(_hex(i["ops_seed_hex"]))),
        "presence": b64url_encode(
            crypto.p256_public(int(i["presence_scalar_hex"], 16))
        ),
        "wrap": b64url_encode(crypto.x25519_public(_hex(i["wrap_private_hex"]))),
    }


@op("sign")
def _sign(i: dict[str, Any]) -> Any:
    secret = (
        _hex(i["secret_hex"]) if i["alg"] == "ed25519" else int(i["secret_hex"], 16)
    )
    return {"sig": crypto.sign_detached(i["alg"], secret, i["context"], i["object"])}


@op("verify")
def _verify(i: dict[str, Any]) -> Any:
    ok = crypto.verify_detached(
        i["alg"], b64url_decode(i["public"]), i["context"], i["object"], i["sig"]
    )
    return {"valid": ok}


@op("aead_seal")
def _aead_seal(i: dict[str, Any]) -> Any:
    return {
        "ct_hex": crypto.aead_seal(
            _hex(i["key_hex"]),
            _hex(i["nonce_hex"]),
            _hex(i["plaintext_hex"]),
            _hex(i["aad_hex"]),
        ).hex()
    }


@op("aead_open")
def _aead_open(i: dict[str, Any]) -> Any:
    return {
        "plaintext_hex": crypto.aead_open(
            _hex(i["key_hex"]),
            _hex(i["nonce_hex"]),
            _hex(i["ct_hex"]),
            _hex(i["aad_hex"]),
        ).hex()
    }


@op("hkdf")
def _hkdf(i: dict[str, Any]) -> Any:
    salt = _hex(i["salt_hex"]) if i.get("salt_hex") is not None else None
    return {
        "okm_hex": crypto.hkdf(
            _hex(i["ikm_hex"]), i["info"].encode(), salt, i["length"]
        ).hex()
    }


@op("wrap")
def _wrap(i: dict[str, Any]) -> Any:
    wrapped = crypto.wrap_key(
        _hex(i["recipient_public_hex"]),
        _hex(i["secret_hex"]),
        i["aad"],
        _hex(i["ephemeral_hex"]),
        _hex(i["nonce_hex"]),
    )
    return {"wrapped": wrapped}


@op("unwrap")
def _unwrap(i: dict[str, Any]) -> Any:
    return {
        "secret_hex": crypto.unwrap_key(
            _hex(i["recipient_private_hex"]), i["wrapped"], i["aad"]
        ).hex()
    }


@op("seal_event")
def _seal_event(i: dict[str, Any]) -> Any:
    envelope = event.seal_event(
        _hex(i["ops_seed_hex"]),
        seq=i["seq"],
        prev=i["prev"],
        after=i["after"],
        clock=i["clock"],
        scope=i["scope"],
        type=i["type"],
        labels=i["labels"],
        key=i["key"],
        content_key=_hex(i["content_key_hex"]),
        body=i["body"],
        epoch_id=i["epoch_id"],
        epoch_key=_hex(i["epoch_key_hex"]),
        body_nonce=_hex(i["body_nonce_hex"]),
        header_nonce=_hex(i["header_nonce_hex"]),
    )
    header, _ = event.open_header(envelope, {i["epoch_id"]: _hex(i["epoch_key_hex"])})
    return {"envelope": envelope, "event_id": event.event_id(header), "header": header}


@op("open_event")
def _open_event(i: dict[str, Any]) -> Any:
    epoch_keys = {k: _hex(v) for k, v in i["epoch_keys"].items()}
    content_keys = {k: _hex(v) for k, v in i["content_keys"].items()}
    header, body = event.open_event(
        i["envelope"], epoch_keys, content_keys, now_ms=i.get("now_ms")
    )
    return {"body": body, "event_id": event.event_id(header), "header": header}


@op("feed_replay")
def _feed_replay(i: dict[str, Any]) -> Any:
    store = sync.EventStore()
    results = []
    for step in i["steps"]:
        status = i["statuses"].get(step["header"]["feed"], {"state": "unknown"})
        try:
            results.append(store.accept(step["header"], status, now_ms=i.get("now_ms")))
        except MeshError as exc:
            results.append(exc.code)
    return {"forked": sorted(store.forked), "heads": store.heads(), "results": results}


@op("roster_fold")
def _roster_fold(i: dict[str, Any]) -> Any:
    state = roster.fold(i["entries"], i.get("mesh"), now_s=i.get("now_s"))
    keep = [
        "mesh",
        "heads",
        "count",
        "members",
        "removed",
        "unattested",
        "stale_attestation",
        "principal",
        "issuers",
        "phi_authorities",
        "phi_members",
        "epochs",
        "current_epoch",
        "stop",
        "resumed",
        "stopped",
        "void",
        "rejected",
        "pending",
        "feeds",
    ]
    return {k: state.data[k] for k in keep}


@op("roster_shape")
def _roster_shape(i: dict[str, Any]) -> Any:
    roster.check_entry_shape(i["entry"])
    return {"id": roster.entry_id(i["entry"])}


@op("attestation_verify")
def _attestation_verify(i: dict[str, Any]) -> Any:
    if i["att"].get("kind") == "phi":
        return attestation.verify_phi(
            i["att"], set(i["authorities"]), now_s=i.get("now_s")
        )
    return attestation.verify_identity(
        i["att"],
        b64url_decode(i["identity"]),
        i["mesh"],
        i["issuers"],
        entry_time_s=i["entry_time_s"],
        now_s=i.get("now_s"),
    )


@op("recovery_encode")
def _recovery_encode(i: dict[str, Any]) -> Any:
    secret = _hex(i["secret_hex"])
    derived = keys.derive_recovery(secret)
    return {
        "sign_public": b64url_encode(derived["sign_public"]),
        "sign_seed_hex": derived["sign_seed"].hex(),
        "wrap_private_hex": derived["wrap_private"].hex(),
        "wrap_public": b64url_encode(derived["wrap_public"]),
        "words": keys.encode_words(secret),
    }


@op("recovery_decode")
def _recovery_decode(i: dict[str, Any]) -> Any:
    return {"secret_hex": keys.decode_words(i["words"]).hex()}


@op("epoch_open")
def _epoch_open(i: dict[str, Any]) -> Any:
    if i["as"] == "recovery":
        return {
            "epoch_key_hex": keys.open_recovery_key(
                i["body"], _hex(i["private_hex"])
            ).hex()
        }
    return {
        "epoch_key_hex": keys.open_epoch_key(
            i["body"], i["device"], _hex(i["private_hex"])
        ).hex()
    }


@op("backup_open")
def _backup_open(i: dict[str, Any]) -> Any:
    return keys.open_backup(_hex(i["recovery_wrap_private_hex"]), i["backup"])


@op("policy_verify")
def _policy_verify(i: dict[str, Any]) -> Any:
    return {"id": policy.verify(i["policy"], i["identity_keys"], i["mesh"])}


@op("policy_effective")
def _policy_effective(i: dict[str, Any]) -> Any:
    return policy.effective(i["wrappers"], i["members"], i["mesh"])


@op("budget_input")
def _budget_input(i: dict[str, Any]) -> Any:
    return policy.budget_input(
        i["effective"], i["mesh"], i["now_s"], i["members"], i["stop"]
    )


@op("action_digest")
def _action_digest(i: dict[str, Any]) -> Any:
    return {"digest": ticket.action_digest(i["action"])}


@op("ticket_verify")
def _ticket_verify(i: dict[str, Any]) -> Any:
    claims = ticket.verify_ticket(
        i["ticket"],
        mesh=i["mesh"],
        roster_feeds=i["roster_feeds"],
        presence_keys=i["presence_keys"],
        stop=i["stop"],
        now_s=i["now_s"],
        action=i["action"],
        effect_kind=i["effect_kind"],
        consent_required=i["consent_required"],
        receipt_verifier=lambda _receipt: i["receipt_valid"],
    )
    return claims


@op("consent_verify")
def _consent_verify(i: dict[str, Any]) -> Any:
    consent.verify(i["consent"], i["kind"], i["digest"], i["presence_keys"])
    return {"valid": True}


@op("labels_join")
def _labels_join(i: dict[str, Any]) -> Any:
    return labels.join(i["labels"])


@op("labels_flow")
def _labels_flow(i: dict[str, Any]) -> Any:
    return labels.flow_rules(i["turn"], i["effect"])


@op("labels_gate")
def _labels_gate(i: dict[str, Any]) -> Any:
    return labels.gate_effect(i["state"])


def _surface_args(i: dict[str, Any]) -> dict[str, Any]:
    return {
        "vocabulary": set(i["vocabulary"]),
        "actionable": set(i["actionable"]),
        "turn": i["turn"],
    }


@op("surface_validate")
def _surface_validate(i: dict[str, Any]) -> Any:
    surface.validate_document(
        i["document"], producer=i.get("producer", "model"), **_surface_args(i)
    )
    return {"valid": True}


@op("surface_fold")
def _surface_fold(i: dict[str, Any]) -> Any:
    return surface.fold(i["events"], **_surface_args(i))


@op("surface_action")
def _surface_action(i: dict[str, Any]) -> Any:
    return surface.validate_action(i["document"], i["action"], i["used"])


@op("surface_bind")
def _surface_bind(i: dict[str, Any]) -> Any:
    return {"value": surface.resolve_binding(i["binding"], i["events"])}


@op("sync_plan")
def _sync_plan(i: dict[str, Any]) -> Any:
    wants = sync.plan_wants(i["local"], i["remote"])
    sends = sync.plan_sends(i["remote"], wants)
    return {
        "first_send": list(sends[0]) if sends else None,
        "last_send": list(sends[-1]) if sends else None,
        "send_count": len(sends),
        "wants": wants,
    }


@op("sync_bodies")
def _sync_bodies(i: dict[str, Any]) -> Any:
    return sync.bodies_for_peer(
        i["headers_by_body"], i["requested"], peer_in_phi_group=i["peer_in_phi_group"]
    )


@op("sync_message")
def _sync_message(i: dict[str, Any]) -> Any:
    return {"kind": sync.check_message(i["message"])}


@op("sync_frame")
def _sync_frame(i: dict[str, Any]) -> Any:
    if "frame_hex" in i:
        return {"message": sync.unframe(_hex(i["frame_hex"]))}
    return {"frame_hex": sync.frame(i["message"]).hex()}


@op("checkpoint_verify")
def _checkpoint_verify(i: dict[str, Any]) -> Any:
    sync.verify_checkpoint(i["checkpoint"], b64url_decode(i["identity"]), i["mesh"])
    store = sync.EventStore()
    for header in i.get("headers", []):
        store.accept(header, {"state": "member"})
    return {"status": sync.check_checkpoint_against(store, i["checkpoint"])}


@op("shortened")
def _shortened(i: dict[str, Any]) -> Any:
    return {"evidence": sync.detect_shortened(i["remote_heads"], i["checkpoints"])}


@op("fold_branches")
def _fold_branches(i: dict[str, Any]) -> Any:
    return fold.chat_branches(i["events"])


@op("fold_authority")
def _fold_authority(i: dict[str, Any]) -> Any:
    return fold.authority(i["events"], i["presence_keys"])


@op("fold_settings")
def _fold_settings(i: dict[str, Any]) -> Any:
    return fold.settings(i["events"])


@op("pairing_invite")
def _pairing_invite(i: dict[str, Any]) -> Any:
    pairing.verify_invite(
        i["invite"], b64url_decode(i["inviter_identity"]), i["mesh"], i["now_s"]
    )
    return {"valid": True}


@op("pairing_request")
def _pairing_request(i: dict[str, Any]) -> Any:
    return {"device": pairing.verify_request(i["request"], i["mesh"], i["nonce"])}


@op("backup_seal")
def _backup_seal(i: dict[str, Any]) -> Any:
    return {
        "backup": keys.seal_backup(
            _hex(i["recovery_wrap_public_hex"]),
            i["mesh"],
            i["payload"],
            _hex(i["ephemeral_hex"]),
            _hex(i["nonce_hex"]),
        )
    }


@op("labels_quarantine")
def _labels_quarantine(i: dict[str, Any]) -> Any:
    return labels.quarantined_value(i["value"], i["schema_ok"])
