#!/usr/bin/env python3
"""Export Deep's framework-facing Work contract as one checked-in JSON schema.

The Astral SDK (``sdk/``) and the backend's own MCP projection
(``backend/orchestrator/mcp_projection.py``) must describe exactly the same
set of ``astral_*`` tools, scopes and operation fields. Rather than hand-copy
those names into the SDK (where they silently drift), this script imports the
server's OWN source-of-truth constants and writes them out as one JSON
document that ``sdk/astral_sdk/tools.py`` loads at import time and that
``scripts/tests/test_export_work_contract.py`` re-runs to catch drift on every
commit — if this script's output no longer matches the checked-in file, the
server contract changed and the SDK needs a matching (reviewed) update.

Nothing here talks to Postgres, a network, or a running Deep instance: every
value is a Python-level constant already present in the reviewed backend
modules it imports. Running it requires only the backend package importable
on ``sys.path`` (``backend/`` on the repo root, exactly like every other
in-repo tool).

Usage::

    python scripts/export_work_contract.py                  # writes the default path
    python scripts/export_work_contract.py --check           # exits 1 on drift, writes nothing
    python scripts/export_work_contract.py --out some/path.json
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

#: Bumped only when this script's OWN output shape changes (not when the
#: server's tool/field set changes — that is tracked by the document's own
#: content and caught by the drift test).
CONTRACT_FORMAT_VERSION = 1


def _ensure_backend_importable() -> None:
    backend = str(BACKEND_ROOT)
    if backend not in sys.path:
        sys.path.insert(0, backend)


def build_contract() -> dict[str, Any]:
    """Return the exportable contract as a plain JSON-serializable dict.

    Every value is read from an existing backend module's own module-level
    constant or dataclass field default — nothing is re-typed by hand.
    """
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
            # The exact key set ``orchestrator.work_service._public`` returns
            # for one operation. Not itself an importable constant (it is a
            # dict literal), so it is pinned here by name and cross-checked by
            # ``backend/tests/test_framework_conformance_088.py`` against a
            # REAL live response from the running server.
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
