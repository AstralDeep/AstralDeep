"""Run configuration for the verification harness: mode, target, output location,
budgets, and the names (never values) of identity-provider and other secret-bearing
environment variables. Read by every harness module and driver.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Literal, Optional

# Names only; values must never be logged, stored, or embedded.
KEYCLOAK_CRED_ENV_NAMES: tuple[str, ...] = (
    "KEYCLOAK_AUTHORITY",
    "KEYCLOAK_CLIENT_ID",
    "KEYCLOAK_CLIENT_SECRET",
    "KEYCLOAK_REALM",
    "KEYCLOAK_TOKEN_URL",
)

OTHER_SECRET_ENV_NAMES: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "AUDIT_HMAC_SECRET",
    "AGENT_API_KEY",
    "WEB_SESSION_SECRET",
    "WEB_SESSION_ENC_KEY",
    "CREDENTIAL_ENCRYPTION_KEY",
    "TYPESAFE_API_KEY",
    "TYPESAFE_BASE_URL",
    "TYPESAFE_DEFAULT_MODEL",
)

Mode = Literal["in_process", "external"]
AuthMode = Literal["real_keycloak", "mock_inprocess"]


@dataclass
class RunConfig:
    mode: Mode = "in_process"
    run_id: str = "__verif__local"
    out_dir: str = field(
        default_factory=lambda: os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "verification",
            ".runs",
        )
    )
    base_url: Optional[str] = None
    personas: List[str] = field(default_factory=list)
    max_steps: int = 8
    max_turns: int = 6
    timeout_s: float = 60.0
    max_retries: int = 2
    strict: bool = False
    llm_judge: bool = False

    def __post_init__(self) -> None:
        normalized = str(self.mode).replace("-", "_")
        if normalized not in ("in_process", "external"):
            raise ValueError(f"invalid mode: {self.mode!r}")
        self.mode = normalized  # type: ignore[assignment]
        if not self.run_id or not str(self.run_id).startswith("__verif__"):
            raise ValueError("run_id must be namespaced with the __verif__ prefix")

    @property
    def run_dir(self) -> str:
        return os.path.join(self.out_dir, self.run_id)

    def secret_values(self) -> List[str]:
        names = KEYCLOAK_CRED_ENV_NAMES + OTHER_SECRET_ENV_NAMES
        out: List[str] = []
        for name in names:
            val = os.environ.get(name)
            if val and len(val.strip()) >= 6:
                out.append(val.strip())
        return out

    def keycloak_available(self) -> bool:
        return all(
            os.environ.get(n)
            for n in ("KEYCLOAK_AUTHORITY", "KEYCLOAK_CLIENT_ID", "KEYCLOAK_CLIENT_SECRET")
        )
