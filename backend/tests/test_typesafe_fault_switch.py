"""The quickstart's fault switch has to be installed, not just defined.

`ASTRAL_TEST_TYPESAFE_FAULT` (T013) existed as a wrapper class in
`tests/fakes/typesafe_fake.py` and a posture check in `configured_fault`, but
nothing ever wrapped the real adapter client with it, so setting the variable on
a running stack did nothing at all and quickstart section 4 could not be walked.

That is the same shape as the two defects in verification.md 7e: a handler with
no path reaching it. These tests pin the path, not the handler.
"""
from __future__ import annotations

import pytest

from orchestrator.typesafe_routing import client as client_module


@pytest.fixture(autouse=True)
def _clean_process_client(monkeypatch):
    """Never leave a wrapped client installed for another test."""
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
    """A fault injector a production process respects is a DoS control."""
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
    """The whole point: a routing call fails instead of reaching the network."""
    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.setenv("ASTRAL_TEST_TYPESAFE_FAULT", "auth")

    from tests.fakes.typesafe_fake import ERROR_CLASSES

    installed = client_module.adapter_client()
    with pytest.raises(type(ERROR_CLASSES["auth"]())):
        await installed.system_one(questions=[], context={})
