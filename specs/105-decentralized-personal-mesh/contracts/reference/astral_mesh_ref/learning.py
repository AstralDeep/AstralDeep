"""The learning loop's wire and rules: candidate content hash, the static gate, the proposal, gate, replay, adoption and reversal bodies, the trial, and the ledger fold that re-checks every adoption.
Labels come from labels.py, the order key from fold.py, and the event builders share their label constants with runs.py.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from typing import Any

from . import crypto, labels as labels_mod
from .errors import MeshError
from .fold import order_key
from .runs import BOOKKEEPING, OWNER_ACTION, WHITESPACE

U32_MAX = 2**32 - 1
U64_MAX = 2**64 - 1
ITEM_KINDS = ("memory", "skill", "instruction_edit")
MAX_CONTENT_BYTES = 4096
MAX_INSTRUCTION_BYTES = 2048
MAX_NAME_BYTES = 64
MAX_LINE_CHARS = 1000
OWNER_REASONS = (
    "untrusted_evidence",
    "wider_scope",
    "instruction_edit",
    "suspicious_text",
)
DEFAULT_POLICY = {
    "max_error_rise_permille": 100,
    "max_repeat_rise_permille": 100,
    "min_samples": 5,
}

HIDDEN = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF9, 0xFFFB),
    (0xE0000, 0xE007F),
    (0xE0100, 0xE01EF),
)
SCRIPT_MARKERS = (
    "<script",
    "</script",
    "<iframe",
    "<object",
    "<embed",
    "javascript:",
    "vbscript:",
    "onerror=",
    "onload=",
    "#!/",
    "eval(",
    "exec(",
    "os.system",
    "subprocess",
    "child_process",
    "powershell",
    "| sh",
    "| bash",
    "|sh",
    "|bash",
    "curl ",
    "wget ",
    "rm -rf",
    "base64 -d",
    "chmod +x",
    "$(",
    "`sh",
)
EXECUTABLE_FENCES = frozenset(
    (
        "sh",
        "bash",
        "zsh",
        "fish",
        "python",
        "py",
        "js",
        "javascript",
        "ts",
        "node",
        "ps1",
        "powershell",
        "cmd",
        "bat",
        "ruby",
        "rb",
        "perl",
        "php",
        "lua",
        "swift",
        "kotlin",
        "java",
        "go",
        "rust",
        "c",
        "cpp",
        "sql",
        "applescript",
    )
)
NETWORK_MARKERS = (
    "://",
    "www.",
    "localhost",
    "ngrok",
    "data:text",
    "data:application",
    "mailto:",
    "ftp.",
)
HYGIENE_PHRASES = (
    "ignore previous",
    "ignore all previous",
    "ignore the above",
    "disregard",
    "forget your instructions",
    "you are now",
    "new instructions",
    "system prompt",
    "developer mode",
    "override",
    "without asking",
    "do not ask",
    "don't ask",
    "skip confirmation",
    "skip the confirmation",
    "no need to confirm",
    "always approve",
    "auto-approve",
    "authorized to",
    "you have permission",
    "pretend",
    "jailbreak",
    "do not tell the user",
    "don't tell the user",
    "exfiltrate",
    "send the contents",
    "reveal your",
)
FOLD_REASONS = {
    "malformed proposal": "learn.fold.malformed_proposal",
    "no evidence": "learn.fold.no_evidence",
    "labels do not match the evidence": "learn.fold.label_mismatch",
    "names an unknown proposal": "learn.fold.unknown_proposal",
    "no proposal": "learn.fold.no_proposal",
    "no gate record": "learn.fold.no_gate",
    "the gate failed": "learn.fold.gate_failed",
    "no replay record": "learn.fold.no_replay",
    "the replay did not pass": "learn.fold.replay_failed",
    "the content hash does not match the proposal": "learn.fold.hash_mismatch",
    "the replay report does not match": "learn.fold.report_mismatch",
    "the proposal needs the owner": "learn.fold.needs_owner",
    "untrusted evidence is never adopted automatically": "learn.fold.untrusted_auto",
    "an owner adoption must carry the owner label": "learn.fold.owner_label",
    "unknown adoption mode": "learn.fold.unknown_mode",
    "already adopted": "learn.fold.already_adopted",
    "wider than the proposal asked": "learn.fold.wider_than_asked",
    "names an unknown adoption": "learn.fold.unknown_adoption",
}


def _text(body: Any, name: str) -> str | None:
    value = body.get(name) if isinstance(body, dict) else None
    return value if isinstance(value, str) else None


def _strings(body: Any, name: str) -> list[str]:
    value = body.get(name) if isinstance(body, dict) else None
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _flag(body: Any, name: str) -> bool | None:
    value = body.get(name) if isinstance(body, dict) else None
    return value if isinstance(value, bool) else None


def _u32(body: Any, name: str) -> int | None:
    value = body.get(name) if isinstance(body, dict) else None
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= U32_MAX:
        return value
    return None


def content_hash(kind: str, name: str, content: str) -> str:
    return crypto.sha256_id(f"{kind}\n{name}\n{content}".encode())


def candidate_labels(evidence: list[dict[str, Any]]) -> dict[str, str]:
    return labels_mod.join([e["labels"] for e in evidence])


def item_labels(label: dict[str, str]) -> dict[str, str]:
    return labels_mod.join([label, BOOKKEEPING])


def _scope(item_scope: dict[str, Any] | None) -> dict[str, Any]:
    scope = item_scope or {}
    return {"project": scope.get("project"), "tool": scope.get("tool")}


def stage_event(candidate: dict[str, Any]) -> dict[str, Any]:
    if candidate["kind"] not in ITEM_KINDS:
        raise MeshError("learn.malformed", "unknown item kind")
    if not candidate["evidence"]:
        raise MeshError("learn.no_evidence", "a proposal must name its evidence")
    return {
        "body": {
            "content": candidate["content"],
            "content_hash": content_hash(
                candidate["kind"], candidate["name"], candidate["content"]
            ),
            "evidence": [
                {"event": e["event"], "labels": dict(e["labels"])}
                for e in candidate["evidence"]
            ],
            "kind": candidate["kind"],
            "name": candidate["name"],
            "op": "stage",
            "requested_permissions": list(candidate["requested_permissions"]),
            "scope": _scope(candidate.get("scope")),
            "tools": list(candidate["tools"]),
            "wider": candidate["wider"],
        },
        "labels": item_labels(candidate_labels(candidate["evidence"])),
        "type": "learn.proposal",
    }


def _lines(content: str) -> list[str]:
    parts = content.split("\n")
    if parts and parts[-1] == "":
        parts.pop()
    return [p[:-1] if p.endswith("\r") else p for p in parts]


def _is_hidden(char: str) -> bool:
    code = ord(char)
    if any(lo <= code <= hi for lo, hi in HIDDEN):
        return True
    return unicodedata.category(char) == "Cc" and char not in "\n\t"


def _has_ipv4(text: str) -> bool:
    token: list[str] = []
    tokens: list[str] = []
    for char in text:
        if char in "0123456789." and char.isascii():
            token.append(char)
        else:
            tokens.append("".join(token))
            token = []
    tokens.append("".join(token))
    for item in tokens:
        parts = item.split(".")
        if len(parts) == 4 and all(p and len(p) <= 3 and int(p) <= 255 for p in parts):
            return True
    return False


def _valid_name(name: str) -> bool:
    def lower_digit(c: str) -> bool:
        return ("a" <= c <= "z") or ("0" <= c <= "9")

    return (
        bool(name)
        and len(name.encode("utf-8")) <= MAX_NAME_BYTES
        and lower_digit(name[0])
        and all(lower_digit(c) or c in "-_" for c in name)
    )


def _mentions_word(text: str, word: str) -> bool:
    data, needle = text.encode("utf-8"), word.encode("utf-8")
    if not needle:
        return False

    def boundary(index: int) -> bool:
        if index < 0 or index >= len(data):
            return True
        b = data[index]
        return not (48 <= b <= 57 or 65 <= b <= 90 or 97 <= b <= 122 or b in (95, 45))

    start_from = 0
    while True:
        at = data.find(needle, start_from)
        if at < 0:
            return False
        end = at + len(needle)
        if boundary(at - 1) and boundary(end):
            return True
        start_from = end


def _fence_languages(content: str) -> list[str]:
    out = []
    for line in _lines(content):
        stripped = line.lstrip("".join(WHITESPACE))
        if stripped.startswith("```"):
            lang = stripped[3:].strip("".join(WHITESPACE))
            lang = "".join(c.lower() if "A" <= c <= "Z" else c for c in lang)
            if lang:
                out.append(lang)
    return out


def static_gate(candidate: dict[str, Any], grants: dict[str, Any]) -> dict[str, Any]:
    granted, known = set(grants["granted"]), sorted(set(grants["known"]))
    violations: list[tuple[str, str]] = []

    def push(code: str, detail: str) -> None:
        violations.append((code, detail))

    content = candidate["content"]
    lowered = content.lower()
    limit = (
        MAX_INSTRUCTION_BYTES
        if candidate["kind"] == "instruction_edit"
        else MAX_CONTENT_BYTES
    )
    size = len(content.encode("utf-8"))
    if _blank(content):
        push("learn.gate.empty", "the content is empty")
    if size > limit:
        push("learn.gate.size", f"{size} bytes exceeds {limit}")
    if not _valid_name(candidate["name"]):
        push(
            "learn.gate.name",
            "names are lowercase letters, digits, dashes and underscores",
        )
    if any(len(line) > MAX_LINE_CHARS for line in _lines(content)):
        push("learn.gate.format", "a line is longer than the limit")
    if any(_is_hidden(c) for c in content):
        push(
            "learn.gate.hidden_characters",
            "the text holds invisible or control characters",
        )
    marker = next((m for m in SCRIPT_MARKERS if m in lowered), None)
    if marker is not None:
        push("learn.gate.script", f"the text holds a script marker: {marker}")
    fence = next((f for f in _fence_languages(content) if f in EXECUTABLE_FENCES), None)
    if fence is not None:
        push("learn.gate.script", f"the text holds an executable code block: {fence}")
    net = next((m for m in NETWORK_MARKERS if m in lowered), None)
    if net is not None:
        push("learn.gate.network", f"the text points at the network: {net}")
    elif _has_ipv4(content):
        push("learn.gate.network", "the text holds an address")
    named = set(candidate["tools"])
    for tool in candidate["tools"]:
        if tool not in granted:
            push("learn.gate.tool_not_granted", f"{tool} is not granted")
    for tool in known:
        if not _mentions_word(content, tool):
            continue
        if tool not in named:
            push(
                "learn.gate.tool_undeclared",
                f"{tool} appears in the text but is not declared",
            )
        if tool not in granted:
            push("learn.gate.tool_not_granted", f"{tool} is not granted")
    if candidate["requested_permissions"]:
        push(
            "learn.gate.carries_authority",
            "a learned item never carries or requests a permission",
        )
    if not candidate["evidence"]:
        push("learn.gate.no_evidence", "no justifying events")
    hygiene = [p for p in HYGIENE_PHRASES if p in lowered]
    label = candidate_labels(candidate["evidence"])
    owner_reasons = []
    if label["integrity"] == "untrusted":
        owner_reasons.append("untrusted_evidence")
    if candidate["wider"]:
        owner_reasons.append("wider_scope")
    if candidate["kind"] == "instruction_edit":
        owner_reasons.append("instruction_edit")
    if hygiene:
        owner_reasons.append("suspicious_text")
    deduped: list[tuple[str, str]] = []
    for violation in violations:
        if not deduped or deduped[-1] != violation:
            deduped.append(violation)
    return {
        "codes": [code for code, _ in deduped],
        "details": [detail for _, detail in deduped],
        "hygiene": hygiene,
        "labels": label,
        "owner_reasons": owner_reasons,
        "passes": not deduped,
    }


def _blank(text: str) -> bool:
    return all(c in WHITESPACE for c in text)


def gate_event(
    proposal: str,
    label: dict[str, str],
    violations: list[dict[str, str]],
    hygiene: list[str],
    owner_reasons: list[str],
) -> dict[str, Any]:
    return {
        "body": {
            "hygiene": list(hygiene),
            "op": "gate",
            "owner_reasons": list(owner_reasons),
            "passed": not violations,
            "proposal": proposal,
            "violations": [
                {"code": v["code"], "detail": v["detail"]} for v in violations
            ],
        },
        "labels": item_labels(label),
        "type": "learn.proposal",
    }


def replay_adopts(report: dict[str, Any]) -> bool:
    return bool(
        report["must_pass_ok"]
        and report["target_improved"]
        and not report["target_regressed"]
    )


def replay_digest(report: dict[str, Any]) -> str:
    def flags(values: list[bool]) -> str:
        return "[" + ", ".join("true" if v else "false" for v in values) + "]"

    text = (
        f"{report['suite_hash']}|{report['candidate_hash']}|"
        f"{'true' if replay_adopts(report) else 'false'}"
    )
    for case in report["cases"]:
        text += (
            f"\n{case['turn']}|{flags(case['must_pass_with'])}|"
            f"{flags(case['target_without'])}|{flags(case['target_with'])}"
        )
    return crypto.sha256_id(text.encode("utf-8"))


def replay_event(
    proposal: str, label: dict[str, str], report: dict[str, Any]
) -> dict[str, Any]:
    return {
        "body": {
            "adopt": replay_adopts(report),
            "candidate_hash": report["candidate_hash"],
            "must_pass_ok": report["must_pass_ok"],
            "op": "replay",
            "proposal": proposal,
            "report_hash": replay_digest(report),
            "suite_hash": report["suite_hash"],
            "target_improved": report["target_improved"],
            "target_regressed": report["target_regressed"],
        },
        "labels": item_labels(label),
        "type": "learn.proposal",
    }


def decline_event(
    proposal: str, label: dict[str, str], reason: str, owner: bool
) -> dict[str, Any]:
    return {
        "body": {
            "by": "owner" if owner else "system",
            "op": "decline",
            "proposal": proposal,
            "reason": reason,
        },
        "labels": dict(OWNER_ACTION) if owner else item_labels(label),
        "type": "learn.proposal",
    }


def adoption_event(record: dict[str, Any]) -> dict[str, Any]:
    owner = record["mode"] == "owner"
    label = item_labels(record["labels"])
    scope = record["scope"]
    return {
        "body": {
            "at": record["at_ms"],
            "baseline": {
                "errors": record["baseline"]["errors"],
                "repeats": record["baseline"]["repeats"],
                "samples": record["baseline"]["samples"],
            },
            "content_hash": record["content_hash"],
            "mode": record["mode"],
            "policy": {
                "max_error_rise_permille": record["policy"]["max_error_rise_permille"],
                "max_repeat_rise_permille": record["policy"][
                    "max_repeat_rise_permille"
                ],
                "min_samples": record["policy"]["min_samples"],
            },
            "proposal": record["proposal"],
            "report_hash": record["report_hash"],
            "scope": {
                "project": scope.get("project"),
                "tool": scope.get("tool"),
                "wide": record["wide"],
            },
        },
        "labels": {
            "confidentiality": label["confidentiality"],
            "integrity": "owner",
        }
        if owner
        else label,
        "type": "learn.adoption",
    }


def reversal_event(
    adoption: str, proposal: str, hash_: str, reason: str, owner: bool
) -> dict[str, Any]:
    return {
        "body": {
            "adoption": adoption,
            "by": "owner" if owner else "system",
            "content_hash": hash_,
            "proposal": proposal,
            "reason": reason,
        },
        "labels": dict(OWNER_ACTION if owner else BOOKKEEPING),
        "type": "learn.reversal",
    }


def _permille(part: int, whole: int) -> int:
    return 0 if whole == 0 else min(part * 1000 // whole, U32_MAX)


def trial(
    baseline: dict[str, int], policy: dict[str, int], samples: list[dict[str, bool]]
) -> dict[str, Any]:
    current = {"errors": 0, "repeats": 0, "samples": 0}
    verdicts = []
    for sample in samples:
        current["samples"] = min(current["samples"] + 1, U32_MAX)
        current["errors"] = min(current["errors"] + int(sample["errored"]), U32_MAX)
        current["repeats"] = min(current["repeats"] + int(sample["repeated"]), U32_MAX)
        verdict = "continue"
        if current["samples"] >= policy["min_samples"]:
            if _permille(current["errors"], current["samples"]) > min(
                _permille(baseline["errors"], baseline["samples"])
                + policy["max_error_rise_permille"],
                U32_MAX,
            ):
                verdict = "withdraw:error_rate_rose"
            elif _permille(current["repeats"], current["samples"]) > min(
                _permille(baseline["repeats"], baseline["samples"])
                + policy["max_repeat_rise_permille"],
                U32_MAX,
            ):
                verdict = "withdraw:repeat_rate_rose"
        verdicts.append(verdict)
    return {"current": current, "verdicts": verdicts}


def _parse_stage(event: dict[str, Any]) -> dict[str, Any] | None:
    body = event["body"]
    if not isinstance(body, dict):
        return None
    if "scope" not in body or not isinstance(body.get("evidence"), list):
        return None
    scope, evidence_raw = body["scope"], body["evidence"]
    evidence = []
    for item in evidence_raw:
        label = item.get("labels") if isinstance(item, dict) else None
        ev = _text(item, "event")
        if ev is None or not _valid_labels(label):
            return None
        evidence.append({"event": ev, "labels": dict(label)})
    kind, name, content = (
        _text(body, "kind"),
        _text(body, "name"),
        _text(body, "content"),
    )
    stored = _text(body, "content_hash")
    wider = _flag(body, "wider")
    if (
        kind not in ITEM_KINDS
        or name is None
        or content is None
        or stored is None
        or wider is None
    ):
        return None
    return {
        "evidence": evidence,
        "gate": None,
        "id": event["id"],
        "item": {
            "content_hash": stored,
            "kind": kind,
            "name": name,
            "requested_permissions": _strings(body, "requested_permissions"),
            "scope": {"project": _text(scope, "project"), "tool": _text(scope, "tool")},
            "tools": _strings(body, "tools"),
            "wider": wider,
        },
        "labels": dict(event["labels"]),
        "replay": None,
        "status": {"name": "staged"},
    }


def _valid_labels(label: Any) -> bool:
    return (
        isinstance(label, dict)
        and label.get("confidentiality") in ("public", "private", "phi", "secret")
        and label.get("integrity") in ("owner", "tool-trusted", "untrusted")
    )


def _closed(proposal: dict[str, Any]) -> bool:
    status = proposal["status"]
    return status["name"] in ("reversed", "declined")


def owner_review_required(proposal: dict[str, Any]) -> bool:
    item = proposal["item"]
    return (
        item["kind"] == "instruction_edit"
        or item["wider"]
        or proposal["labels"]["integrity"] == "untrusted"
        or any(e["labels"]["integrity"] == "untrusted" for e in proposal["evidence"])
    )


def _adoption_problem(proposal: dict[str, Any], event: dict[str, Any]) -> str | None:
    body = event["body"]
    gate, replay = proposal["gate"], proposal["replay"]
    if gate is None:
        return "no gate record"
    if not gate["passed"]:
        return "the gate failed"
    if replay is None:
        return "no replay record"
    if not replay["adopt"]:
        return "the replay did not pass"
    item = proposal["item"]
    if _text(body, "content_hash") != item["content_hash"] or replay[
        "candidate_hash"
    ] != content_hash(item["kind"], item["name"], _content_of(proposal)):
        return "the content hash does not match the proposal"
    if _text(body, "report_hash") != replay["report_hash"]:
        return "the replay report does not match"
    mode = _text(body, "mode")
    if mode == "auto":
        if gate["owner_reasons"] or owner_review_required(proposal):
            return "the proposal needs the owner"
        if (
            proposal["labels"]["integrity"] == "untrusted"
            or event["labels"]["integrity"] == "untrusted"
        ):
            return "untrusted evidence is never adopted automatically"
        return None
    if mode == "owner":
        if event["labels"]["integrity"] != "owner":
            return "an owner adoption must carry the owner label"
        return None
    return "unknown adoption mode"


def _content_of(proposal: dict[str, Any]) -> str:
    return proposal["_content"]


def _rates(raw: Any) -> dict[str, int]:
    return {name: _u32(raw, name) or 0 for name in ("samples", "errors", "repeats")}


def _policy(raw: Any) -> dict[str, int]:
    out = {}
    for name, default in DEFAULT_POLICY.items():
        value = _u32(raw, name)
        out[name] = default if value is None else value
    return out


def ledger_fold(events: list[dict[str, Any]]) -> dict[str, Any]:
    proposals: list[dict[str, Any]] = []
    adoptions: list[dict[str, Any]] = []
    invalid: list[dict[str, str]] = []

    def find(pid: str | None) -> dict[str, Any] | None:
        return next((p for p in proposals if p["id"] == pid), None)

    def refuse(event: dict[str, Any], reason: str) -> None:
        invalid.append({"event": event["id"], "reason": FOLD_REASONS[reason]})

    for event in sorted(events, key=order_key):
        body = event["body"]
        if event["type"] == "learn.proposal":
            op = _text(body, "op") or ""
            if op == "stage":
                parsed = _parse_stage(event)
                if parsed is not None:
                    parsed["_content"] = body["content"]
                    item = parsed["item"]
                    if item["content_hash"] != content_hash(
                        item["kind"], item["name"], body["content"]
                    ):
                        refuse(event, "malformed proposal")
                    elif not parsed["evidence"]:
                        refuse(event, "no evidence")
                    elif parsed["labels"] != item_labels(
                        candidate_labels(parsed["evidence"])
                    ):
                        refuse(event, "labels do not match the evidence")
                    else:
                        proposals.append(parsed)
                    continue
                refuse(event, "malformed proposal")
                continue
            pid = _text(body, "proposal")
            if pid is None:
                continue
            proposal = find(pid)
            if proposal is None:
                refuse(event, "names an unknown proposal")
                continue
            if _closed(proposal) or proposal["status"]["name"] == "adopted":
                continue
            _apply_step(proposal, op, body)
        elif event["type"] == "learn.adoption":
            pid = _text(body, "proposal")
            if pid is None:
                refuse(event, "no proposal")
                continue
            proposal = find(pid)
            if proposal is None:
                refuse(event, "names an unknown proposal")
                continue
            if _closed(proposal):
                continue
            problem = _adoption_problem(proposal, event)
            if problem is not None:
                refuse(event, problem)
                continue
            if proposal["status"]["name"] == "adopted":
                refuse(event, "already adopted")
                continue
            scope = body.get("scope")
            wide = bool(_flag(scope, "wide"))
            if wide and not proposal["item"]["wider"]:
                refuse(event, "wider than the proposal asked")
                continue
            proposal["status"] = {"adoption": event["id"], "name": "adopted"}
            adoptions.append(
                {
                    "adoption": event["id"],
                    "baseline": _rates(body.get("baseline")),
                    "content_hash": proposal["item"]["content_hash"],
                    "kind": proposal["item"]["kind"],
                    "labels": dict(proposal["labels"]),
                    "mode": _text(body, "mode") or "",
                    "name": proposal["item"]["name"],
                    "policy": _policy(body.get("policy")),
                    "proposal": proposal["id"],
                    "scope": dict(proposal["item"]["scope"]),
                    "wide": wide,
                }
            )
        elif event["type"] == "learn.reversal":
            aid = _text(body, "adoption")
            if aid is None:
                continue
            at = next(
                (i for i, a in enumerate(adoptions) if a["adoption"] == aid), None
            )
            if at is None:
                refuse(event, "names an unknown adoption")
                continue
            adoption = adoptions.pop(at)
            proposal = find(adoption["proposal"])
            if proposal is not None:
                proposal["status"] = {
                    "adoption": aid,
                    "name": "reversed",
                    "reason": _text(body, "reason") or "",
                }
    for proposal in proposals:
        proposal.pop("_content", None)
    return {"active": adoptions, "invalid": invalid, "proposals": proposals}


def _apply_step(proposal: dict[str, Any], op: str, body: dict[str, Any]) -> None:
    status = proposal["status"]["name"]
    if op == "gate" and proposal["gate"] is None:
        violations = body.get("violations")
        codes = (
            [c for v in violations if (c := _text(v, "code")) is not None]
            if isinstance(violations, list)
            else []
        )
        passed = bool(_flag(body, "passed"))
        proposal["gate"] = {
            "codes": codes,
            "hygiene": _strings(body, "hygiene"),
            "owner_reasons": [
                r for r in _strings(body, "owner_reasons") if r in OWNER_REASONS
            ],
            "passed": passed,
        }
        proposal["status"] = (
            {"name": "gated"} if passed else {"codes": codes, "name": "rejected"}
        )
    elif op == "replay" and status in ("gated", "replayed", "waiting_owner"):
        adopt = bool(_flag(body, "adopt"))
        proposal["replay"] = {
            "adopt": adopt,
            "candidate_hash": _text(body, "candidate_hash") or "",
            "report_hash": _text(body, "report_hash") or "",
            "suite_hash": _text(body, "suite_hash") or "",
        }
        needs_owner = bool(
            proposal["gate"] and proposal["gate"]["owner_reasons"]
        ) or owner_review_required(proposal)
        if not adopt:
            proposal["status"] = {"name": "declined", "reason": "replay_failed"}
        elif needs_owner:
            proposal["status"] = {"name": "waiting_owner"}
        else:
            proposal["status"] = {"name": "replayed"}
    elif op == "decline":
        proposal["status"] = {"name": "declined", "reason": _text(body, "reason") or ""}


def register(op: Callable[[str], Callable[[Any], Any]]) -> None:
    @op("learn_stage")
    def _stage(i: dict[str, Any]) -> Any:
        event = stage_event(i["candidate"])
        return {"event": event}

    @op("learn_gate")
    def _gate(i: dict[str, Any]) -> Any:
        report = static_gate(i["candidate"], i["grants"])
        return {
            k: report[k]
            for k in ("codes", "hygiene", "labels", "owner_reasons", "passes")
        }

    @op("learn_gate_record")
    def _gate_record(i: dict[str, Any]) -> Any:
        return {
            "event": gate_event(
                i["proposal"],
                i["labels"],
                i["violations"],
                i["hygiene"],
                i["owner_reasons"],
            )
        }

    @op("learn_replay_record")
    def _replay_record(i: dict[str, Any]) -> Any:
        event = replay_event(i["proposal"], i["labels"], i["report"])
        return {
            "adopt": event["body"]["adopt"],
            "event": event,
            "report_hash": event["body"]["report_hash"],
        }

    @op("learn_decline")
    def _decline(i: dict[str, Any]) -> Any:
        return {
            "event": decline_event(i["proposal"], i["labels"], i["reason"], i["owner"])
        }

    @op("learn_adoption")
    def _adoption(i: dict[str, Any]) -> Any:
        return {"event": adoption_event(i["record"])}

    @op("learn_reversal")
    def _reversal(i: dict[str, Any]) -> Any:
        return {
            "event": reversal_event(
                i["adoption"],
                i["proposal"],
                i["content_hash"],
                i["reason"],
                i["owner"],
            )
        }

    @op("learn_trial")
    def _trial(i: dict[str, Any]) -> Any:
        return trial(i["baseline"], i["policy"], i["samples"])

    @op("learn_ledger_fold")
    def _ledger(i: dict[str, Any]) -> Any:
        return ledger_fold(i["events"])
