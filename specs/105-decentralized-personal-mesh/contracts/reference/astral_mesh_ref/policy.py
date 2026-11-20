"""The budget policy document: shape, signature, narrowing and widening rules, the merge of concurrent policies and the input handed to LETS to derive a period's budget.
It uses consent.py for widening consent and roster.py's device kinds, and the share arithmetic itself belongs to LETS.
"""

from __future__ import annotations

from typing import Any

from . import consent as consent_mod
from . import crypto
from .cj import b64url_decode
from .errors import MeshError
from .roster import KINDS

MODES = ("fresh", "solo")
CLASSES = ("notes_write", "outside_effect", "model_tokens")
POLICY_FIELDS = {
    "author",
    "classes",
    "clock",
    "devices",
    "follows",
    "freshness_s",
    "mesh",
    "period",
    "sig",
    "v",
}


def policy_id(policy: dict[str, Any]) -> str:
    return crypto.object_id(policy)


def build(
    identity_scalar: int,
    *,
    mesh: str,
    author: str,
    follows: list[str],
    clock: dict[str, int],
    period: dict[str, Any],
    freshness_s: int,
    classes: dict[str, Any],
    devices: dict[str, Any],
) -> dict[str, Any]:
    policy: dict[str, Any] = {
        "author": author,
        "classes": classes,
        "clock": clock,
        "devices": devices,
        "follows": sorted(follows),
        "freshness_s": freshness_s,
        "mesh": mesh,
        "period": period,
        "v": 1,
    }
    policy["sig"] = crypto.sign_object(
        "p256", identity_scalar, "astral.policy/v1", policy
    )
    return policy


def _shares(block: Any) -> None:
    if not isinstance(block, dict) or set(block) != set(MODES):
        raise MeshError("policy.malformed", "shares need fresh and solo")
    for mode in MODES:
        caps = block[mode]
        if (
            not isinstance(caps, dict)
            or set(caps) != set(CLASSES)
            or any(type(v) is not int or v < 0 for v in caps.values())
        ):
            raise MeshError(
                "policy.malformed", "caps need every class as a non-negative integer"
            )
    if any(block["solo"][c] > block["fresh"][c] for c in CLASSES):
        raise MeshError(
            "policy.solo_exceeds_fresh", "a solo cap cannot exceed the fresh cap"
        )


def check_shape(policy: Any) -> None:
    if not isinstance(policy, dict) or set(policy) != POLICY_FIELDS or policy["v"] != 1:
        raise MeshError("policy.malformed", "fields")
    if not crypto.is_id(policy["mesh"]) or not crypto.is_id(policy["author"]):
        raise MeshError("policy.malformed", "mesh or author")
    follows = policy["follows"]
    if (
        not isinstance(follows, list)
        or follows != sorted(set(follows))
        or any(not crypto.is_id(f) for f in follows)
    ):
        raise MeshError("policy.malformed", "follows")
    period = policy["period"]
    if (
        not isinstance(period, dict)
        or set(period) != {"anchor_s", "length_s", "unit"}
        or period["unit"] != "day"
        or type(period["length_s"]) is not int
        or period["length_s"] <= 0
        or type(period["anchor_s"]) is not int
        or period["anchor_s"] < 0
    ):
        raise MeshError("policy.malformed", "period")
    if type(policy["freshness_s"]) is not int or policy["freshness_s"] < 0:
        raise MeshError("policy.malformed", "freshness_s")
    if not isinstance(policy["classes"], dict) or set(policy["classes"]) != set(KINDS):
        raise MeshError("policy.malformed", "classes need every device kind")
    for kind in KINDS:
        _shares(policy["classes"][kind])
    if not isinstance(policy["devices"], dict):
        raise MeshError("policy.malformed", "devices")
    for device, block in policy["devices"].items():
        if not crypto.is_id(device):
            raise MeshError("policy.malformed", "device id")
        _shares(block)
    clock = policy["clock"]
    if (
        not isinstance(clock, dict)
        or set(clock) != {"ms", "n"}
        or any(type(v) is not int or v < 0 for v in clock.values())
    ):
        raise MeshError("policy.malformed", "clock")


def verify(policy: Any, identity_keys: dict[str, str], mesh: str) -> str:
    check_shape(policy)
    if policy["mesh"] != mesh:
        raise MeshError("policy.wrong_mesh", policy["mesh"])
    key = identity_keys.get(policy["author"])
    if key is None:
        raise MeshError("policy.unknown_author", policy["author"])
    if not crypto.verify_object("p256", b64url_decode(key), "astral.policy/v1", policy):
        raise MeshError("policy.bad_signature", "author signature")
    return policy_id(policy)


def share(policy: dict[str, Any], device: str, kind: str, mode: str) -> dict[str, int]:
    block = policy["devices"].get(device) or policy["classes"][kind]
    return block[mode]


