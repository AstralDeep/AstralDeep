"""Feature-066 (FR-036) pins for the bounded speech-preflight re-check.

Before this, a speech service that was briefly missing its models or routes
killed the worker at startup: the preflight raised, nothing caught it, and
the process exited 78 — silently, because the package logs nothing before
admission. Under ``restart: "no"`` (staging) the worker then stayed dead
until an operator noticed. That is the exact failure FR-036 removes.
"""

from __future__ import annotations

import asyncio

import pytest

from voice_agent import main as main_module
from voice_agent.speech_adapters import SpeechPreflightError


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the bounded backoff, remove the wall-clock wait."""
    monkeypatch.setattr(main_module, "_PREFLIGHT_RETRY_INITIAL_SECONDS", 0.001)
    monkeypatch.setattr(main_module, "_PREFLIGHT_RETRY_MAX_SECONDS", 0.002)


@pytest.mark.asyncio
async def test_recovers_without_a_restart(monkeypatch, capsys) -> None:
    """A transient speech outage heals in-process instead of exiting."""
    attempts = {"n": 0}

    async def flaky(config):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise SpeechPreflightError("asr_unavailable")
        return None

    monkeypatch.setattr(main_module, "run_speech_preflight", flaky)
    stop = asyncio.Event()

    assert await main_module.preflight_until_ready(object(), stop) is True
    assert attempts["n"] == 3

    # Every attempt says WHY, so an operator can see it in `docker logs`.
    err = capsys.readouterr().err
    assert "voice_worker_preflight:asr_unavailable attempt=1" in err
    assert "voice_worker_preflight:asr_unavailable attempt=2" in err
    assert "voice_worker_preflight:ok attempt=3" in err


@pytest.mark.asyncio
async def test_ready_on_the_first_attempt_logs_once(monkeypatch, capsys) -> None:
    async def ready(config):
        return None

    monkeypatch.setattr(main_module, "run_speech_preflight", ready)

    assert await main_module.preflight_until_ready(object(), asyncio.Event()) is True
    err = capsys.readouterr().err
    assert err.count("voice_worker_preflight:") == 1
    assert "voice_worker_preflight:ok attempt=1" in err


@pytest.mark.asyncio
async def test_a_credential_fault_still_fails_fast(monkeypatch) -> None:
    """Waiting cannot heal a misconfigured credential — keep failing closed."""
    calls = {"n": 0}

    async def bad_credential(config):
        calls["n"] += 1
        raise SpeechPreflightError("missing_credential")

    monkeypatch.setattr(main_module, "run_speech_preflight", bad_credential)

    with pytest.raises(SpeechPreflightError):
        await main_module.preflight_until_ready(object(), asyncio.Event())
    assert calls["n"] == 1  # never retried


@pytest.mark.asyncio
async def test_shutdown_stops_the_retry_loop(monkeypatch) -> None:
    """A stop request wins over an unavailable speech service."""

    async def never_ready(config):
        raise SpeechPreflightError("tts_unavailable")

    monkeypatch.setattr(main_module, "run_speech_preflight", never_ready)
    stop = asyncio.Event()

    async def stop_soon() -> None:
        await asyncio.sleep(0.01)
        stop.set()

    task = asyncio.create_task(stop_soon())
    assert await main_module.preflight_until_ready(object(), stop) is False
    await task


@pytest.mark.asyncio
async def test_an_already_stopped_worker_never_probes(monkeypatch) -> None:
    calls = {"n": 0}

    async def counted(config):
        calls["n"] += 1

    monkeypatch.setattr(main_module, "run_speech_preflight", counted)
    stop = asyncio.Event()
    stop.set()

    assert await main_module.preflight_until_ready(object(), stop) is False
    assert calls["n"] == 0
