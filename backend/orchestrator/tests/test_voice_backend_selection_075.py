"""Immutable deployment speech-backend selection for Feature 075."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator.voice_backend import (
    SpeechBackendSelection,
    VoiceSpeechBackend,
    backend_value,
)
from orchestrator.voice_bootstrap import build_voice_services


class _PlaneRuntime:
    repositories = SimpleNamespace(voice=object())

    def transaction(self):  # pragma: no cover - construction must not transact.
        raise AssertionError("unexpected database access")


def test_missing_selector_preserves_exact_legacy_default() -> None:
    selection = SpeechBackendSelection.from_environ({})

    assert selection.value is VoiceSpeechBackend.LLM_FACTORY
    assert selection.valid is True
    assert selection.source == "legacy_default"


@pytest.mark.parametrize("value", ["llm_factory", "client_local"])
def test_only_exact_selector_values_are_accepted(value: str) -> None:
    selection = SpeechBackendSelection.from_environ(
        {"VOICE_SPEECH_BACKEND": value}
    )

    assert selection.value.value == value
    assert selection.valid is True
    assert selection.source == "explicit"


@pytest.mark.parametrize(
    "value",
    ["", " ", "LLM_FACTORY", "client-local", "remote", "client_local\n"],
)
def test_blank_unknown_or_malformed_selector_fails_voice_closed(value: str) -> None:
    selection = SpeechBackendSelection.from_environ(
        {"VOICE_SPEECH_BACKEND": value}
    )

    assert selection.value is None
    assert selection.valid is False
    assert selection.source == "explicit"
    assert repr(selection) == (
        "SpeechBackendSelection(value=None, valid=False, source='explicit')"
    )


def test_selection_is_frozen_and_independent_of_later_environment_mutation() -> None:
    environ = {"VOICE_SPEECH_BACKEND": "client_local"}
    selection = SpeechBackendSelection.from_environ(environ)
    environ["VOICE_SPEECH_BACKEND"] = "llm_factory"

    assert selection.value is VoiceSpeechBackend.CLIENT_LOCAL
    with pytest.raises(AttributeError):
        selection.value = VoiceSpeechBackend.LLM_FACTORY  # type: ignore[misc]


def test_selector_value_object_rejects_impossible_states_and_aliases() -> None:
    with pytest.raises(ValueError, match="invalid_speech_backend_source"):
        SpeechBackendSelection(
            value=VoiceSpeechBackend.LLM_FACTORY,
            valid=True,
            source="request",
        )
    with pytest.raises(ValueError, match="invalid_speech_backend_selection"):
        SpeechBackendSelection(value=None, valid=True, source="explicit")
    with pytest.raises(ValueError, match="invalid_speech_backend_default"):
        SpeechBackendSelection(
            value=VoiceSpeechBackend.CLIENT_LOCAL,
            valid=True,
            source="legacy_default",
        )
    assert backend_value(VoiceSpeechBackend.CLIENT_LOCAL) is (
        VoiceSpeechBackend.CLIENT_LOCAL
    )
    assert backend_value("client-local") is None


def test_client_local_bootstrap_constructs_no_remote_media_or_worker(monkeypatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("client_local constructed a remote dependency")

    monkeypatch.setattr("orchestrator.voice_bootstrap.LiveKitService", forbidden)
    monkeypatch.setattr("orchestrator.voice_bootstrap.WorkerPool", forbidden)
    monkeypatch.setattr("orchestrator.voice_bootstrap.DirectRtcVoiceMedia", forbidden)

    services = build_voice_services(
        plane_runtime=_PlaneRuntime(),
        plane_repositories=_PlaneRuntime.repositories,
        environ={
            "ASTRAL_ENV": "test",
            "VOICE_SPEECH_BACKEND": "client_local",
        },
    )

    assert services.speech_backend is VoiceSpeechBackend.CLIENT_LOCAL
    assert services.livekit is None
    assert services.worker_pool is None
    assert services.worker_endpoint is None


def test_invalid_selection_returns_unavailable_services_without_remote_construction(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "orchestrator.voice_bootstrap.LiveKitService",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid selection constructed remote media")
        ),
    )

    services = build_voice_services(
        plane_runtime=_PlaneRuntime(),
        plane_repositories=_PlaneRuntime.repositories,
        environ={"ASTRAL_ENV": "test", "VOICE_SPEECH_BACKEND": ""},
    )

    assert services.speech_backend is None
    assert services.runtime is None
    assert services.voice_status()["reason"] == "backend_selection_invalid"
