"""Feature 054 — T033: the legacy operator-default env credentials are INERT.

Two layers:

* Behavior (SC-007): with ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` /
  ``LLM_MODEL`` / ``KNOWLEDGE_LLM_MODEL`` set in the environment, a freshly
  constructed orchestrator still treats an unconfigured user as unconfigured
  — resolution raises ``LLMUnavailable`` and the gate predicate stays False.
  There is no code path that turns the env trio into a usable default.
* Mechanism absence (SC-004 / FR-001): a source-tree guard walks
  ``backend/**/*.py`` asserting no live ``os.getenv`` / ``os.environ`` read
  of the retired variables remains — removal, not merely unset.

Feature 089 adds a third subject with the same two layers. TypeSafe routing is
bring-your-own-key, so ``TYPESAFE_API_KEY`` / ``TYPESAFE_BASE_URL`` /
``TYPESAFE_DEFAULT_MODEL`` must configure nothing: a production process refuses
to boot with them set, a development process warns, a sandboxed child never
sees them, and no source file reads them.

References: specs/054-byo-llm-setup/spec.md FR-001/FR-002, SC-004, SC-007;
specs/089-typesafe-a8p-integration/spec.md FR-005.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent

LEGACY_VARS = {
    "OPENAI_API_KEY": "sk-shipped-operator-key-000000000000000",
    "OPENAI_BASE_URL": "https://operator-default.example.com/v1",
    "LLM_MODEL": "operator-default-model",
    "KNOWLEDGE_LLM_MODEL": "operator-knowledge-model",
}

# Feature 089. The value is a synthetic canary that matches the committed
# TypeSafe pattern; it is not a credential.
TYPESAFE_VARS = {
    "TYPESAFE_API_KEY": "ts_live_CANARY0000NOTAREALKEY000000",
    "TYPESAFE_BASE_URL": "https://operator-typesafe.example.com",
    "TYPESAFE_DEFAULT_MODEL": "operator-default-jev",
}


# ---------------------------------------------------------------------------
# (a) SC-007 — env vars change nothing
# ---------------------------------------------------------------------------

def test_legacy_env_vars_do_not_configure_any_llm(
    monkeypatch,
    orchestrator_factory,
):
    for name, value in LEGACY_VARS.items():
        monkeypatch.setenv(name, value)

    # Fresh construction WITH the vars set — boot must succeed (FR-003) and
    # must not mint any default credential from them.
    from unittest.mock import AsyncMock

    orch = orchestrator_factory()
    orch._record_llm_unconfigured = AsyncMock()

    uid = f"envinert054-{uuid.uuid4().hex[:10]}"
    ws = MagicMock()
    orch.ui_sessions[ws] = {"sub": uid, "preferred_username": f"{uid}@example"}

    async def _scenario():
        # The gate predicate ignores the environment entirely.
        assert await orch.llm_configured_for(uid) is False
        # User-context resolution fails closed...
        with pytest.raises(orch._LLMUnavailable):
            await orch._resolve_llm_client_for(ws)
        # ...and so does system-context resolution (no system row either;
        # the env vars must not become a system credential).
        if await orch._llm_store.get_system() is None:
            with pytest.raises(orch._LLMUnavailable):
                await orch._resolve_llm_client_for(None)
        # _call_llm degrades to the audited (None, None) shape, no crash.
        message, usage = await orch._call_llm(ws, [{"role": "user", "content": "hi"}])
        assert message is None and usage is None

    asyncio.run(_scenario())


# ---------------------------------------------------------------------------
# (b) SC-004 — mechanism-absence guard over the source tree
# ---------------------------------------------------------------------------

# Reads of the retired trio via os.getenv / os.environ[...] / os.environ.get.
_FORBIDDEN_READ = re.compile(
    r"os\.(?:getenv|environ(?:\.get)?)\s*[\(\[]\s*['\"]"
    r"(?:OPENAI_API_KEY|OPENAI_BASE_URL|KNOWLEDGE_LLM_MODEL)['\"]"
)
# LLM_MODEL is checked with the quote anchored so LLM_REASONING_EFFORT and
# KNOWLEDGE_LLM_MODEL (handled above) do not false-positive.
_FORBIDDEN_LLM_MODEL = re.compile(
    r"os\.(?:getenv|environ(?:\.get)?)\s*[\(\[]\s*['\"]LLM_MODEL['\"]"
)

# Feature 089: no source file may read a TYPESAFE_ environment name. The
# adapter passes api_key/base_url/model to the SDK explicitly, precisely so the
# SDK's own environment fallback can never fire.
_FORBIDDEN_TYPESAFE_READ = re.compile(
    r"os\.(?:getenv|environ(?:\.get)?)\s*[\(\[]\s*['\"]TYPESAFE_[A-Z_]*['\"]"
)
# The names may still be *mentioned* -- the boot gate and the sandbox denylist
# have to name what they refuse. Those files declare them as data, not reads.
_TYPESAFE_DECLARATION_PATHS = {
    os.path.join("orchestrator", "session_store.py"),
    os.path.join("orchestrator", "sandbox.py"),
    os.path.join("verification", "config.py"),
}

_SKIP_DIR_NAMES = {"tests", "__pycache__", "tmp", "node_modules", ".venv"}
_SKIP_FILE_NAMES = {"sandbox.py", "redteam.py"}
_SKIP_REL_PATHS = {os.path.join("verification", "config.py")}


def _scan_files():
    for path in sorted(BACKEND_DIR.rglob("*.py")):
        rel = path.relative_to(BACKEND_DIR)
        if any(part in _SKIP_DIR_NAMES for part in rel.parts):
            continue
        if rel.name in _SKIP_FILE_NAMES:
            continue
        if str(rel) in _SKIP_REL_PATHS:
            continue
        yield path, rel


def test_no_live_operator_credential_reads_remain_in_source_tree():
    violations = []
    for path, rel in _scan_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover — unreadable file is not a pass
            violations.append(f"{rel}: unreadable")
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _FORBIDDEN_READ.search(line) or _FORBIDDEN_LLM_MODEL.search(line):
                violations.append(f"{rel}:{lineno}: {line.strip()}")
    assert violations == [], (
        "FR-001/FR-002: the operator-default env credential mechanism must be "
        "REMOVED, not left unset. Live reads found:\n" + "\n".join(violations)
    )


def test_guard_scans_a_meaningful_tree():
    """The guard must actually cover the orchestrator + llm_config sources
    (an over-aggressive skip list would make the sweep vacuous)."""
    scanned = {str(rel) for _, rel in _scan_files()}
    assert os.path.join("orchestrator", "orchestrator.py") in scanned
    assert os.path.join("llm_config", "client_factory.py") in scanned
    assert os.path.join("llm_config", "user_store.py") in scanned
    assert len(scanned) > 100, "suspiciously small scan set"


def test_voice_compose_does_not_restore_operator_llm_environment():
    """Feature 065 may rename speech inputs only at the worker boundary.

    The main service still loads ``.env`` for unrelated settings. Explicit
    empty overrides therefore remain a security boundary: the operator speech
    values cannot become a user or System LLM fallback inside AstralDeep.
    """

    for name in ("docker-compose.yml", "docker-compose.staging.yml"):
        document = (REPO_ROOT / name).read_text(encoding="utf-8")
        main_start = document.index("  astraldeep:\n")
        worker_start = document.index("  voice-worker:\n", main_start)
        main_service = document[main_start:worker_start]
        next_service = re.search(
            r"(?m)^  [A-Za-z0-9_-]+:\n",
            document[worker_start + len("  voice-worker:\n") :],
        )
        worker_end = (
            -1
            if next_service is None
            else worker_start + len("  voice-worker:\n") + next_service.start()
        )
        worker_service = document[
            worker_start : None if worker_end == -1 else worker_end
        ]

        assert 'OPENAI_BASE_URL: ""' in main_service
        assert 'OPENAI_API_KEY: ""' in main_service
        assert 'VOICE_SPEECH_BASE_URL: ""' in main_service
        assert 'VOICE_SPEECH_API_KEY: ""' in main_service

        assert "VOICE_SPEECH_BASE_URL: ${OPENAI_BASE_URL:?" in worker_service
        assert "VOICE_SPEECH_API_KEY: ${OPENAI_API_KEY:?" in worker_service
        assert 'OPENAI_BASE_URL: ""' in worker_service
        assert 'OPENAI_API_KEY: ""' in worker_service


# ---------------------------------------------------------------------------
# (c) Feature 089 FR-005 — TypeSafe configuration never comes from the env
# ---------------------------------------------------------------------------


def test_typesafe_env_vars_produce_zero_typesafe_requests(monkeypatch, orchestrator_factory):
    """Env set + no user key must mean no TypeSafe traffic at all.

    This is the behavioral half. Setting all three variables and running a turn
    for a user who has saved no key must not construct a client or issue a
    request, because the only thing that can authorize a routing call is a row
    in ``user_typesafe_credential``.
    """
    for name, value in {**LEGACY_VARS, **TYPESAFE_VARS}.items():
        monkeypatch.setenv(name, value)

    from unittest.mock import AsyncMock

    requests: list[object] = []

    orch = orchestrator_factory()
    orch._record_llm_unconfigured = AsyncMock()

    uid = f"tsenv089-{uuid.uuid4().hex[:10]}"
    ws = MagicMock()
    orch.ui_sessions[ws] = {"sub": uid, "preferred_username": f"{uid}@example"}

    routing = getattr(orch, "_typesafe_routing", None)
    if routing is not None:  # the adapter is wired (T023 onward)
        monkeypatch.setattr(
            routing,
            "start_routing",
            lambda *args, **kwargs: requests.append(("start", args, kwargs)),
            raising=False,
        )

    async def _scenario():
        assert await orch.llm_configured_for(uid) is False
        message, usage = await orch._call_llm(ws, [{"role": "user", "content": "hi"}])
        assert message is None and usage is None

    asyncio.run(_scenario())

    assert requests == [], "a TypeSafe request was issued from environment configuration"


def test_production_posture_refuses_typesafe_environment(monkeypatch):
    from orchestrator import session_store

    monkeypatch.setenv("ASTRAL_ENV", "production")
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", "x" * 44)
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "y" * 44)
    monkeypatch.setenv("AUDIT_HMAC_SECRET", "z" * 44)
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://idp.example/realms/a")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral")
    monkeypatch.setenv("KEYCLOAK_CLIENT_SECRET", "s" * 32)
    for name in session_store.TYPESAFE_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)

    # Baseline: this configuration boots.
    session_store.assert_production_posture()

    for name in session_store.TYPESAFE_ENV_NAMES:
        monkeypatch.setenv(name, "set-by-an-operator")
        with pytest.raises(SystemExit) as exit_info:
            session_store.assert_production_posture()
        assert exit_info.value.code == 78
        monkeypatch.delenv(name)


def test_development_posture_warns_instead_of_refusing(monkeypatch, caplog):
    import logging

    from orchestrator import session_store

    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.setenv("TYPESAFE_API_KEY", TYPESAFE_VARS["TYPESAFE_API_KEY"])

    with caplog.at_level(logging.WARNING):
        session_store.assert_production_posture()  # must not raise

    warnings = "\n".join(record.getMessage() for record in caplog.records)
    assert "TYPESAFE_API_KEY" in warnings
    # The warning names the variable, never its value.
    assert TYPESAFE_VARS["TYPESAFE_API_KEY"] not in warnings


def test_sandbox_child_never_inherits_typesafe_configuration():
    from orchestrator.sandbox import sandbox_env

    env = sandbox_env({**TYPESAFE_VARS, "AGENT_API_KEY": "kept"}, "/tmp/sandbox-089")

    assert not [name for name in env if name.startswith("TYPESAFE_")]
    assert env["AGENT_API_KEY"] == "kept"


def test_no_live_typesafe_environment_reads_remain_in_source_tree():
    violations = []
    for path, rel in _scan_files():
        if str(rel) in _TYPESAFE_DECLARATION_PATHS:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover - unreadable file is not a pass
            violations.append(f"{rel}: unreadable")
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _FORBIDDEN_TYPESAFE_READ.search(line):
                violations.append(f"{rel}:{lineno}: {line.strip()}")
    assert violations == [], (
        "089 FR-005: TypeSafe configuration must never be read from the "
        "environment. Live reads found:\n" + "\n".join(violations)
    )


def test_the_declaring_files_name_the_variables_without_reading_them():
    """The exemption list must stay an exemption, not a loophole."""
    from orchestrator import sandbox, session_store

    assert session_store.TYPESAFE_ENV_NAMES == (
        "TYPESAFE_API_KEY",
        "TYPESAFE_BASE_URL",
        "TYPESAFE_DEFAULT_MODEL",
    )
    assert set(session_store.TYPESAFE_ENV_NAMES) <= set(sandbox._SECRET_ENV_DENYLIST)
