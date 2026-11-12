"""The four-part ticket: a Biscuit capability chain, a LETS receipt binding, a presence-key consent and the labels carried in the call block.
verify_ticket is what an executor runs offline, and the receipt signature itself is checked by LETS through a callback.
"""

from __future__ import annotations

import base64
import datetime as dt
import re
from collections.abc import Callable
from typing import Any

from biscuit_auth import (
    Algorithm,
    AuthorizerBuilder,
    Biscuit,
    BiscuitBuilder,
    BlockBuilder,
    PrivateKey,
    PublicKey,
    UnverifiedBiscuit,
)

from . import consent as consent_mod
from . import crypto
from .cj import b64url_decode, b64url_encode
from .errors import MeshError
from .event import CONFIDENTIALITY, INTEGRITY

TICKET_FIELDS = {"capability", "consent", "receipt", "v"}
CONSENT_MAX_AGE_MS = 600_000
_LITERAL = re.compile(r"^[A-Za-z0-9_.:/@=+-]{1,200}$")
_FACT = re.compile(r"^([a-z_]+)\((.*)\);$")
_ARG = re.compile(r'^(?:"[A-Za-z0-9_.:/@=+-]{1,200}"|-?[0-9]{1,18})$')
_CHECK = re.compile(r"^check if .+;$")
RECEIPT_TYPE = "lets.receipt/v1"
BUDGET_DIMENSIONS = {
    "mesh_read": None,
    "mesh_write": "notes_write",
    "outward_read": None,
    "outside_effect": "outside_effect",
}
RETRIES = 20
AUTHORITY_FACTS = {"actor", "device", "feed", "mesh", "stop_counter"}
TURN_FACTS = {"actor", "turn"}
AGENT_FACTS = {"actor"}
CALL_FACTS = {"actor", "label", "source"}


def _lit(value: str) -> str:
    if not isinstance(value, str) or not _LITERAL.match(value):
        raise MeshError("ticket.bad_literal", str(value)[:40])
    return '"' + value + '"'


