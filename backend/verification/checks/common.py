"""Shared vocabulary_ok check used by both the tangible-UI and backend-only-UI
properties (backend/verification/checks/tangible_ui.py, thin_client.py): every
delivered component type must be in webrender's published registry.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from verification.checks.base import Check, CheckResult, no, ok, unsure
from verification.evidence import CapturedEvidence


def _published_types() -> set:
    from webrender import allowed_primitive_types

    return set(allowed_primitive_types())


def _component_types(components: List[Dict[str, Any]]) -> List[str]:
    return [c.get("type") for c in components if isinstance(c, dict) and c.get("type")]


def _vocabulary_run(evidence: CapturedEvidence, inputs: Dict[str, Any]) -> CheckResult:
    allowed = _published_types()
    types = _component_types(evidence.components)
    out_of_vocab = sorted({t for t in types if t not in allowed})
    if not types:
        return unsure("us.vocabulary_ok", "no components to check", types=types)
    if out_of_vocab:
        return no(
            "us.vocabulary_ok",
            f"out-of-vocabulary component types: {out_of_vocab}",
            out_of_vocab=out_of_vocab,
        )
    return ok("us.vocabulary_ok", "all component types are in the published set",
              types=sorted(set(types)))


def _vocabulary_counter(evidence: CapturedEvidence, inputs: Dict[str, Any]) -> CheckResult:
    allowed = _published_types()
    bad = [t for t in _component_types(evidence.components) if t not in allowed]
    if bad:
        return ok("us.vocabulary_ok.counter", f"found out-of-vocab types: {bad}")
    return no("us.vocabulary_ok.counter", "no out-of-vocabulary types found")


def vocabulary_check(property_name: str) -> Check:
    return Check(
        check_id="vocabulary_ok",
        property=property_name,
        run_fn=_vocabulary_run,
        counter_fn=_vocabulary_counter,
    )


def json_blob(components: List[Dict[str, Any]]) -> str:
    try:
        return json.dumps(components, default=str)
    except (TypeError, ValueError):
        return str(components)
