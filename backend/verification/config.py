"""Run configuration for the verification harness (T003).

Holds the knobs for one run: mode, target, output location, budgets, and the
*names* (never the values) of the identity-provider credentials. Credential
values are read from the environment by name only and never embedded, logged, or
persisted (FR-022 / SC-011).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Literal, Optional

# Identity-provider credential environment-variable NAMES (FR-022). The harness
# references these by name; their VALUES are read from os.environ only to (a)
# authenticate in external mode and (b) drive redaction so a value can never leak
# into an artifact. They are never stored on the RunConfig.
KEYCLOAK_CRED_ENV_NAMES: tuple[str, ...] = (
    "KEYCLOAK_AUTHORITY",
    "KEYCLOAK_CLIENT_ID",
    "KEYCLOAK_CLIENT_SECRET",
    "KEYCLOAK_REALM",
    "KEYCLOAK_TOKEN_URL",
)

# Additional secret-bearing env names whose values must be scrubbed from
# artifacts if they ever appear (defence in depth — these are product secrets,
# not harness inputs).
OTHER_SECRET_ENV_NAMES: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "AUDIT_HMAC_SECRET",
    "AGENT_API_KEY",
    "WEB_SESSION_SECRET",
    "WEB_SESSION_ENC_KEY",
    "CREDENTIAL_ENCRYPTION_KEY",
    # Feature 089. These names must never configure an Astral process -- the
    # production boot gate refuses them outright (FR-005). They are listed here
    # so that if one is nevertheless present in a developer's environment, its
    # value is scrubbed from every harness artifact rather than captured in one.
    "TYPESAFE_API_KEY",
    "TYPESAFE_BASE_URL",
    "TYPESAFE_DEFAULT_MODEL",
)

Mode = Literal["in_process", "external"]
AuthMode = Literal["real_keycloak", "mock_inprocess"]


@dataclass
class RunConfig:
    """Configuration for a single harness run.

    Attributes:
        mode: ``in_process`` (scripted LLM, the CI gate) or ``external``
            (live endpoints + real Keycloak, opt-in).
        run_id: Namespace for principals + artifacts. Callers supply a stamp;
            scripts cannot read the clock.
        out_dir: Gitignored artifacts root; a ``<run_id>/`` subdir is created.
        base_url: External-mode target (e.g. https://sandbox.ai.uky.edu).
        personas: Restrict to these persona keys (empty = all).
        max_steps / max_turns / timeout_s / max_retries: Per-scenario hard
            budgets (FR-005 / FR-006).
        strict: Any ``uncertain`` verdict becomes a non-zero exit.
        llm_judge: Enable optional LLM-as-judge enrichment (real LLM only;
            resolves to ``na`` when no LLM is available, e.g. in CI).
    """

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
        # Normalize the CLI-friendly hyphen form to the internal underscore form
        # (I1): ``--mode in-process`` -> ``in_process``.
        normalized = str(self.mode).replace("-", "_")
        if normalized not in ("in_process", "external"):
            raise ValueError(f"invalid mode: {self.mode!r}")
        self.mode = normalized  # type: ignore[assignment]
        if not self.run_id or not str(self.run_id).startswith("__verif__"):
            raise ValueError("run_id must be namespaced with the __verif__ prefix")

    @property
    def run_dir(self) -> str:
        """Per-run artifacts directory ``<out_dir>/<run_id>/``."""
        return os.path.join(self.out_dir, self.run_id)

    def secret_values(self) -> List[str]:
        """Live values of every secret-bearing env name, for redaction only.

        Reads ``os.environ`` by name; never stores the values on the config.
        Empty/short values are ignored (a 1-char secret is not a useful redaction
        target and risks masking unrelated text).
        """
        names = KEYCLOAK_CRED_ENV_NAMES + OTHER_SECRET_ENV_NAMES
        out: List[str] = []
        for name in names:
            val = os.environ.get(name)
            if val and len(val.strip()) >= 6:
                out.append(val.strip())
        return out

    def keycloak_available(self) -> bool:
        """True when the minimum real-Keycloak credentials are present by name."""
        return all(
            os.environ.get(n)
            for n in ("KEYCLOAK_AUTHORITY", "KEYCLOAK_CLIENT_ID", "KEYCLOAK_CLIENT_SECRET")
        )
