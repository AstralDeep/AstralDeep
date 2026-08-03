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
)
from .speech_adapters import (
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
    tts = SpeachesTTS(transport=transport, api_key=config.speech_api_key)
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
    await run_speech_preflight(resolved)
    client = build_pool_client(resolved)
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
