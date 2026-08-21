"""Startup boundary tests for preserved pre-Plane tool-permission state."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from orchestrator.tool_permissions import ToolPermissionManager


class _Runtime:
    def __init__(self) -> None:
        self.repositories = SimpleNamespace(
            tool_policy_state=object(),
            agents=object(),
        )

    @contextmanager
    def transaction(self):
        yield object()


def test_nonempty_legacy_permission_file_is_preserved_and_rejected(tmp_path) -> None:
    legacy = tmp_path / "tool_permissions.json"
    original = b'{"legacy": true}\n'
    legacy.write_bytes(original)

    with pytest.raises(RuntimeError, match="AstralPlane recovery"):
        ToolPermissionManager(
            data_dir=str(tmp_path),
            plane_runtime=_Runtime(),
        )

    assert legacy.read_bytes() == original
    assert not (tmp_path / "tool_permissions.json.bak").exists()


def test_absent_or_empty_legacy_file_does_not_create_or_rename_state(tmp_path) -> None:
    runtime = _Runtime()
    manager = ToolPermissionManager(
        data_dir=str(tmp_path),
        plane_runtime=runtime,
    )
    assert manager._policy.plane_runtime is runtime  # noqa: SLF001
    assert list(tmp_path.iterdir()) == []

    legacy = tmp_path / "tool_permissions.json"
    legacy.write_bytes(b"")
    manager = ToolPermissionManager(
        data_dir=str(tmp_path),
        plane_runtime=runtime,
    )
    assert manager._policy.plane_runtime is runtime  # noqa: SLF001
    assert legacy.read_bytes() == b""
