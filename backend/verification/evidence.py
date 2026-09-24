"""Captured evidence and secret-safe redaction: CapturedEvidence holds a scenario's
observations for verdict and replay; redact() scrubs credential-shaped values before
persistence and flags any near-exposure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

_MASK = "***REDACTED***"

_GENERIC_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{6,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
)


def _redact_text(text: str, secret_values: List[str]) -> Tuple[str, bool]:
    hit = False
    for sv in secret_values:
        if sv and sv in text:
            text = text.replace(sv, _MASK)
            hit = True
    for pat in _GENERIC_PATTERNS:
        if pat.search(text):
            text = pat.sub(_MASK, text)
            hit = True
    return text, hit


def redact(obj: Any, secret_values: List[str]) -> Tuple[Any, bool]:
    near = False
    if isinstance(obj, str):
        cleaned, hit = _redact_text(obj, secret_values)
        return cleaned, hit
    if isinstance(obj, dict):
        out: Dict[Any, Any] = {}
        for k, v in obj.items():
            cv, h = redact(v, secret_values)
            out[k] = cv
            near = near or h
        return out, near
    if isinstance(obj, (list, tuple)):
        out_list = []
        for v in obj:
            cv, h = redact(v, secret_values)
            out_list.append(cv)
            near = near or h
        return out_list, near
    return obj, False


@dataclass
class CapturedEvidence:
    evidence_id: str
    scenario_id: str
    run_mode: str
    messages: List[Dict[str, Any]] = field(default_factory=list)
    components: List[Dict[str, Any]] = field(default_factory=list)
    workspace_state: List[Dict[str, Any]] = field(default_factory=list)
    audit_rows: List[Dict[str, Any]] = field(default_factory=list)
    audit_chain_ok: Any = True
    client_inspection: Dict[str, Any] = field(default_factory=dict)
    device_diff: Dict[str, Any] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)
    near_exposure: bool = False

    def redacted(self, secret_values: List[str]) -> "CapturedEvidence":
        msgs, h1 = redact(self.messages, secret_values)
        comps, h2 = redact(self.components, secret_values)
        ws, h3 = redact(self.workspace_state, secret_values)
        audit, h4 = redact(self.audit_rows, secret_values)
        extra, h5 = redact(self.extra, secret_values)
        return CapturedEvidence(
            evidence_id=self.evidence_id,
            scenario_id=self.scenario_id,
            run_mode=self.run_mode,
            messages=msgs,
            components=comps,
            workspace_state=ws,
            audit_rows=audit,
            audit_chain_ok=self.audit_chain_ok,
            client_inspection=self.client_inspection,
            device_diff=self.device_diff,
            extra=extra,
            near_exposure=any((h1, h2, h3, h4, h5)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "scenario_id": self.scenario_id,
            "run_mode": self.run_mode,
            "messages": self.messages,
            "components": self.components,
            "workspace_state": self.workspace_state,
            "audit_rows": self.audit_rows,
            "audit_chain_ok": self.audit_chain_ok,
            "client_inspection": self.client_inspection,
            "device_diff": self.device_diff,
            "extra": self.extra,
            "near_exposure": self.near_exposure,
        }


def flatten_components(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        mtype = msg.get("type")
        if mtype == "ui_render":
            for c in msg.get("components", []) or []:
                if isinstance(c, dict):
                    out.append(c)
        elif mtype == "ui_upsert":
            for op in msg.get("ops", []) or []:
                if isinstance(op, dict) and isinstance(op.get("component"), dict):
                    out.append(op["component"])
    return out