def narrower_or_equal(
    a: dict[str, Any], b: dict[str, Any], kinds: dict[str, str]
) -> bool:
    if a["freshness_s"] > b["freshness_s"]:
        return False
    for kind in KINDS:
        for mode in MODES:
            if any(
                a["classes"][kind][mode][c] > b["classes"][kind][mode][c]
                for c in CLASSES
            ):
                return False
    for device in sorted(set(a["devices"]) | set(b["devices"])):
        kind = kinds.get(device)
        if kind is None:
            continue
        for mode in MODES:
            left, right = share(a, device, kind, mode), share(b, device, kind, mode)
            if any(left[c] > right[c] for c in CLASSES):
                return False
    return True


def meet(docs: list[dict[str, Any]], kinds: dict[str, str]) -> dict[str, Any]:
    first = docs[0]
    classes = {
        k: {
            m: {c: min(d["classes"][k][m][c] for d in docs) for c in CLASSES}
            for m in MODES
        }
        for k in KINDS
    }
    ids = sorted({dev for d in docs for dev in d["devices"]})
    devices = {}
    for dev in ids:
        kind = kinds.get(dev)
        if kind is None:
            continue
        devices[dev] = {
            m: {c: min(share(d, dev, kind, m)[c] for d in docs) for c in CLASSES}
            for m in MODES
        }
    newest = max(docs, key=lambda d: (d["clock"]["ms"], d["clock"]["n"], policy_id(d)))
    return {
        "classes": classes,
        "devices": devices,
        "freshness_s": min(d["freshness_s"] for d in docs),
        "period": newest["period"]
        if any(d["period"] != first["period"] for d in docs)
        else first["period"],
    }


ZERO_CAPS = {m: {c: 0 for c in CLASSES} for m in MODES}
ZERO = {
    "classes": {k: ZERO_CAPS for k in KINDS},
    "devices": {},
    "freshness_s": 0,
    "period": None,
}


def effective(
    wrappers: list[dict[str, Any]],
    members: dict[str, dict[str, str]],
    mesh: str,
) -> dict[str, Any]:
    identity_keys = {d: m["identity"] for d, m in members.items()}
    presence_keys = {d: m["presence"] for d, m in members.items()}
    kinds = {d: m["kind"] for d, m in members.items()}
    items: dict[str, dict[str, Any]] = {}
    ignored: dict[str, str] = {}
    for wrapper in wrappers:
        policy = wrapper["policy"]
        try:
            identifier = verify(policy, identity_keys, mesh)
        except MeshError as exc:
            try:
                ignored[policy_id(policy)] = exc.code
            except MeshError:
                pass
            continue
        items[identifier] = wrapper
    accepted: dict[str, dict[str, Any]] = {}
    pending: set[str] = set()
    changed = True
    remaining = dict(items)
    while changed:
        changed = False
        for identifier in sorted(remaining):
            wrapper = remaining[identifier]
            policy = wrapper["policy"]
            parents = policy["follows"]
            if any(p in ignored for p in parents):
                ignored[identifier] = "policy.parent_rejected"
                del remaining[identifier]
                changed = True
                continue
            if any(p not in accepted for p in parents):
                continue
            base = (
                meet([accepted[p]["policy"] for p in parents], kinds)
                if parents
                else ZERO
            )
            widening = (
                base["period"] is not None and policy["period"] != base["period"]
            ) or not narrower_or_equal(policy, base, kinds)
            if widening:
                try:
                    consent_mod.verify(
                        wrapper["consent"], "policy", identifier, presence_keys
                    )
                except MeshError as exc:
                    code = (
                        "policy.widening_needs_consent"
                        if wrapper["consent"] is None
                        else "policy." + exc.code
                    )
                    ignored[identifier] = code
                    del remaining[identifier]
                    changed = True
                    continue
            accepted[identifier] = wrapper
            del remaining[identifier]
            changed = True
    pending = set(remaining) - set(ignored)
    followed = {p for w in accepted.values() for p in w["policy"]["follows"]}
    heads = sorted(i for i in accepted if i not in followed)
    result: dict[str, Any] | None = None
    conflict = False
    if heads:
        docs = [accepted[h]["policy"] for h in heads]
        result = meet(docs, kinds)
        conflict = any(d["period"] != docs[0]["period"] for d in docs)
    return {
        "accepted": sorted(accepted),
        "conflict": conflict,
        "effective": result,
        "heads": heads,
        "ignored": dict(sorted(ignored.items())),
        "pending": sorted(pending),
    }


def budget_input(
    effective_policy: dict[str, Any],
    mesh: str,
    now_s: int,
    members: dict[str, str],
    stop: int,
) -> dict[str, Any]:
    period = effective_policy["period"]
    if now_s < period["anchor_s"]:
        raise MeshError("policy.before_anchor", "clock is before the period anchor")
    index = (now_s - period["anchor_s"]) // period["length_s"]
    body = {
        "freshness_s": effective_policy["freshness_s"],
        "mesh": mesh,
        "members": [{"device": d, "kind": members[d]} for d in sorted(members)],
        "period": period,
        "period_index": index,
        "policy": crypto.object_id(
            {
                k: effective_policy[k]
                for k in ("classes", "devices", "freshness_s", "period")
            }
        ),
        "stop": stop,
        "v": 1,
    }
    return {"digest": crypto.object_id(body), "input": body}
