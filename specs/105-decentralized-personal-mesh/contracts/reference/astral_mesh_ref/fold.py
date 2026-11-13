"""Folds that every peer computes identically from the same events: chat branches, grants and revocations, last-writer-wins settings.
They read decoded event summaries, so the merge rules of the event families are testable without any transport.
"""

from __future__ import annotations

from typing import Any

from . import consent as consent_mod
from .errors import MeshError

RULE_ORDER = ("allow", "consent", "deny")


def order_key(event: dict[str, Any]) -> tuple[int, int, str, int]:
    return (event["clock"]["ms"], event["clock"]["n"], event["feed"], event["seq"])


def ancestors(events: list[dict[str, Any]]) -> dict[str, frozenset[str]]:
    by_id = {e["id"]: e for e in events}
    by_pos = {(e["feed"], e["seq"]): e["id"] for e in events}
    cache: dict[str, frozenset[str]] = {}

    def visit(eid: str, stack: frozenset[str]) -> frozenset[str]:
        if eid in cache:
            return cache[eid]
        if eid in stack:
            raise MeshError("fold.cycle", eid)
        event = by_id[eid]
        parents = [d for d in event["after"] if d in by_id]
        previous = by_pos.get((event["feed"], event["seq"] - 1))
        if previous is not None:
            parents.append(previous)
        out: set[str] = set()
        for parent in parents:
            out |= visit(parent, stack | {eid}) | {parent}
        cache[eid] = frozenset(out)
        return cache[eid]

    return {e["id"]: visit(e["id"], frozenset()) for e in events}


def chat_branches(events: list[dict[str, Any]]) -> dict[str, Any]:
    turns = sorted((e for e in events if e["type"] == "chat.turn_start"), key=order_key)
    children: dict[str | None, list[str]] = {}
    for turn in turns:
        children.setdefault(turn["body"]["continues"], []).append(turn["id"])
    known = {t["id"] for t in turns}
    for parent in children:
        if parent is not None and parent not in known:
            raise MeshError("fold.unknown_turn", str(parent))
    run_messages = {
        t["body"]["message"] for t in turns if t["body"]["message"] is not None
    }
    keys = {t["id"]: order_key(t) for t in turns}

    def latest(base: str | None) -> str | None:
        if base is None:
            return max(keys, key=keys.__getitem__) if keys else None
        subtree = {base}
        frontier = [base]
        while frontier:
            for child in children.get(frontier.pop(), []):
                subtree.add(child)
                frontier.append(child)
        return max(subtree, key=keys.__getitem__)

    queued = {
        m["id"]: latest(m["body"]["base"])
        for m in sorted(
            (e for e in events if e["type"] == "chat.user_message"), key=order_key
        )
        if m["id"] not in run_messages
    }
    return {
        "branches": [
            {"children": kids, "parent": parent}
            for parent, kids in sorted(children.items(), key=lambda kv: str(kv[0]))
            if len(kids) > 1
        ],
        "queued": queued,
        "roots": children.get(None, []),
    }


def authority(
    events: list[dict[str, Any]], presence_keys: dict[str, str]
) -> dict[str, Any]:
    anc = ancestors(events)
    consented: set[str] = set()
    failures: dict[str, str] = {}
    for event in sorted(
        (e for e in events if e["type"] == "auth.consent"), key=order_key
    ):
        target = event["body"]["grant"]
        try:
            consent_mod.verify(event["body"]["consent"], "grant", target, presence_keys)
        except MeshError as error:
            failures.setdefault(target, error.code.removeprefix("consent."))
            continue
        consented.add(target)

    def key_of(e: dict[str, Any]) -> tuple[str, str, str]:
        return (e["body"]["agent"], e["body"]["tool"], e["body"]["scope"])

    revokes: dict[tuple[str, str, str], list[str]] = {}
    for event in events:
        if event["type"] == "auth.revoke":
            revokes.setdefault(key_of(event), []).append(event["id"])
    grants: dict[str, str] = {}
    waiting: list[str] = []
    refusals: dict[str, str] = {}
    for event in sorted(
        (e for e in events if e["type"] == "auth.grant"), key=order_key
    ):
        if event["id"] not in consented:
            waiting.append(event["id"])
            refusals[event["id"]] = (
                "grant.consent_" + failures[event["id"]]
                if event["id"] in failures
                else "grant.consent_missing"
            )
            continue
        if all(r in anc[event["id"]] for r in revokes.get(key_of(event), [])):
            grants["/".join(key_of(event))] = event["id"]
    rules: dict[str, str] = {}
    for event in events:
        if event["type"] == "auth.narrow":
            key = event["body"]["key"]
            rule = event["body"]["rule"]
            if rule not in RULE_ORDER:
                raise MeshError("fold.bad_rule", rule)
            if key not in rules or RULE_ORDER.index(rule) > RULE_ORDER.index(
                rules[key]
            ):
                rules[key] = rule
    return {
        "grants": dict(sorted(grants.items())),
        "refusals": dict(sorted(refusals.items())),
        "rules": dict(sorted(rules.items())),
        "waiting_for_consent": sorted(waiting),
    }


def settings(events: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    winners: dict[str, tuple[int, int, str, int]] = {}
    for event in events:
        if event["type"] in ("settings.set", "mem.remember"):
            key = event["body"]["key"]
            rank = order_key(event)
            if key not in winners or rank > winners[key]:
                winners[key] = rank
                out[key] = event["body"]["value"]
    return dict(sorted(out.items()))
