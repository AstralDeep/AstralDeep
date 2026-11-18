"""Boot posture for the personal node: refuses to start with exit 78 when production secrets are missing or placeholders, the node key is unusable, or legacy settings are present.
The orchestrator entrypoint and the agent-key check call it, and it imports no sign-in or session code.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("orchestrator.boot_posture")

_DEV_VALUES = ("development", "dev")


def is_dev_mode() -> bool:
    return os.getenv("ASTRAL_ENV", "").strip().lower() in _DEV_VALUES


TYPESAFE_ENV_NAMES = (
    "TYPESAFE_API_KEY",
    "TYPESAFE_BASE_URL",
    "TYPESAFE_DEFAULT_MODEL",
)


def typesafe_env_names_present() -> tuple[str, ...]:
    return tuple(
        name for name in TYPESAFE_ENV_NAMES if os.getenv(name, "").strip()
    )


def warn_on_typesafe_environment() -> tuple[str, ...]:
    present = typesafe_env_names_present()
    if present:
        logger.warning(
            "%s set in the environment. TypeSafe configuration is never read "
            "from the environment: routing uses each user's own "
            "saved key. These variables have no effect and production mode "
            "refuses to start with them set.",
            ", ".join(present),
        )
    return present


SNAPSHOT_LINKS_ENV = "FF_ARTIFACT_SHARING"


def refuse_snapshot_links_on_personal_node() -> bool:
    from shared import node_identity

    if not node_identity.is_personal():
        return False
    requested = os.getenv(SNAPSHOT_LINKS_ENV, "").strip().lower() in ("true", "1", "yes")
    if requested:
        logger.warning(
            "%s is ignored on a personal node: snapshot links are off there and "
            "sharing runs through mesh contacts.",
            SNAPSHOT_LINKS_ENV,
        )
    os.environ[SNAPSHOT_LINKS_ENV] = "false"
    return requested


_DEV_PLACEHOLDER_SECRETS = (
    "dev-audit-hmac-secret-change-me-in-prod",
    "change-me",
)


def assert_production_posture() -> None:
    from shared import node_identity

    refuse_snapshot_links_on_personal_node()
    if node_identity.is_institution():
        from shared import institution_profile

        institution_profile.enforce()
    mock_requested = os.getenv("USE_MOCK_AUTH", "").strip().lower() in ("1", "true", "yes")
    if is_dev_mode():
        if mock_requested:
            logger.warning("USE_MOCK_AUTH is ignored: mock auth was removed.")
        if node_identity.is_personal():
            problems = _personal_node_problems(node_identity)
            if problems:
                logger.error("Personal node refuses to start:\n%s", "\n".join(f"  {p}" for p in problems))
                raise SystemExit(78)
        warn_on_typesafe_environment()
        return
    problems = []
    typesafe_present = typesafe_env_names_present()
    if typesafe_present:
        problems.append(
            f"{', '.join(typesafe_present)} set in the environment. LLM "
            f"routing is bring-your-own-key: there is no deployment-wide "
            f"TypeSafe credential and no table that can hold one. A process "
            f"configured this way would either do nothing with the value or "
            f"route every user through one operator key. Unset "
            f"{', '.join(typesafe_present)}; users save their own key in LLM "
            f"settings."
        )
    if mock_requested:
        problems.append(
            "USE_MOCK_AUTH is set but mock auth was removed. Unset USE_MOCK_AUTH; "
            "a personal node authenticates with mesh keys and a server node "
            "needs KEYCLOAK_*."
        )
    if not os.getenv("CREDENTIAL_ENCRYPTION_KEY", "").strip():
        problems.append(
            "CREDENTIAL_ENCRYPTION_KEY is unset — OAuth/Fernet credentials would be "
            "encrypted under an auto-generated key that is lost on an ephemeral "
            "volume (silent fail-open). Generate one: python -c \"from "
            "cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    audit_secret = os.getenv("AUDIT_HMAC_SECRET", "").strip()
    if not audit_secret or audit_secret in _DEV_PLACEHOLDER_SECRETS:
        problems.append(
            "AUDIT_HMAC_SECRET is unset or still the shipped dev placeholder — "
            "the audit hash chain would be forgeable. Set a high-entropy value."
        )
    personal = node_identity.is_personal()
    if not personal:
        for var in ("KEYCLOAK_AUTHORITY", "KEYCLOAK_CLIENT_ID", "KEYCLOAK_CLIENT_SECRET"):
            if not os.getenv(var, "").strip():
                problems.append(f"{var} is unset — the OIDC flow cannot operate.")
    if personal:
        problems.extend(_personal_node_problems(node_identity))
    try:
        node_identity.bind_host()
    except node_identity.NodeIdentityError as exc:
        problems.append(str(exc))
    from orchestrator import vessel_executor

    problems.extend(vessel_executor.configuration_problems())
    agent_key = os.getenv("AGENT_API_KEY", "").strip()
    if agent_key and (agent_key in _DEV_PLACEHOLDER_SECRETS or len(agent_key) < 16):
        problems.append(
            "AGENT_API_KEY is a shipped placeholder or too short (<16 chars) — "
            "set a high-entropy value so agent registrations cannot be forged."
        )
    if problems:
        logger.critical(
            "REFUSING TO START (production posture, ASTRAL_ENV != development) — "
            "fix the following before deploying:\n%s",
            "\n".join(f"  [{i + 1}] {p}" for i, p in enumerate(problems)),
        )
        raise SystemExit(78)
    if not os.getenv("AGENT_API_KEY", "").strip():
        logger.warning(
            "AGENT_API_KEY is unset in production mode: ALL agent registrations "
            "will be refused (fail closed). Configure it if this "
            "deployment is meant to run specialist agents."
        )


def _personal_node_problems(node_identity) -> list:
    problems = []
    try:
        node_identity.load_node_identity()
    except node_identity.NodeIdentityError as exc:
        problems.append(
            f"The node signing key is unusable ({exc}). Check "
            f"{node_identity.DATA_DIR_ENV} is writable by this process and the key "
            f"file is mode 0600."
        )
    try:
        node_identity.retire_legacy_owner()
    except node_identity.NodeIdentityError as exc:
        problems.append(f"The legacy owner binding cannot be retired ({exc}).")
    return problems
