"""Tests for backend/agents/computer_use/mcp_tools.py: the extension registration hook, the
macOS tool descriptions, post-effect screenshots, the host wait verb and the three command tools.
"""

from __future__ import annotations

import sys
import types

import pytest

from agents.computer_use import mcp_tools
from orchestrator import computer_use_policy as policy

_EXTENSION = "agents.computer_use.browser_tools"


def _extension(register, **attrs):
    module = types.ModuleType(_EXTENSION)
    module.register = register
    for name, value in attrs.items():
        setattr(module, name, value)
    return module


def _browser_entry(name):
    return {"function": lambda **_: {}, "description": name, "input_schema": {"type": "object"},
            "scope": policy.SCOPES[name], "tier": policy.TIERS[name], "retryable": False,
            "timeout": policy.TIMEOUTS[name]}


def test_hook_is_a_no_op_without_the_extension(monkeypatch):
    monkeypatch.setitem(sys.modules, _EXTENSION, None)
    registry = {"screenshot": {"description": "kept"}}
    mcp_tools.register_extension_tools(registry)
    assert registry == {"screenshot": {"description": "kept"}}


def test_hook_adds_new_browser_verbs_from_the_extension(monkeypatch):
    def register(registry):
        registry["browser_snapshot"] = _browser_entry("browser_snapshot")

    monkeypatch.setitem(sys.modules, _EXTENSION, _extension(register))
    registry = {"screenshot": {"description": "kept"}}
    mcp_tools.register_extension_tools(registry)
    assert set(registry) == {"screenshot", "browser_snapshot"}
    assert registry["screenshot"] == {"description": "kept"}


@pytest.mark.parametrize("name", ["run_shell", "screenshot", "run_command"])
def test_hook_refuses_unknown_or_overriding_entries_and_keeps_the_registry(monkeypatch, name):
    def register(registry):
        registry["browser_snapshot"] = _browser_entry("browser_snapshot")
        registry[name] = {"description": "forged"}

    monkeypatch.setitem(sys.modules, _EXTENSION, _extension(register))
    registry = {"screenshot": {"description": "kept"}}
    with pytest.raises(RuntimeError):
        mcp_tools.register_extension_tools(registry)
    assert registry == {"screenshot": {"description": "kept"}}


def test_hook_refuses_an_entry_whose_policy_declaration_drifted(monkeypatch):
    def register(registry):
        registry["browser_click"] = {**_browser_entry("browser_click"), "scope": "tools:read"}

    monkeypatch.setitem(sys.modules, _EXTENSION, _extension(register))
    registry = {}
    with pytest.raises(RuntimeError):
        mcp_tools.register_extension_tools(registry)
    assert registry == {}


def test_hook_propagates_a_broken_extension_import(monkeypatch):
    broken = types.ModuleType(_EXTENSION)
    monkeypatch.setitem(sys.modules, _EXTENSION, broken)
    with pytest.raises(AttributeError):
        mcp_tools.register_extension_tools({})
