"""Every framework adapter must be importable, and usable for pure schema
generation, WITHOUT its target framework installed. Only the function that
actually needs the third-party package raises ``ImportError`` (with an
actionable ``pip install astral-sdk[...]`` hint) — and only when that package
genuinely is not installed in this environment.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys

import pytest


def _not_installed(name: str) -> bool:
    return importlib.util.find_spec(name) is None


@pytest.mark.parametrize("module_name", [
    "astral_sdk.integrations.generic",
    "astral_sdk.integrations.openai_agents",
    "astral_sdk.integrations.anthropic",
    "astral_sdk.integrations.langchain",
    "astral_sdk.mcp_bridge",
])
def test_module_imports_without_the_optional_framework(module_name):
    # A prior test in this process may have already imported (and cached) the
    # module; re-import is still cheap and still proves no import-time error.
    sys.modules.pop(module_name, None)
    importlib.import_module(module_name)


def test_generic_schemas_need_nothing_extra():
    from astral_sdk.integrations import generic

    assert len(generic.FUNCTION_SCHEMAS) == 7
    names = {schema["name"] for schema in generic.FUNCTION_SCHEMAS}
    assert "astral_submit_operation" in names
    assert "astral_get_artifact" in names


def test_anthropic_schema_shaping_needs_no_anthropic_package():
    from astral_sdk.integrations import anthropic

    tools = anthropic.as_anthropic_tools()
    assert all("input_schema" in tool for tool in tools)
    assert all("parameters" not in tool for tool in tools)


def test_openai_schema_shaping_needs_no_openai_package():
    from astral_sdk.integrations import openai_agents

    tools = openai_agents.as_openai_tools()
    assert all(tool["type"] == "function" for tool in tools)
    assert all("parameters" in tool["function"] for tool in tools)


@pytest.mark.skipif(not _not_installed("openai"), reason="openai IS installed in this environment")
def test_require_openai_raises_actionable_import_error():
    from astral_sdk.integrations import openai_agents

    with pytest.raises(ImportError, match=r"pip install astral-sdk\[openai\]"):
        openai_agents.require_openai()


@pytest.mark.skipif(not _not_installed("anthropic"), reason="anthropic IS installed in this environment")
def test_require_anthropic_raises_actionable_import_error():
    from astral_sdk.integrations import anthropic

    with pytest.raises(ImportError, match=r"pip install astral-sdk\[anthropic\]"):
        anthropic.require_anthropic()


@pytest.mark.skipif(not _not_installed("langchain_core"), reason="langchain_core IS installed in this environment")
def test_langchain_adapter_raises_actionable_import_error():
    from astral_sdk.integrations import langchain

    with pytest.raises(ImportError, match=r"pip install astral-sdk\[langchain\]"):
        langchain.as_langchain_tools(client=object())


@pytest.mark.skipif(not _not_installed("mcp"), reason="mcp IS installed in this environment")
def test_bridge_serve_with_official_sdk_raises_actionable_import_error():
    from astral_sdk.client import AstralClient
    from astral_sdk.mcp_bridge import Bridge

    bridge = Bridge(AstralClient.__new__(AstralClient))
    with pytest.raises(ImportError, match=r"pip install astral-sdk\[mcp\]"):
        bridge.serve_with_official_sdk()


@pytest.mark.skipif(_not_installed("openai"), reason="openai is not installed in this environment")
def test_require_openai_succeeds_when_the_package_is_present():
    from astral_sdk.integrations import openai_agents

    assert openai_agents.require_openai() is not None


def test_top_level_package_import_never_touches_any_optional_framework():
    for optional in ("openai", "anthropic", "langchain_core", "mcp", "crewai", "autogen_agentchat"):
        sys.modules.pop(optional, None)
    sys.modules.pop("astral_sdk", None)
    importlib.import_module("astral_sdk")
    for optional in ("openai", "anthropic", "langchain_core", "mcp", "crewai", "autogen_agentchat"):
        assert optional not in sys.modules
