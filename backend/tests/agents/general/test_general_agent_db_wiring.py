"""General Agent reuses one explicitly injected AstralPlane runtime."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def test_general_agent_init_wires_file_tools_to_injected_plane(monkeypatch, tmp_path):
    from agents.general import file_tools
    from agents.general.general_agent import GeneralAgent
    import shared.attachment_resolver as resolver

    monkeypatch.setenv("AGENT_KEY_PATH", str(tmp_path / "general-1.pem"))
    monkeypatch.setattr(file_tools, "_PRODUCTION_DEPENDENCIES", None)
    monkeypatch.setattr(resolver, "_PLANE_RUNTIME", None)
    monkeypatch.setattr(resolver, "_PLANE_REPOSITORIES", None)
    monkeypatch.setattr(resolver, "_PLANE_BLOBS", None)
    repositories = SimpleNamespace()
    runtime = SimpleNamespace(repositories=repositories)
    blobs = object()

    GeneralAgent(port=18091, plane_runtime=runtime, plane_blobs=blobs)

    assert file_tools._get_plane_dependencies() == (runtime, repositories, blobs)
    assert resolver._PLANE_RUNTIME is runtime
    assert resolver._PLANE_REPOSITORIES is repositories
    assert resolver._PLANE_BLOBS is blobs


def test_general_agent_refuses_missing_plane_dependencies(monkeypatch, tmp_path):
    from agents.general.general_agent import GeneralAgent

    monkeypatch.setenv("AGENT_KEY_PATH", str(tmp_path / "general-1.pem"))
    with pytest.raises(RuntimeError, match="initialized AstralPlane"):
        GeneralAgent(port=18091, plane_runtime=None)
