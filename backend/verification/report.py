"""Dual run record and Markdown report (backend/verification/config.py, evidence.py,
verdict.py): writes verdicts.json and report.md from the same record, with
differentiation claims grounded only in passing verdicts.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from verification.config import RunConfig
from verification.evidence import CapturedEvidence
from verification.verdict import Outcome, Verdict

_PROPERTY_LABEL = {
    "tangible_ui": "Tangible server-driven UI",
    "delegated_authority": "Delegated authority",
    "backend_only_ui": "Backend-only UI",
}


def _coverage(evidence: Dict[str, CapturedEvidence]) -> Dict[str, List[str]]:
    cats: set = set()
    types: set = set()
    for ev in evidence.values():
        for c in ev.components:
            if isinstance(c, dict) and c.get("type"):
                types.add(c["type"])
        cat = (ev.extra or {}).get("file_category")
        if cat:
            cats.add(cat)
    return {"file_categories": sorted(cats), "component_types": sorted(types)}


def _differentiation(verdicts: List[Verdict], coverage: Dict[str, List[str]]) -> List[str]:
    passed = {
        v.refs.get("check")
        for v in verdicts
        if v.outcome == Outcome.PASS and v.scope == "check"
    }
    claims: List[str] = []
    if "us1.component_from_file" in passed and coverage["component_types"]:
        claims.append(
            "Interactive components built from the uploaded file's real contents "
            f"({', '.join(coverage['component_types'])})."
        )
    if "us1.persisted_with_identity" in passed and "us1.re_executable" in passed:
        claims.append(
            "Components persisted under a stable identity and re-executable via the "
            "permission-gated action path."
        )
    if "us2.delegation_attribution" in passed and "us2.audit_chain_unbroken" in passed:
        claims.append(
            "Every action recorded on-behalf-of the user by a scoped delegate agent "
            "in an unbroken, tamper-evident audit chain."
        )
    if "us2.cross_user_refused" in passed:
        claims.append("Strict cross-user isolation of attachments and components.")
    if "us1.autoparse_safe" in passed:
        claims.append("Safe on-demand parser drafting (held for admin approval) for an "
                      "unknown file type.")
    if "us3.no_client_construction" in passed:
        claims.append("UI authored entirely server-side; the client only injects output "
                      "and forwards actions.")
    return claims


def build_record(
    config: RunConfig,
    verdicts: List[Verdict],
    evidence: Dict[str, CapturedEvidence],
    *,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
    auth_mode: str = "mock_inprocess",
    personas: Optional[List[str]] = None,
) -> Dict[str, Any]:
    coverage = _coverage(evidence)
    near = any(ev.near_exposure for ev in evidence.values())
    flags: List[str] = []
    if near:
        flags.append("credential_near_exposure")
    n_uncertain = sum(1 for v in verdicts if v.outcome == Outcome.UNCERTAIN)
    return {
        "run_id": config.run_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "mode": config.mode,
        "auth_mode": auth_mode,
        "personas": personas or [],
        "coverage": coverage,
        "verdicts": [v.to_dict() for v in verdicts],
        "uncertain_ratio": round(n_uncertain / len(verdicts), 4) if verdicts else 0.0,
        "differentiation": _differentiation(verdicts, coverage),
        "flags": flags,
        "evidence": {eid: ev.to_dict() for eid, ev in evidence.items()},
    }


def _render_markdown(record: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append(f"# Verification Report — {record['run_id']}")
    lines.append("")
    lines.append(f"- Mode: **{record['mode']}**")
    am = record["auth_mode"]
    lines.append(f"- Authority mode: **{am}**")
    if am == "mock_inprocess":
        lines.append("  - NOTE: this is a development mock run — NOT a real-realm "
                     "delegated-authority guarantee.")
    lines.append(f"- Window: {record.get('started_at')} -> {record.get('finished_at')}")
    lines.append(f"- Personas: {', '.join(record['personas']) or '(none)'}")
    lines.append(f"- Uncertain ratio: {record['uncertain_ratio']}")
    if record["flags"]:
        lines.append(f"- Flags: {', '.join(record['flags'])}")
    lines.append("")

    lines.append("## Verdicts")
    lines.append("")
    lines.append("| Persona | Property | Check | Outcome | Evidence |")
    lines.append("|---|---|---|---|---|")
    for v in record["verdicts"]:
        refs = v.get("refs", {})
        prop = _PROPERTY_LABEL.get(refs.get("property", ""), refs.get("property", "-"))
        lines.append(
            f"| {refs.get('persona', '-')} | {prop} | {refs.get('check', v.get('scope'))} "
            f"| {v['outcome'].upper()} | {v.get('reason', '')[:80]} |"
        )
    lines.append("")

    cov = record["coverage"]
    lines.append("## Coverage (observed only)")
    lines.append("")
    lines.append(f"- File categories: {', '.join(cov['file_categories']) or '(none)'}")
    lines.append(f"- Component types: {', '.join(cov['component_types']) or '(none)'}")
    lines.append("")

    lines.append("## Differentiation (what a text-only assistant cannot do)")
    lines.append("")
    if record["differentiation"]:
        for d in record["differentiation"]:
            lines.append(f"- {d}")
    else:
        lines.append("- (no differentiation claims corroborated by this run)")
    lines.append("")
    return "\n".join(lines)


def write_report(record: Dict[str, Any], run_dir: str) -> Dict[str, str]:
    os.makedirs(run_dir, exist_ok=True)
    json_path = os.path.join(run_dir, "verdicts.json")
    md_path = os.path.join(run_dir, "report.md")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, default=str)
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(_render_markdown(record))
    return {"json": json_path, "markdown": md_path}
