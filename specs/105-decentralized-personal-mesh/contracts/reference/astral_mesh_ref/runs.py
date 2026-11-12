"""Run events: the lease fold that names one executor per run, the state fold of goal and schedule events, run start, schedule arithmetic, the pre-check and the judge answer check.
The kernel's run driver writes these bodies, the vectors in runs.json pin them, and fold.py supplies the order key the lease fold sorts by.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from . import crypto
from .errors import MeshError
from .fold import order_key

U32_MAX = 2**32 - 1
U64_MAX = 2**64 - 1
MAX_GOAL_BYTES = 8192
MAX_ROUND_CHARS = 6000
MAX_JUDGE_VALUE_BYTES = 256
RUN_KINDS = ("goal", "schedule", "watch")
PAUSE_KINDS = ("owner", "condition", "waiting")
BOOKKEEPING = {"confidentiality": "private", "integrity": "tool-trusted"}
OWNER_ACTION = {"confidentiality": "private", "integrity": "owner"}
DEFAULT_LIMITS = {
    "max_rounds": 20,
    "max_stalls": 3,
    "max_wait_ms": 600_000,
    "max_tokens": None,
}
WHITESPACE = frozenset(
    "\t\n\x0b\x0c\r \x85\xa0     　" + "".join(chr(c) for c in range(0x2000, 0x200B))
)


def _text(body: Any, name: str) -> str | None:
    value = body.get(name) if isinstance(body, dict) else None
    return value if isinstance(value, str) else None


def _num(body: Any, name: str) -> int | None:
    value = body.get(name) if isinstance(body, dict) else None
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= U64_MAX:
        return value
    return None


def _flag(body: Any, name: str) -> bool | None:
    value = body.get(name) if isinstance(body, dict) else None
    return value if isinstance(value, bool) else None


def _u32(value: int | None) -> int | None:
    return value if value is not None and value <= U32_MAX else None


def _blank(text: str) -> bool:
    return all(c in WHITESPACE for c in text)


def _parse_lease(event: dict[str, Any]) -> dict[str, Any] | None:
    body = event["body"]
    if not isinstance(body, dict):
        return None
    raw = body.get("supersedes")
    if raw is None:
        supersedes: list[str] = []
    elif isinstance(raw, list) and all(isinstance(t, str) for t in raw):
        supersedes = list(raw)
    else:
        return None
    if "takeover" not in body:
        takeover = False
    elif isinstance(body["takeover"], bool):
        takeover = body["takeover"]
    else:
        return None
    run, executor = _text(body, "run"), _text(body, "executor")
    if run is None or executor is None:
        return None
    return {
        "event": event,
        "id": event["id"],
        "run": run,
        "executor": executor,
        "supersedes": supersedes,
        "takeover": takeover,
    }


def lease_fold(
    events: list[dict[str, Any]], device_of_feed: Callable[[str], str | None]
) -> list[dict[str, Any]]:
    leases = []
    for event in events:
        if event["type"] != "run.lease":
            continue
        lease = _parse_lease(event)
        if lease is not None:
            leases.append(lease)
    leases.sort(key=lambda lease: order_key(lease["event"]))
    by_id = {lease["id"]: lease for lease in leases}
    by_run: dict[str, list[dict[str, Any]]] = {}
    for lease in leases:
        by_run.setdefault(lease["run"], []).append(lease)
    out = []
    for run in sorted(by_run):
        run_leases = by_run[run]
        valid: set[str] = set()
        rejected: list[str] = []
        settled = False
        while not settled:
            settled = True
            for lease in run_leases:
                lid = lease["id"]
                if lid in valid or lid in rejected:
                    continue
                author = device_of_feed(lease["event"]["feed"])
                verdict: bool | None = True
                for target_id in lease["supersedes"]:
                    target = by_id.get(target_id)
                    if target is None:
                        verdict = None
                        break
                    if target["run"] != run:
                        verdict = False
                        break
                    if target["id"] not in valid:
                        verdict = False if target["id"] in rejected else None
                        break
                    if not lease["takeover"] and author != target["executor"]:
                        verdict = False
                        break
                if (
                    not lease["supersedes"]
                    and not lease["takeover"]
                    and author != lease["executor"]
                ):
                    verdict = False
                if verdict is True:
                    valid.add(lid)
                    settled = False
                elif verdict is False:
                    rejected.append(lid)
                    settled = False
        waiting = [
            lease["id"]
            for lease in run_leases
            if lease["id"] not in valid and lease["id"] not in rejected
        ]
        superseded = {
            target
            for lease in run_leases
            if lease["id"] in valid
            for target in lease["supersedes"]
        }
        heads = [
            lease
            for lease in run_leases
            if lease["id"] in valid and lease["id"] not in superseded
        ]
        if len(heads) == 1:
            executor, contested = heads[0]["executor"], []
        elif not heads:
            executor, contested = None, []
        else:
            executor, contested = None, [h["executor"] for h in heads]
        out.append(
            {
                "contested": contested,
                "executor": executor,
                "heads": [h["id"] for h in heads],
                "leases": [
                    {
                        "author": device_of_feed(lease["event"]["feed"]),
                        "executor": lease["executor"],
                        "id": lease["id"],
                        "supersedes": lease["supersedes"],
                        "takeover": lease["takeover"],
                    }
                    for lease in run_leases
                    if lease["id"] in valid
                ],
                "rejected": rejected,
                "run": run,
                "waiting": waiting,
            }
        )
    return out


def _limits_of(body: Any) -> dict[str, Any]:
    raw = body.get("limits") if isinstance(body, dict) else None

    def get(name: str) -> int | None:
        return _num(raw, name)

    rounds, stalls = _u32(get("max_rounds")), _u32(get("max_stalls"))
    wait = get("max_wait_ms")
    return {
        "max_rounds": DEFAULT_LIMITS["max_rounds"] if rounds is None else rounds,
        "max_stalls": DEFAULT_LIMITS["max_stalls"] if stalls is None else stalls,
        "max_tokens": get("max_tokens"),
        "max_wait_ms": DEFAULT_LIMITS["max_wait_ms"] if wait is None else wait,
    }


def _schedule_of(body: Any) -> dict[str, Any] | None:
    interval, anchor = _num(body, "interval_ms"), _num(body, "anchor_ms")
    catch_up, grace = _flag(body, "catch_up"), _num(body, "grace_ms")
    max_catch = _u32(_num(body, "max_catch_up"))
    precheck = _flag(body, "precheck")
    if None in (interval, anchor, catch_up, grace, max_catch, precheck):
        return None
    return {
        "anchor_ms": anchor,
        "catch_up": catch_up,
        "grace_ms": grace,
        "interval_ms": interval,
        "max_catch_up": max_catch,
        "precheck": precheck,
    }


def _is_for(event: dict[str, Any], run: str) -> bool:
    return _text(event["body"], "run") == run


def _op(event: dict[str, Any]) -> str:
    return _text(event["body"], "op") or ""


def run_ids(events: list[dict[str, Any]]) -> list[str]:
    return [
        run
        for e in events
        if e["type"] == "run.goal"
        and _op(e) == "define"
        and (run := _text(e["body"], "run")) is not None
    ]


def state_fold(run: str, chat: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    define = next(
        (
            e
            for e in events
            if e["type"] == "run.goal" and _op(e) == "define" and _is_for(e, run)
        ),
        None,
    )
    if define is None:
        raise MeshError("run.unknown", run)
    kind = _text(define["body"], "kind")
    if kind not in RUN_KINDS:
        raise MeshError("run.malformed", "unknown run kind")
    goal = _text(define["body"], "goal")
    if goal is None:
        raise MeshError("run.malformed", "the goal is missing")
    state: dict[str, Any] = {
        "branches": [],
        "chat": chat,
        "goal": goal,
        "kind": kind,
        "labels": dict(define["labels"]),
        "last_fingerprint": None,
        "last_slot": None,
        "last_text_hash": None,
        "limits": _limits_of(define["body"]),
        "notes": None,
        "project": _text(define["body"], "project"),
        "rounds": 0,
        "run": run,
        "schedule": None,
        "stalls": 0,
        "status": {"pause": None, "reason": None, "state": "active"},
        "tokens_spent": 0,
        "unknown": [],
        "waiting_since_ms": None,
    }
    for event in events:
        if not _is_for(event, run):
            continue
        body, key = event["body"], (event["type"], _op(event))
        if key == ("run.schedule", "define"):
            state["schedule"] = _schedule_of(body)
        elif key == ("run.schedule", "fired"):
            state["last_slot"] = _later(state["last_slot"], _num(body, "slot"))
            if (fingerprint := _text(body, "fingerprint")) is not None:
                state["last_fingerprint"] = fingerprint
        elif key == ("run.schedule", "skipped"):
            state["last_slot"] = _later(state["last_slot"], _num(body, "slot"))
        elif key == ("run.goal", "round"):
            _apply_round(state, body)
        elif key == ("run.goal", "status"):
            _apply_status(state, body)
        elif key == ("run.goal", "notes"):
            through = _u32(_num(body, "through_round"))
            state["notes"] = {
                "event": event["id"],
                "labels": dict(event["labels"]),
                "text": _text(body, "text") or "",
                "through_round": 0 if through is None else through,
            }
        elif key == ("run.goal", "unknown"):
            calls = body.get("calls")
            state["unknown"].append(
                {
                    "calls": [c for c in calls if isinstance(c, str)]
                    if isinstance(calls, list)
                    else [],
                    "reason": _text(body, "reason") or "",
                    "turn": _text(body, "turn") or "",
                }
            )
    return state


def _later(current: int | None, new: int | None) -> int | None:
    if current is None:
        return new
    if new is None:
        return current
    return max(current, new)


def _apply_round(state: dict[str, Any], body: dict[str, Any]) -> None:
    state["rounds"] = min(state["rounds"] + 1, U32_MAX)
    state["tokens_spent"] = min(
        state["tokens_spent"] + (_num(body, "tokens") or 0), U64_MAX
    )
    state["last_text_hash"] = _text(body, "text_hash")
    verdict = _text(body, "verdict")
    if verdict is not None:
        state["stalls"] = (
            0 if _flag(body, "progress") else min(state["stalls"] + 1, U32_MAX)
        )
    executor = _text(body, "executor") or ""
    turn = _text(body, "turn")
    for branch in state["branches"]:
        if branch["executor"] == executor:
            branch["rounds"] += 1
            branch["last_turn"] = turn
            branch["last_verdict"] = verdict
            return
    state["branches"].append(
        {
            "executor": executor,
            "last_turn": turn,
            "last_verdict": verdict,
            "rounds": 1,
        }
    )


def _apply_status(state: dict[str, Any], body: dict[str, Any]) -> None:
    reason = _text(body, "reason") or ""
    name = _text(body, "status")
    if name == "active":
        nxt = {"pause": None, "reason": None, "state": "active"}
    elif name == "paused":
        pause = _text(body, "pause")
        nxt = {
            "pause": pause if pause in PAUSE_KINDS else "owner",
            "reason": reason,
            "state": "paused",
        }
    elif name == "done":
        nxt = {"pause": None, "reason": None, "state": "done"}
    elif name == "stopped":
        nxt = {"pause": None, "reason": reason, "state": "stopped"}
    else:
        return
    before = state["status"]
    if nxt["pause"] == "waiting":
        if before["pause"] != "waiting":
            state["waiting_since_ms"] = _num(body, "at")
    else:
        state["waiting_since_ms"] = None
    state["status"] = nxt


def _is_chat_id(value: Any) -> bool:
    return crypto.is_id(value)


def start_events(
    spec: dict[str, Any], device: str, now_ms: int, nonce: int = 0
) -> dict[str, Any]:
    goal = spec["goal"]
    if _blank(goal) or len(goal.encode("utf-8")) > MAX_GOAL_BYTES:
        raise MeshError("run.invalid", "the goal must be between 1 and 8192 bytes")
    limits = spec["limits"]
    if limits["max_rounds"] == 0 or limits["max_stalls"] == 0:
        raise MeshError("run.invalid", "round and stall limits must be above zero")
    kind, schedule = spec["kind"], spec.get("schedule")
    if kind not in RUN_KINDS:
        raise MeshError("run.invalid", "unknown run kind")
    if kind == "goal" and schedule is not None:
        raise MeshError("run.invalid", "a goal run has no schedule")
    if kind != "goal" and schedule is None:
        raise MeshError("run.invalid", "a schedule or watch run needs a schedule")
    if schedule is not None and schedule["interval_ms"] == 0:
        raise MeshError("run.invalid", "the schedule interval must be above zero")
    chat = spec["chat_id"]
    if not _is_chat_id(chat):
        raise MeshError("run.invalid", "the chat id is malformed")
    seed = f"{chat}|{device}|{now_ms}|{nonce}|{goal}"
    run = f"{chat}.{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:24]}"
    events = [
        {
            "body": {
                "goal": goal,
                "kind": kind,
                "limits": {
                    "max_rounds": limits["max_rounds"],
                    "max_stalls": limits["max_stalls"],
                    "max_tokens": limits["max_tokens"],
                    "max_wait_ms": limits["max_wait_ms"],
                },
                "op": "define",
                "project": spec.get("project"),
                "run": run,
            },
            "labels": dict(spec["labels"]),
            "type": "run.goal",
        }
    ]
    if schedule is not None:
        events.append(
            {
                "body": {
                    "anchor_ms": schedule["anchor_ms"],
                    "catch_up": schedule["catch_up"],
                    "grace_ms": schedule["grace_ms"],
                    "interval_ms": schedule["interval_ms"],
                    "max_catch_up": schedule["max_catch_up"],
                    "op": "define",
                    "precheck": schedule["precheck"],
                    "run": run,
                },
                "labels": dict(BOOKKEEPING),
                "type": "run.schedule",
            }
        )
    events.append(
        {
            "body": lease_body(run, device, [], False, "start"),
            "labels": dict(BOOKKEEPING),
            "type": "run.lease",
        }
    )
    return {"events": events, "run": run}


def lease_body(
    run: str, executor: str, supersedes: list[str], takeover: bool, reason: str
) -> dict[str, Any]:
    return {
        "executor": executor,
        "reason": reason,
        "run": run,
        "supersedes": supersedes,
        "takeover": takeover,
    }


def schedule_plan(
    schedule: dict[str, Any], now_ms: int, last_slot: int | None
) -> dict[str, Any]:
    interval = schedule["interval_ms"]
    if interval == 0:
        raise MeshError("run.invalid", "the schedule interval must be above zero")

    def sat(value: int) -> int:
        return min(value, U64_MAX)

    nxt = sat(last_slot + interval) if last_slot is not None else schedule["anchor_ms"]
    if now_ms < nxt:
        return {"fire": None, "handled_through": None, "missed": 0, "next_ms": nxt}
    due = (now_ms - nxt) // interval + 1

    def slot_at(k: int) -> int:
        return sat(nxt + sat(k * interval))

    if schedule["catch_up"]:
        keep = max(schedule["max_catch_up"], 1)
        skipped = max(due - keep, 0)
        if skipped > 0:
            return {
                "fire": None,
                "handled_through": slot_at(skipped - 1),
                "missed": skipped,
                "next_ms": slot_at(skipped),
            }
        return {
            "fire": nxt,
            "handled_through": nxt,
            "missed": 0,
            "next_ms": sat(nxt + interval),
        }
    latest = slot_at(due - 1)
    following = sat(latest + interval)
    if max(now_ms - latest, 0) <= schedule["grace_ms"]:
        return {
            "fire": latest,
            "handled_through": latest,
            "missed": due - 1,
            "next_ms": following,
        }
    return {
        "fire": None,
        "handled_through": latest,
        "missed": due,
        "next_ms": following,
    }


def precheck(probe_text: str, last_fingerprint: str | None) -> dict[str, Any]:
    fingerprint = crypto.sha256_id(probe_text.encode("utf-8"))
    if last_fingerprint == fingerprint:
        return {"reason": "unchanged", "wake": False}
    return {"fingerprint": fingerprint, "wake": True}


def clip_round_output(text: str) -> str:
    return text.encode("utf-8")[:MAX_ROUND_CHARS].decode("utf-8", "ignore")


def judge_answer(
    goal: str, round_number: int, round_output: str, raw: Any
) -> dict[str, Any]:
    answer = raw.get("answer") if isinstance(raw, dict) else None
    ok = (
        isinstance(answer, dict)
        and set(answer) == {"verdict", "progress"}
        and answer["verdict"] in ("done", "continue", "blocked")
        and isinstance(answer["progress"], bool)
        and len(
            json.dumps(answer, separators=(",", ":"), ensure_ascii=False).encode(
                "utf-8"
            )
        )
        <= MAX_JUDGE_VALUE_BYTES
    )
    if not ok:
        raise MeshError(
            "labels.quarantine_schema",
            "the judge answer did not match its fixed schema",
        )
    return {
        "content": {
            "goal": goal,
            "round": round_number,
            "round_output": clip_round_output(round_output),
        },
        "progress": answer["progress"],
        "verdict": answer["verdict"],
    }


def register(op: Callable[[str], Callable[[Any], Any]]) -> None:
    @op("run_lease_fold")
    def _lease_fold(i: dict[str, Any]) -> Any:
        devices = i["devices"]
        return {"runs": lease_fold(i["events"], devices.get)}

    @op("run_state_fold")
    def _state_fold(i: dict[str, Any]) -> Any:
        return {"state": state_fold(i["run"], i["chat"], i["events"])}

    @op("run_start")
    def _start(i: dict[str, Any]) -> Any:
        return start_events(i["spec"], i["device"], i["now_ms"])

    @op("run_schedule_plan")
    def _plan(i: dict[str, Any]) -> Any:
        return schedule_plan(i["schedule"], i["now_ms"], i.get("last_slot"))

    @op("run_precheck")
    def _precheck(i: dict[str, Any]) -> Any:
        return precheck(i["probe"], i.get("last_fingerprint"))

    @op("run_judge_answer")
    def _judge(i: dict[str, Any]) -> Any:
        return judge_answer(i["goal"], i["round"], i["round_output"], i["raw"])
