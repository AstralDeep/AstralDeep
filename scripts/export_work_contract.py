#!/usr/bin/env python3
"""Exports the framework Work contract (astral_* tools, scopes, fields) as checked-in
JSON from mcp_projection.py/mcp_authz.py/work_operations.py, so
sdk/astral_sdk/tools.py loads one drift-checked source of truth.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
DEFAULT_OUTPUT = REPO_ROOT / "sdk" / "astral_sdk" / "work_contract.json"

CONTRACT_FORMAT_VERSION = 1


def _ensure_backend_importable() -> None:
    backend = str(BACKEND_ROOT)
    if backend not in sys.path:
        sys.path.insert(0, backend)


def build_contract() -> dict[str, Any]:
    _ensure_backend_importable()

    from astralplane.repositories.framework_credentials import (
        FRAMEWORK_CREDENTIAL_SCOPES,
    )
    from orchestrator.mcp_authz import MCP_AUDIENCE, MCP_SCOPES
    from orchestrator.mcp_projection import _WORK_TOOL_SPECS
    from orchestrator.work_operations import DISPATCHABLE_TOOL_NAMES, dispatch_name
    from orchestrator.work_service import (
        _DISPOSITIONS,
        _MONEY_STATUS,
        _SAFE_ERRORS,
        _USAGE_BASIS,
        _USAGE_DIMENSIONS,
    )
    from shared.protocol import MCP_PROTOCOL_VERSION, MCP_SUPPORTED_PROTOCOL_VERSIONS

    tools: dict[str, Any] = {}
    for name in DISPATCHABLE_TOOL_NAMES:
        spec = _WORK_TOOL_SPECS[name]
        tools[name] = {
            "scope": spec["scope"],
            "description": spec["description"],
            "read_only": spec["readOnly"],
            "input_schema": spec["input_schema"],
            "facade_method": dispatch_name(name),
        }

    return {
        "contract_format_version": CONTRACT_FORMAT_VERSION,
        "source": "AstralDeep backend (orchestrator.mcp_projection, "
                  "orchestrator.work_operations, orchestrator.work_service, "
                  "orchestrator.mcp_authz, astralplane.repositories.framework_credentials)",
        "mcp": {
            "protocol_version": MCP_PROTOCOL_VERSION,
            "supported_protocol_versions": sorted(MCP_SUPPORTED_PROTOCOL_VERSIONS),
            "audience": MCP_AUDIENCE,
            "entry_scopes": list(MCP_SCOPES),
        },
        "framework_credential_scopes": sorted(FRAMEWORK_CREDENTIAL_SCOPES),
        "dispatchable_tool_names": list(DISPATCHABLE_TOOL_NAMES),
        "tools": tools,
        "operation": {
            # Hand-pinned; a conformance test cross-checks it live
            "fields": [
                "id", "revision", "instruction_revision", "control_epoch", "title",
                "kind", "disposition", "lifecycle", "phase", "created_at", "updated_at",
                "next_wake_at", "deadline_at", "schema_supported", "safe_error_code", "usage",
            ],
            "dispositions": sorted(_DISPOSITIONS),
            "safe_error_codes": sorted(_SAFE_ERRORS),
            "usage_dimensions": list(_USAGE_DIMENSIONS),
            "usage_basis_values": sorted(_USAGE_BASIS),
            "money_status_values": sorted(_MONEY_STATUS),
        },
    }


def _write(contract: dict[str, Any], out_path: Path) -> str:
    text = json.dumps(contract, indent=2, sort_keys=True) + "\n"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT,
                         help="Where to write the contract JSON (default: sdk/astral_sdk/work_contract.json)")
    parser.add_argument("--check", action="store_true",
                         help="Do not write; exit 1 if the freshly built contract differs from --out's contents")
    args = parser.parse_args(argv)

    contract = build_contract()
    text = json.dumps(contract, indent=2, sort_keys=True) + "\n"

    if args.check:
        existing = args.out.read_text(encoding="utf-8") if args.out.exists() else None
        if existing != text:
            sys.stderr.write(
                f"{args.out} is stale relative to the live backend contract.\n"
                f"Run: python scripts/export_work_contract.py --out {args.out}\n"
            )
            return 1
        return 0

    _write(contract, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
