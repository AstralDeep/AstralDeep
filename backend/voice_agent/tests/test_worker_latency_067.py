"""Worker startup and event-pump latency guards for the 067 remediation."""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import voice_agent.main as main_module
import voice_agent.session as session_module
from voice_agent.config import WorkerConfig
from voice_agent.session import (
    RtcSessionError,
    SileroVad,
    preload_vad_model,
)
from voice_agent.speech_adapters import (
    SERVER_OWNED_PHRASE_TEXTS,
    FixedPhraseTTSCache,
)
from voice_agent.tests.test_session_start_065 import (
    FakeRoom,
    FakeRtcFactory,
    _session,
)
from voice_agent.tests.test_tts_phrase_cache_066 import (
    CountingInner,
    FailingInner,
)
from voice_agent.tests.test_worker_runtime_integration_065 import (
    _binding,
    _config,
)


def _fake_onnxruntime(
    monkeypatch: pytest.MonkeyPatch, inference_session: type
) -> None:
    module = ModuleType("onnxruntime")
    module.InferenceSession = inference_session
    monkeypatch.setitem(sys.modules, "onnxruntime", module)
    monkeypatch.setattr(session_module, "_VAD_INFERENCE_SESSIONS", {})


# ---------------------------------------------------------------- V9: TTS warm


def test_startup_warm_covers_only_server_owned_phrases() -> None:
    assert set(main_module.WARM_PHRASE_TEXTS) <= SERVER_OWNED_PHRASE_TEXTS
    assert "On it!" in main_module.WARM_PHRASE_TEXTS


@pytest.mark.asyncio
async def test_startup_warm_makes_the_first_acknowledgement_a_cache_hit() -> None:
    inner = CountingInner()
    tts = FixedPhraseTTSCache(inner)

    await main_module.warm_phrase_cache(tts)
    assert inner.calls == list(main_module.WARM_PHRASE_TEXTS)

    await tts.synthesize("On it!", max_duration_samples=96_000)

    # The first real acknowledgement pays no synthesis round trip.
    assert inner.calls == list(main_module.WARM_PHRASE_TEXTS)


@pytest.mark.asyncio
async def test_startup_warm_failure_is_swallowed_and_caches_nothing() -> None:
    inner = FailingInner()
    tts = FixedPhraseTTSCache(inner)

    await main_module.warm_phrase_cache(tts)

    assert inner.calls == 1
    assert len(tts._entries) == 0


def test_production_builder_exposes_the_cache_it_gave_the_sessions() -> None:
    client = main_module.build_pool_client(
        _config(), transport=object(), rtc_factory=object(), vad_factory=object
    )
    runtime = client.supervisor._session_factory(_binding())

    assert isinstance(client.speech_tts, FixedPhraseTTSCache)
    assert client.speech_tts is runtime._tts


@pytest.mark.asyncio
async def test_run_worker_warms_concurrently_with_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    started: list[str] = []
    warmed: list[str] = []
    release = asyncio.Event()

    class BlockingTts:
        async def synthesize(self, text: str, *, max_duration_samples: int) -> Any:
            del max_duration_samples
            started.append(text)
            await release.wait()
            warmed.append(text)

    class Client:
        supervisor = object()
        speech_tts = BlockingTts()

        async def run_forever(self, stop: asyncio.Event) -> None:
            assert isinstance(stop, asyncio.Event)
            events.append("run")
            await asyncio.sleep(0)

    class Bridge:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs

        async def start(self) -> None:
            events.append("bridge-start")

        async def close(self) -> None:
            events.append("bridge-close")

    class Guard:
        def assert_clean(self, names: set[str]) -> None:
            del names

        def install(self) -> None:
            return None

    async def preflight(_config_argument: WorkerConfig) -> None:
        events.append("preflight")

    monkeypatch.setattr(main_module, "RuntimeImportGuard", Guard)
    monkeypatch.setattr(main_module, "assert_runtime_distributions", lambda: None)
    monkeypatch.setattr(main_module, "run_speech_preflight", preflight)
    monkeypatch.setattr(main_module, "build_pool_client", lambda config: Client())
    monkeypatch.setattr(main_module, "WatchBridgeServer", Bridge)

    await main_module.run_worker(_config())

    assert events == ["preflight", "bridge-start", "run", "bridge-close"]
    # The warm was in flight while the worker registered, and a warm that never
    # finishes is cancelled at shutdown instead of holding the process open.
    assert started == ["On it!"]
    assert warmed == []


# ------------------------------------------------------------ V3: shared graph


