"""Executable entrypoint for the isolated Feature 065 direct-RTC worker."""

from __future__ import annotations

import asyncio
import importlib.abc
import importlib.metadata
import signal
import sys
from collections.abc import Mapping, Set
from types import ModuleType
from typing import Any

from .config import ConfigError, WorkerConfig
from .control import ChallengeError, PoolClient, ProtocolViolation
from .session import (
    DirectRtcSession,
    LiveKitRtcFactory,
    SessionBinding,
    SessionSupervisor,
    SileroVad,
    preload_vad_model,
)
from .speech_adapters import (
    KOKORO_SAMPLE_RATE,
    SERVER_OWNED_PHRASE_TEXTS,
    FixedPhraseTTSCache,
    SpeechPreflight,
    SpeechPreflightError,
    SpeechPreflightResult,
    SpeachesBatchSTT,
    SpeachesTTS,
)
from .watch_bridge import WatchBridgeServer, WatchPcmSession


_FORBIDDEN_MODULE_PREFIXES = frozenset(
    {
        "agents",
        "anthropic",
        "asyncpg",
        "langchain",
        "litellm",
        "livekit.agents",
        "livekit.api",
        "livekit.plugins",
        "llama_index",
        "mcp",
        "openai",
        "orchestrator",
        "psycopg",
        "psycopg2",
        "shared.database",
        "shared.external_http",
        "sqlalchemy",
    }
)
_FORBIDDEN_DISTRIBUTIONS = frozenset(
    {
        "anthropic",
        "asyncpg",
        "langchain",
        "litellm",
        "livekit-agents",
        "livekit-api",
        "llama-index",
        "mcp",
        "openai",
        "psycopg",
        "psycopg-binary",
        "sqlalchemy",
    }
)
_REQUIRED_EXACT_DISTRIBUTIONS = {
    "livekit": "1.1.14",
    "websockets": "17.0.1",
}


def build_pool_client(
    config: WorkerConfig,
    *,
    transport: Any | None = None,
    rtc_factory: Any | None = None,
    vad_factory: Any | None = None,
) -> PoolClient:
    """Construct the production direct-RTC sessions from worker-only config.

    The fixed-origin transport import is delayed because the isolated image
    copies its audited source into ``voice_agent.streaming_egress``. Tests may
    inject the transport, RTC adapter, and VAD constructor without loading
    native packages on the host.
    """

    if transport is None:
        transport = _build_speech_transport(config)
    resolved_rtc = rtc_factory or LiveKitRtcFactory()
    resolved_vad_factory = vad_factory or SileroVad
    asr = SpeachesBatchSTT(transport=transport, api_key=config.speech_api_key)
    # Feature 066: repeated server-owned announcements are served from bounded
    # worker memory instead of a fresh TTS round trip; user-content text never
    # matches the closed vocabulary and passes straight through.
    tts = FixedPhraseTTSCache(
        SpeachesTTS(transport=transport, api_key=config.speech_api_key)
    )
    client_holder: dict[str, PoolClient] = {}

    def create_session(binding: SessionBinding) -> DirectRtcSession:
        if binding.transport not in {"livekit", "watch_pcm_websocket"}:
            raise ProtocolViolation("unsupported_transport")
        client = client_holder.get("client")
        if client is None:
            raise RuntimeError("voice_pool_not_initialized")
        runtime_type = (
            WatchPcmSession
            if binding.transport == "watch_pcm_websocket"
            else DirectRtcSession
        )
        return runtime_type(
            binding,
            rtc_factory=resolved_rtc,
            vad=resolved_vad_factory(),
            asr=asr,
            tts=tts,
            worker_control_secret=config.control_secret,
            notice_sink=lambda notice: client.enqueue_session_notice(
                binding, notice
            ),
        )

    supervisor = SessionSupervisor(
        max_sessions=config.max_sessions,
        session_factory=create_session,
    )
    client = PoolClient(config, supervisor=supervisor)
    # The runtime cache is reachable from the client so startup can warm it
    # without rebuilding the adapters the sessions actually share.
    client.speech_tts = tts
    client_holder["client"] = client
    return client


