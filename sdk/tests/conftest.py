"""Pytest fixtures wrapping sdk/tests/fake_server.py's FakeAstralServer for the SDK test
suite, plus the vendored MCP protocol schema used to validate bridge envelopes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import jsonschema
import pytest

from tests.fake_server import FakeAstralServer, FakeAstralState

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def fake_server():
    server = FakeAstralServer().start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def fake_server_factory():
    servers: list[FakeAstralServer] = []

    def _make(state: FakeAstralState | None = None) -> FakeAstralServer:
        server = FakeAstralServer(state).start()
        servers.append(server)
        return server

    yield _make
    for server in servers:
        server.stop()


@pytest.fixture(scope="session")
def mcp_protocol_schema() -> dict[str, Any]:
    return json.loads((_FIXTURES / "mcp" / "2026-07-28.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def validate_mcp(mcp_protocol_schema) -> Callable[[str, Any], None]:
    def _validate(definition: str, payload: Any) -> None:
        validator = jsonschema.Draft202012Validator({
            "$schema": mcp_protocol_schema["$schema"],
            "$ref": f"#/$defs/{definition}",
            "$defs": mcp_protocol_schema["$defs"],
        })
        errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.path))
        assert not errors, [error.message for error in errors]

    return _validate