def test_vad_graph_is_built_once_per_model_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("numpy")
    builds: list[str] = []

    class FakeInferenceSession:
        def __init__(self, path: str, providers: list[str]) -> None:
            del providers
            builds.append(path)

    _fake_onnxruntime(monkeypatch, FakeInferenceSession)
    model = tmp_path / "silero.onnx"
    model.write_bytes(b"stub")

    first = SileroVad(model_path=model)
    second = SileroVad(model_path=model)

    assert builds == [str(model)]
    assert first._session is second._session
    # Recurrent state stays per instance so a shared graph cannot leak a turn.
    assert first._state is not second._state


def test_a_failed_graph_build_is_never_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("numpy")
    attempts: list[str] = []

    class FailingInferenceSession:
        def __init__(self, path: str, providers: list[str]) -> None:
            del providers
            attempts.append(path)
            raise RuntimeError("native detail")

    _fake_onnxruntime(monkeypatch, FailingInferenceSession)
    invalid = tmp_path / "invalid.onnx"
    invalid.write_bytes(b"not an onnx model")

    for _ in range(2):
        with pytest.raises(RtcSessionError, match="vad_model_invalid"):
            SileroVad(model_path=invalid)
    with pytest.raises(RtcSessionError, match="vad_model_unavailable"):
        SileroVad(model_path=tmp_path / "missing.onnx")

    assert attempts == [str(invalid), str(invalid)]
    assert session_module._VAD_INFERENCE_SESSIONS == {}


@pytest.mark.asyncio
async def test_preload_builds_the_graph_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("numpy")
    build_threads: list[int] = []

    class FakeInferenceSession:
        def __init__(self, path: str, providers: list[str]) -> None:
            del path, providers
            build_threads.append(threading.get_ident())

    _fake_onnxruntime(monkeypatch, FakeInferenceSession)
    model = tmp_path / "silero.onnx"
    model.write_bytes(b"stub")

    await preload_vad_model(model)

    assert len(build_threads) == 1
    assert build_threads[0] != threading.get_ident()
    # The first activation then constructs without building anything.
    vad = SileroVad(model_path=model)
    assert len(build_threads) == 1
    assert vad._session is session_module._VAD_INFERENCE_SESSIONS[str(model.resolve())]


@pytest.mark.asyncio
async def test_preload_swallows_a_missing_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class UnusedInferenceSession:
        def __init__(self, path: str, providers: list[str]) -> None:
            raise AssertionError("a missing asset must not reach the runtime")

    _fake_onnxruntime(monkeypatch, UnusedInferenceSession)

    await preload_vad_model(tmp_path / "missing.onnx")

    assert session_module._VAD_INFERENCE_SESSIONS == {}


# --------------------------------------------------------------- V5: event pump


@pytest.mark.asyncio
async def test_event_pump_reuses_its_waiters_across_events() -> None:
    session = _session(FakeRtcFactory(FakeRoom()))
    session._queue.put_nowait({"type": "first"})

    source, value = await session._next_owned_event()
    assert (source, value["type"]) == ("control", "first")
    rtc_waiter = session._rtc_waiter
    overrun_waiter = session._overrun_waiter
    closed_waiter = session._closed_waiter
    assert rtc_waiter is not None and not rtc_waiter.done()

    session._queue.put_nowait({"type": "second"})
    source, value = await session._next_owned_event()
    assert (source, value["type"]) == ("control", "second")

    # The three losing waiters survived instead of being cancelled and rebuilt.
    assert session._rtc_waiter is rtc_waiter
    assert session._overrun_waiter is overrun_waiter
    assert session._closed_waiter is closed_waiter
    assert not rtc_waiter.cancelled()

    await session.close("test")


@pytest.mark.asyncio
async def test_closed_wins_over_a_control_frame_ready_in_the_same_wait() -> None:
    session = _session(FakeRtcFactory(FakeRoom()))
    racing = {"type": "opaque-control", "payload": ["sensitive"]}
    session._queue.put_nowait(racing)
    session._closed.set()

    source, value = await session._next_owned_event()

    assert (source, value) == ("closed", None)
    assert racing == {}
    assert session._control_waiter is None
    await session.close("test")


@pytest.mark.asyncio
async def test_teardown_discards_the_event_parked_in_a_waiter() -> None:
    session = _session(FakeRtcFactory(FakeRoom()))
    retained = {"pcm": ["sensitive"]}
    session._queue.put_nowait({"type": "opaque-control"})
    session._callback("participant_connected")(retained)

    source, _value = await session._next_owned_event()
    assert source == "control"
    parked = session._rtc_waiter
    assert parked is not None and parked.done()

    await session.close("test")

    # A finished waiter holds its value outside the queue the teardown drains.
    assert session._rtc_waiter is None
    assert retained == {}
    assert parked.result().args == ()
    assert session._control_waiter is None
    assert session._overrun_waiter is None
    assert session._closed_waiter is None