async def run_speech_preflight(
    config: WorkerConfig,
    *,
    transport: Any | None = None,
) -> SpeechPreflightResult:
    """Prove the exact live speech profile before pool authentication."""

    resolved_transport = transport or _build_speech_transport(config)
    return await SpeechPreflight(
        transport=resolved_transport,
        api_key=config.speech_api_key,
    ).run()


# Feature 066 (FR-036). A speech service that is briefly missing its models
# or routes used to kill the worker at startup: the preflight raised, nothing
# caught it, and the process exited 78 — silently, because this package logs
# nothing before admission. Under `restart: "no"` (staging) the worker then
# stayed dead until an operator noticed, which is the exact failure this
# requirement removes. Re-check on bounded backoff instead, and say why on
# every attempt (reason codes only — a closed, content-free vocabulary).
_PREFLIGHT_RETRY_INITIAL_SECONDS = 5.0
_PREFLIGHT_RETRY_MAX_SECONDS = 60.0
#: A misconfigured credential cannot heal by waiting — fail fast as before.
_PREFLIGHT_FATAL_REASONS = frozenset({"missing_credential"})


def _log_preflight(reason: str, attempt: int) -> None:
    print(f"voice_worker_preflight:{reason} attempt={attempt}", file=sys.stderr)


async def preflight_until_ready(config: WorkerConfig, stop: asyncio.Event) -> bool:
    """Re-check the speech profile until it is ready or shutdown is requested.

    Returns True once the profile is proven, False if stopped first. Calls
    ``run_speech_preflight`` with exactly its production signature so the
    single-call-site contract (and every test double of it) is unchanged.
    """

    backoff = _PREFLIGHT_RETRY_INITIAL_SECONDS
    attempt = 0
    while not stop.is_set():
        attempt += 1
        try:
            await run_speech_preflight(config)
        except SpeechPreflightError as exc:
            if exc.reason in _PREFLIGHT_FATAL_REASONS:
                raise
            _log_preflight(exc.reason, attempt)
        else:
            _log_preflight("ok", attempt)
            return True
        try:
            await asyncio.wait_for(stop.wait(), timeout=backoff)
        except asyncio.TimeoutError:
            backoff = min(backoff * 2, _PREFLIGHT_RETRY_MAX_SECONDS)
    return False


# The preflight proves TTS by synthesizing "On it!" and then deliberately
# discards it (zero retention before pool authentication), so the first real
# acknowledgement of the worker's life used to pay a full cold round trip
# against the <=1.5 s budget. Warm the runtime cache instead, off the critical
# path, with the phrases that can be spoken earliest. Members of the closed
# server-owned vocabulary only — the cache refuses anything else anyway.
WARM_PHRASE_TEXTS: tuple[str, ...] = (
    "On it!",
    "Hi! I'm ready when you are.",
    "I'm on it.",
    "Let me take care of that.",
)
#: Every warmed phrase is an acknowledgement or greeting, which the
#: coordinator reserves at the single-announcement ceiling.
_WARM_PHRASE_SAMPLES = KOKORO_SAMPLE_RATE * 4


async def warm_phrase_cache(tts: Any) -> None:
    """Prime the bounded phrase cache without ever failing the worker."""

    for text in WARM_PHRASE_TEXTS:
        if text not in SERVER_OWNED_PHRASE_TEXTS:
            continue
        try:
            await tts.synthesize(text, max_duration_samples=_WARM_PHRASE_SAMPLES)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The service regressed since the preflight proved it. The live
            # synthesis path is unchanged; stop rather than retry the rest.
            return


def _start_phrase_cache_warm(client: Any) -> asyncio.Task[None] | None:
    tts = getattr(client, "speech_tts", None)
    if tts is None:
        return None
    return asyncio.create_task(warm_phrase_cache(tts), name="voice-phrase-warm")


def _build_speech_transport(config: WorkerConfig) -> Any:
    from .streaming_egress import FixedOriginHttpTransport

    return FixedOriginHttpTransport(
        config.speech_base_url,
        allow_insecure_loopback_development=(
            config.environment in {"development", "test"}
        ),
    )


