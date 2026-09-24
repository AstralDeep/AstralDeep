"""Tests for the ASTRAL_TEST_TYPESAFE_FAULT switch in
orchestrator/typesafe_routing/client.py: it wraps the adapter client with a fault
injector only in development posture, leaving production unaffected.
"""

from __future__ import annotations

import pytest

from orchestrator.typesafe_routing import client as client_module


@pytest.fixture(autouse=True)
def _clean_process_client(monkeypatch):
    monkeypatch.setattr(client_module, "_adapter_client", None, raising=False)
    yield
    client_module._adapter_client = None


def _is_wrapped(obj) -> bool:
    return type(obj).__name__ == "FaultInjectingClient"


def test_no_switch_gives_the_plain_client(monkeypatch):
    monkeypatch.delenv("ASTRAL_TEST_TYPESAFE_FAULT", raising=False)
    monkeypatch.setenv("ASTRAL_ENV", "development")

    assert not _is_wrapped(client_module.adapter_client())


def test_the_switch_installs_the_injector_in_development(monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.setenv("ASTRAL_TEST_TYPESAFE_FAULT", "timeout")

    assert _is_wrapped(client_module.adapter_client())


def test_production_posture_ignores_the_switch(monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "production")
    monkeypatch.setenv("ASTRAL_TEST_TYPESAFE_FAULT", "timeout")

    assert not _is_wrapped(client_module.adapter_client())


def test_an_unset_posture_is_production_and_ignores_the_switch(monkeypatch):
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    monkeypatch.setenv("ASTRAL_TEST_TYPESAFE_FAULT", "timeout")

    assert not _is_wrapped(client_module.adapter_client())


def test_an_unknown_fault_name_is_not_installed(monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.setenv("ASTRAL_TEST_TYPESAFE_FAULT", "not-a-fault")

    assert not _is_wrapped(client_module.adapter_client())


@pytest.mark.asyncio
async def test_the_installed_injector_actually_raises(monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.setenv("ASTRAL_TEST_TYPESAFE_FAULT", "auth")

    from tests.fakes.typesafe_fake import ERROR_CLASSES

    installed = client_module.adapter_client()
    with pytest.raises(type(ERROR_CLASSES["auth"]())):
        await installed.system_one(questions=[], context={})
