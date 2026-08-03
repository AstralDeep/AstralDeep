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

References: specs/054-byo-llm-setup/spec.md FR-001/FR-002, SC-004, SC-007.
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


# ---------------------------------------------------------------------------
# (a) SC-007 — env vars change nothing
# ---------------------------------------------------------------------------

def test_legacy_env_vars_do_not_configure_any_llm(monkeypatch):
    for name, value in LEGACY_VARS.items():
        monkeypatch.setenv(name, value)

    # Fresh construction WITH the vars set — boot must succeed (FR-003) and
    # must not mint any default credential from them.
    from unittest.mock import AsyncMock

    from orchestrator.orchestrator import Orchestrator
    orch = Orchestrator()
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
        assert "VOICE_SPEECH_BASE_URL" not in main_service
        assert "VOICE_SPEECH_API_KEY" not in main_service

        assert "VOICE_SPEECH_BASE_URL: ${OPENAI_BASE_URL:?" in worker_service
        assert "VOICE_SPEECH_API_KEY: ${OPENAI_API_KEY:?" in worker_service
        assert 'OPENAI_BASE_URL: ""' in worker_service
        assert 'OPENAI_API_KEY: ""' in worker_service