class ForbiddenRuntimeImport(RuntimeError):
    """An authority-bearing package crossed the worker isolation boundary."""


class RuntimeImportGuard(importlib.abc.MetaPathFinder):
    """Reject Agents, LLM, tool, database, and LiveKit API imports at runtime."""

    def find_spec(
        self,
        fullname: str,
        path: list[str] | None = None,
        target: ModuleType | None = None,
    ) -> None:
        del path, target
        forbidden = _matching_prefix(fullname, _FORBIDDEN_MODULE_PREFIXES)
        if forbidden is not None:
            raise ForbiddenRuntimeImport(f"forbidden_runtime_import:{forbidden}")
        return None

    def assert_clean(self, module_names: Set[str]) -> None:
        """Fail if forbidden authority was imported before guard installation."""

        for name in sorted(module_names):
            forbidden = _matching_prefix(name, _FORBIDDEN_MODULE_PREFIXES)
            if forbidden is not None:
                raise ForbiddenRuntimeImport(f"forbidden_runtime_import:{forbidden}")

    def install(self) -> None:
        """Install once at the front of import resolution."""

        if self not in sys.meta_path:
            sys.meta_path.insert(0, self)


def assert_runtime_distributions(
    installed: Mapping[str, str] | None = None,
) -> None:
    """Verify the deployed closure excludes authority-bearing distributions."""

    if installed is None:
        installed = {
            distribution.metadata["Name"]: distribution.version
            for distribution in importlib.metadata.distributions()
            if distribution.metadata.get("Name")
        }
    normalized = {
        _normalize_distribution_name(name): version
        for name, version in installed.items()
    }
    prohibited = sorted(set(normalized).intersection(_FORBIDDEN_DISTRIBUTIONS))
    if prohibited:
        raise ForbiddenRuntimeImport("forbidden_runtime_distribution:" + prohibited[0])
    for name, expected_version in _REQUIRED_EXACT_DISTRIBUTIONS.items():
        actual_version = normalized.get(name)
        if actual_version != expected_version:
            raise ForbiddenRuntimeImport(f"unexpected_runtime_distribution:{name}")


async def run_worker(config: WorkerConfig | None = None) -> None:
    """Run the authenticated pool client until the process receives a signal."""

    resolved = config or WorkerConfig.from_environ()
    guard = RuntimeImportGuard()
    guard.assert_clean(set(sys.modules))
    guard.install()
    assert_runtime_distributions()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for stop_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(stop_signal, stop.set)
        except (NotImplementedError, RuntimeError):
            continue
    if not await preflight_until_ready(resolved, stop):
        return
    await preload_vad_model()
    client = build_pool_client(resolved)
    warm = _start_phrase_cache_warm(client)
    bridge = WatchBridgeServer(
        supervisor=client.supervisor,
        secret=resolved.control_secret,
        worker_identity=resolved.worker_identity,
        host=resolved.watch_bridge_listen_host,
        port=resolved.watch_bridge_listen_port,
    )
    await bridge.start()
    try:
        await client.run_forever(stop)
    finally:
        if warm is not None:
            warm.cancel()
            await asyncio.gather(warm, return_exceptions=True)
        await bridge.close()


def main() -> int:
    """Return a stable process status without rendering secret-bearing errors."""

    try:
        asyncio.run(run_worker())
    except (ConfigError, ForbiddenRuntimeImport, SpeechPreflightError) as exc:
        print(f"voice_worker_startup_failed:{exc}", file=sys.stderr)
        return 78
    except (ChallengeError, ProtocolViolation) as exc:
        print(f"voice_worker_control_failed:{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("voice_worker_failed:unexpected", file=sys.stderr)
        return 1
    return 0


def _matching_prefix(name: str, prefixes: Set[str]) -> str | None:
    for prefix in prefixes:
        if name == prefix or name.startswith(prefix + "."):
            return prefix
    return None


def _normalize_distribution_name(name: Any) -> str:
    return str(name).strip().lower().replace("_", "-").replace(".", "-")


if __name__ == "__main__":
    raise SystemExit(main())