def rfc3339(epoch_s: int) -> str:
    return dt.datetime.fromtimestamp(epoch_s, dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def action_digest(action: dict[str, Any]) -> str:
    fields = {
        "agent",
        "args_digest",
        "device",
        "influence",
        "mesh",
        "tool",
        "tool_def",
        "turn",
        "v",
    }
    if (
        set(action) != fields
        or action["v"] != 1
        or action["influence"] != sorted(set(action["influence"]))
    ):
        raise MeshError("ticket.malformed_action", "action fields")
    return crypto.object_id(action)


def mint(
    ops_seed: bytes,
    *,
    mesh: str,
    device: str,
    stop_counter: int,
    expires_s: int,
    turn: str,
    tools: list[str],
    turn_expires_s: int,
    agents: list[str],
    tool: str,
    args_digest: str,
    action: str,
    integrity: str,
    confidentiality: str,
    sources: list[str],
) -> bytes:
    feed = b64url_encode(crypto.ed25519_public(ops_seed))
    root = PrivateKey.from_bytes(ops_seed, Algorithm.Ed25519)
    token = BiscuitBuilder(
        f"mesh({_lit(mesh)}); device({_lit(device)}); feed({_lit(feed)}); actor({_lit('device:' + device)}); "
        f"stop_counter({stop_counter}); "
        f"check if mesh_ctx($m), mesh($m); "
        f"check if stop_current($n), stop_counter($c), $n <= $c; "
        f"check if time($t), $t < {rfc3339(expires_s)};"
    ).build(root)
    tool_set = ", ".join(_lit(t) for t in sorted(tools))
    token = token.append(
        BlockBuilder(
            f"actor({_lit('turn:' + turn)}); turn({_lit(turn)}); "
            f"check if tool($t), {{{tool_set}}}.contains($t); "
            f"check if time($t), $t < {rfc3339(turn_expires_s)};"
        )
    )
    for n, agent in enumerate(agents):
        pin = (
            f" check if agent($a), $a == {_lit(agent)};" if n == len(agents) - 1 else ""
        )
        token = token.append(BlockBuilder(f"actor({_lit('agent:' + agent)});{pin}"))
    labels = f"label({_lit('integrity')}, {_lit(integrity)}); label({_lit('confidentiality')}, {_lit(confidentiality)}); "
    labels += "".join(f"source({_lit(s)}); " for s in sorted(sources))
    token = token.append(
        BlockBuilder(
            f"actor({_lit('call:' + action)}); {labels}"
            f"check if tool($t), $t == {_lit(tool)}; "
            f"check if args_digest($d), $d == {_lit(args_digest)}; "
            f"check if action_digest($d), $d == {_lit(action)};"
        )
    )
    return token.to_bytes()


def attenuate(token: bytes, root_public: bytes, block_code: str) -> bytes:
    biscuit = Biscuit.from_bytes(
        token, PublicKey.from_bytes(root_public, Algorithm.Ed25519)
    )
    return biscuit.append(BlockBuilder(block_code)).to_bytes()


def _unverified(token: bytes) -> UnverifiedBiscuit:
    try:
        return UnverifiedBiscuit.from_base64(
            base64.urlsafe_b64encode(token).decode("ascii")
        )
    except Exception as exc:
        raise MeshError("ticket.malformed", "not a Biscuit token") from exc


def _facts(source: str, allowed: set[str]) -> list[tuple[str, list[str]]]:
    facts = []
    for raw in source.strip().split("\n"):
        line = raw.strip()
        if not line:
            continue
        if _CHECK.match(line):
            continue
        match = _FACT.match(line)
        if not match or match.group(1) not in allowed:
            raise MeshError("ticket.wrong_block_shape", line[:60])
        args = [a.strip() for a in match.group(2).split(",")]
        if any(not _ARG.match(a) for a in args):
            raise MeshError("ticket.wrong_block_shape", line[:60])
        facts.append((match.group(1), [a.strip('"') for a in args]))
    return facts


def _one(facts: list[tuple[str, list[str]]], name: str) -> list[str]:
    found = [args for fact, args in facts if fact == name]
    if len(found) != 1:
        raise MeshError(
            "ticket.wrong_block_shape", f"exactly one {name} fact is required"
        )
    return found[0]


def parse_blocks(token: bytes) -> dict[str, Any]:
    biscuit = _unverified(token)
    count = biscuit.block_count()
    if count < 4:
        raise MeshError(
            "ticket.wrong_block_shape",
            "device, turn, agent and call blocks are required",
        )
    blocks = []
    for i in range(count):
        allowed = (
            AUTHORITY_FACTS
            if i == 0
            else TURN_FACTS
            if i == 1
            else CALL_FACTS
            if i == count - 1
            else AGENT_FACTS
        )
        blocks.append(_facts(biscuit.block_source(i), allowed))
    prefixes = ["device"] + ["turn"] + ["agent"] * (count - 3) + ["call"]
    for block, prefix in zip(blocks, prefixes, strict=True):
        if not _one(block, "actor")[0].startswith(prefix + ":"):
            raise MeshError("ticket.wrong_block_shape", f"expected a {prefix} link")
    authority = blocks[0]
    labels = blocks[-1]
    sources = sorted(args[0] for name, args in labels if name == "source")
    pairs = {args[0]: args[1] for name, args in labels if name == "label"}
    if (
        set(pairs) != {"integrity", "confidentiality"}
        or sum(1 for n, _ in labels if n == "label") != 2
    ):
        raise MeshError("ticket.label_missing", "the call block must carry both labels")
    if (
        pairs["integrity"] not in INTEGRITY
        or pairs["confidentiality"] not in CONFIDENTIALITY
        or any(not crypto.is_id(s) for s in sources)
    ):
        raise MeshError("ticket.label_missing", "label values")
    return {
        "device": _one(authority, "device")[0],
        "feed": _one(authority, "feed")[0],
        "mesh": _one(authority, "mesh")[0],
        "stop_counter": int(_one(authority, "stop_counter")[0]),
        "turn": _one(blocks[1], "turn")[0],
        "agents": [_one(b, "actor")[0].split(":", 1)[1] for b in blocks[2:-1]],
        "action": _one(blocks[-1], "actor")[0].split(":", 1)[1],
        "labels": {
            "confidentiality": pairs["confidentiality"],
            "integrity": pairs["integrity"],
            "sources": sources,
        },
    }


def verify_capability(
    token: bytes,
    *,
    mesh: str,
    roster_feeds: dict[str, dict[str, Any]],
    stop: int,
    now_s: int,
    tool: str,
    args_digest: str,
    action: str,
    agent: str,
) -> dict[str, Any]:
    parsed = parse_blocks(token)
    if parsed["mesh"] != mesh:
        raise MeshError("ticket.mesh_mismatch", parsed["mesh"])
    status = roster_feeds.get(parsed["feed"])
    if status is None:
        raise MeshError(
            "ticket.bad_root", "the root key is not an operations key on the roster"
        )
    if status["state"] != "member":
        raise MeshError("ticket.device_removed", status["device"])
    if status["device"] != parsed["device"]:
        raise MeshError("ticket.bad_root", "the device fact does not own the root key")
    if parsed["stop_counter"] < stop:
        raise MeshError("ticket.stopped", "issued before the latest stop")
    if parsed["action"] != action:
        raise MeshError("ticket.unauthorized", "the call block pins a different action")
    try:
        biscuit = Biscuit.from_bytes(
            token,
            PublicKey.from_bytes(b64url_decode(parsed["feed"]), Algorithm.Ed25519),
        )
    except Exception as exc:
        raise MeshError(
            "ticket.bad_root", "signature chain does not verify under the device key"
        ) from exc
    if any(
        biscuit.block_external_key(i) is not None for i in range(biscuit.block_count())
    ):
        raise MeshError("ticket.wrong_block_shape", "third-party blocks are refused")
    authorizer = AuthorizerBuilder(
        f"time({rfc3339(now_s)}); tool({_lit(tool)}); args_digest({_lit(args_digest)}); action_digest({_lit(action)}); "
        f"agent({_lit(agent)}); mesh_ctx({_lit(mesh)}); stop_current({stop}); allow if true;"
    ).build(biscuit)
    for _ in range(RETRIES):
        try:
            authorizer.authorize()
            return parsed
        except Exception as exc:
            if "execution limits" not in str(exc):
                raise MeshError(
                    "ticket.unauthorized",
                    str(exc).split("following checks failed:")[-1].strip(),
                ) from exc
    raise MeshError(
        "ticket.limits", "the Datalog run limit was reached on every attempt"
    )


def verify_ticket(
    ticket: Any,
    *,
    mesh: str,
    roster_feeds: dict[str, dict[str, Any]],
    presence_keys: dict[str, str],
    stop: int,
    now_s: int,
    action: dict[str, Any],
    effect_kind: str,
    consent_required: bool,
    receipt_verifier: Callable[[dict[str, Any]], bool],
) -> dict[str, Any]:
    if not isinstance(ticket, dict) or set(ticket) != TICKET_FIELDS or ticket["v"] != 1:
        raise MeshError("ticket.malformed", "fields")
    digest = action_digest(action)
    if action["mesh"] != mesh:
        raise MeshError("ticket.mesh_mismatch", action["mesh"])
    try:
        token = b64url_decode(ticket["capability"])
    except MeshError as exc:
        raise MeshError("ticket.malformed", "capability encoding") from exc
    claims = verify_capability(
        token,
        mesh=mesh,
        roster_feeds=roster_feeds,
        stop=stop,
        now_s=now_s,
        tool=action["tool"],
        args_digest=action["args_digest"],
        action=digest,
        agent=action["agent"],
    )
    if (
        claims["device"] != action["device"]
        or claims["turn"] != action["turn"]
        or claims["agents"][-1] != action["agent"]
    ):
        raise MeshError(
            "ticket.unauthorized", "action names a different device, turn or agent"
        )
    if claims["labels"]["sources"] != action["influence"]:
        raise MeshError(
            "ticket.unauthorized", "labels do not match the action's influence list"
        )
    if effect_kind not in BUDGET_DIMENSIONS:
        raise MeshError("labels.invalid", str(effect_kind)[:40])
    receipt = ticket["receipt"]
    if BUDGET_DIMENSIONS[effect_kind] is None:
        if receipt is not None:
            raise MeshError(
                "ticket.receipt_unexpected", "this effect kind has no budget to count"
            )
    else:
        if not isinstance(receipt, dict):
            raise MeshError("ticket.receipt_missing", "")
        if (
            receipt.get("type") != RECEIPT_TYPE
            or receipt.get("evidence_digest") != digest
        ):
            raise MeshError(
                "ticket.receipt_mismatch", "the receipt is not bound to this action"
            )
        if not receipt_verifier(receipt):
            raise MeshError("ticket.receipt_invalid", "LETS refused the receipt")
    if ticket["consent"] is None:
        if consent_required:
            raise MeshError("ticket.consent_required", "")
    else:
        try:
            checked = consent_mod.verify(
                ticket["consent"], "action", digest, presence_keys
            )
        except MeshError as exc:
            raise MeshError(
                "ticket.consent_wrong_action"
                if exc.code == "consent.wrong_action"
                else "ticket.consent_invalid",
                exc.code,
            ) from exc
        if abs(now_s * 1000 - checked["clock"]["ms"]) > CONSENT_MAX_AGE_MS:
            raise MeshError(
                "ticket.consent_stale", "consent is too old or from the future"
            )
    return claims
