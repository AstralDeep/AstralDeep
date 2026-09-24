"""Scenario catalogue (backend/verification/isolation.py, personas.py): a Scenario is
one persona-conditioned flow — file, query, acting principal, run mode, and expected
properties — the atomic unit the runner drives and verifies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from verification.isolation import Principal, make_principal
from verification.personas import Persona, all_personas

TANGIBLE_UI = "tangible_ui"
DELEGATED_AUTHORITY = "delegated_authority"
BACKEND_ONLY_UI = "backend_only_ui"


@dataclass
class Scenario:
    scenario_id: str
    persona: Persona
    principal: Principal
    auth_mode: str
    expected_properties: List[str] = field(default_factory=list)
    warrants_ui: bool = True

    @property
    def query(self) -> str:
        return self.persona.query


def build_scenarios(
    run_id: str,
    auth_mode: str,
    persona_keys: Optional[List[str]] = None,
    properties: Optional[List[str]] = None,
) -> List[Scenario]:
    props = properties or [TANGIBLE_UI, BACKEND_ONLY_UI]
    out: List[Scenario] = []
    for persona in all_personas(persona_keys):
        principal = make_principal(run_id, persona.key, role="primary",
                                   roles=list(persona.roles))
        out.append(
            Scenario(
                scenario_id=f"{persona.key}:primary",
                persona=persona,
                principal=principal,
                auth_mode=auth_mode,
                expected_properties=list(props),
                warrants_ui=persona.warrants_ui,
            )
        )
    return out
