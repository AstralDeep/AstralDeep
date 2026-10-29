"""Exercise private native egress admission and deadline/cancellation boundaries without a network service."""

from __future__ import annotations

import importlib

import pytest
import requests

from personalization.semantic_pilot import providers, runner
from personalization.semantic_pilot.bounds import Bounds
from personalization.semantic_pilot.corpus import load_corpus
from personalization.semantic_pilot.providers import (
    ProviderFault,
    ProviderUsage,
    RunCancelled,
    TeiEndpoint,
    VectorGateway,
    resolve_providers,
    tei_http_transport,
)


@pytest.mark.parametrize("base_url", ["http://127.0.0.1:8080", "http://tei.internal:8080",
                                      "https://[::1]:8080"])
@pytest.mark.parametrize("route", ["/v1/embeddings", "/rerank"])
@pytest.mark.parametrize("approved", [False, True])
def test_private_endpoint_port_crosses_real_egress_validator(monkeypatch, base_url, route, approved):
    monkeypatch.delenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS", raising=False)
    monkeypatch.setattr(providers.external_http, "_resolve_host_addresses", lambda host: ["127.0.0.1"])
    sent = []

    def request(method, url, **kwargs):
        sent.append((url, kwargs))
        response = requests.Response()
        response.status_code = 200
        response._content = b"[]"
        response._content_consumed = True
        return response

    monkeypatch.setattr(requests, "request", request)
    endpoint = TeiEndpoint(base_url, allow_private_endpoint=approved)
    transport = tei_http_transport(endpoint, endpoint)
    if approved:
        assert transport(route, {"query": "synthetic"}) == b"[]"
        assert len(sent) == 1
        assert sent[0][0] == base_url + route
        assert sent[0][1].get("verify", True) is True
        assert sent[0][1]["allow_redirects"] is False
    else:
        with pytest.raises(ProviderFault, match="egress_blocked"):
            transport(route, {"query": "synthetic"})
        assert sent == []


@pytest.mark.parametrize("operation", ["embed", "rerank"])
@pytest.mark.parametrize("fault", [False, True])
def test_cancel_during_final_provider_attempt_rejects_result(operation, fault):
    _, identity, _ = resolve_providers("scripted-double")
    cancelled = False

    def transport(route, payload):
        nonlocal cancelled
        cancelled = True
        if fault:
            raise ProviderFault("http_unreachable", "synthetic failure after cancellation")
        if operation == "embed":
            return {"data": [{"index": 0, "embedding": [0.] * identity.dimension}]}
        return [{"index": 0, "score": .9}]

    gateway = VectorGateway(identity, transport)
    usage = ProviderUsage()
    with pytest.raises(RunCancelled):
        if operation == "embed":
            gateway.embed(["synthetic"], Bounds(max_retries=0), usage, lambda: cancelled)
        else:
            gateway.rerank("synthetic", ["text"], Bounds(max_retries=0), usage, lambda: cancelled)
    assert usage.embedding_calls + usage.rerank_calls == 1
    assert usage.vectors == 0


@pytest.mark.parametrize("fault", [False, True])
def test_cancel_during_retry_never_returns_success_or_final_failure(fault):
    gateway, identity, _ = resolve_providers("scripted-double")
    cancelled = False
    attempts = 0

    def transport(route, payload):
        nonlocal cancelled, attempts
        attempts += 1
        if attempts == 1:
            raise ProviderFault("http_unreachable", "first synthetic failure")
        cancelled = True
        if fault:
            raise ProviderFault("http_unreachable", "retry failure after cancellation")
        return {"data": [{"index": 0, "embedding": [0.] * identity.dimension}]}

    gateway = VectorGateway(identity, transport)
    usage = ProviderUsage()
    with pytest.raises(RunCancelled):
        gateway.embed(["synthetic"], Bounds(max_retries=1), usage, lambda: cancelled)
    assert attempts == 2
    assert usage.retries == 1


@pytest.fixture()
def defenses(monkeypatch):
    for name in ("FF_MEMORY_GUARD", "FF_MEMORY_TEMPORAL", "FF_PROJECT_MEMORY"):
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("MEMORY_HMAC_KEY", "synthetic-runtime-test-key")


def _one_final_case(monkeypatch):
    selection, final, identity = load_corpus()
    monkeypatch.setattr(runner, "load_corpus", lambda: ((), (final[0],), identity))


def test_last_inference_deadline_overrun_cancels_pilot(monkeypatch, defenses):
    _one_final_case(monkeypatch)
    now = [0.]
    monkeypatch.setattr(runner.time, "monotonic", lambda: now[0])
    gateway, identity, status = resolve_providers("scripted-double")
    original = gateway._transport

    def transport(route, payload):
        result = original(route, payload)
        now[0] = 2.
        return result

    gateway._transport = transport
    monkeypatch.setattr(runner, "resolve_providers", lambda *args: (gateway, identity, status))
    with pytest.raises(runner.RunCancelled):
        runner.run_pilot(runner.PilotConfig(
            profile="scripted-double", candidate_k_grid=(1,), rerank_enabled=False,
            bounds=Bounds(runtime_deadline_seconds=1.)))


@pytest.mark.parametrize("phase", ["baseline", "aggregate", "report"])
def test_final_nonprovider_work_cannot_overrun_deadline(monkeypatch, defenses, phase):
    _one_final_case(monkeypatch)
    now = [0.]
    monkeypatch.setattr(runner.time, "monotonic", lambda: now[0])
    if phase == "baseline":
        from personalization.memory_tools import MemoryTools

        original = MemoryTools.memory_search_ranked

        def baseline(*args, **kwargs):
            result = original(*args, **kwargs)
            now[0] = 2.
            return result

        monkeypatch.setattr(MemoryTools, "memory_search_ranked", baseline)
    else:
        name = "_aggregate" if phase == "aggregate" else "build_report"
        original = getattr(runner, name)

        def work(*args, **kwargs):
            result = original(*args, **kwargs)
            now[0] = 2.
            return result

        monkeypatch.setattr(runner, name, work)
    with pytest.raises(runner.RunCancelled):
        runner.run_pilot(runner.PilotConfig(
            profile="tei-http", candidate_k_grid=(1,), rerank_enabled=False,
            bounds=Bounds(runtime_deadline_seconds=1.)))


def test_cli_deadline_watchdog_terminates_stalled_worker(monkeypatch):
    runtime = importlib.import_module("personalization.semantic_pilot.runtime")
    events = []

    class Pipe:
        def poll(self, timeout):
            return False

        def close(self):
            events.append("pipe closed")

    class Worker:
        alive = True

        def start(self):
            events.append("worker started")

        def is_alive(self):
            return self.alive

        def terminate(self):
            self.alive = False
            events.append("worker terminated")

        def join(self, timeout):
            events.append("worker joined")

        def close(self):
            events.append("worker closed")

    class Context:
        def Pipe(self, duplex):
            return Pipe(), Pipe()

        def Process(self, **kwargs):
            return Worker()

    monkeypatch.setattr(runtime.multiprocessing, "get_context", lambda method: Context())
    with pytest.raises(runner.RunCancelled):
        runtime.run_pilot_bounded(runner.PilotConfig(bounds=Bounds(runtime_deadline_seconds=1.)))
    assert "worker terminated" in events
    assert "worker joined" in events
    assert events.count("pipe closed") == 2
    assert "worker closed" in events


def test_cli_deadline_worker_executes_real_diagnostic(defenses):
    runtime = importlib.import_module("personalization.semantic_pilot.runtime")
    report = runtime.run_pilot_bounded(runner.PilotConfig(
        profile="offline-diagnostic", bounds=Bounds(runtime_deadline_seconds=20.)))
    assert report["provider"]["embedding_qualification"]["state"] == "executed_diagnostic_only"
    assert report["recommendation"]["recommendation"] == "inconclusive"


@pytest.mark.parametrize("kind,exception", [("PilotError", runner.PilotError),
                                           ("ValueError", ValueError),
                                           ("error", RuntimeError),
                                           ("cancelled", runner.RunCancelled),
                                           ("eof", runner.PilotError),
                                           ("late", runner.RunCancelled),
                                           ("ok", None)])
def test_deadline_worker_reply_never_turns_failure_into_success(monkeypatch, kind, exception):
    runtime = importlib.import_module("personalization.semantic_pilot.runtime")
    now = [0.]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: now[0])
    closed = []

    class Pipe:
        def poll(self, timeout):
            return True

        def recv(self):
            if kind == "eof":
                raise EOFError
            if kind == "late":
                now[0] = 2.
                return "ok", {"synthetic": True}
            return kind, {"synthetic": True} if kind == "ok" else "synthetic failure"

        def close(self):
            closed.append(self)

    class Worker:
        def start(self):
            pass

        def join(self, timeout):
            pass

        def is_alive(self):
            return False

        def close(self):
            closed.append(self)

    class Context:
        def Pipe(self, duplex):
            return Pipe(), Pipe()

        def Process(self, **kwargs):
            return Worker()

    monkeypatch.setattr(runtime.multiprocessing, "get_context", lambda method: Context())
    config = runner.PilotConfig(bounds=Bounds(runtime_deadline_seconds=1.))
    if exception:
        with pytest.raises(exception):
            runtime.run_pilot_bounded(config)
    else:
        assert runtime.run_pilot_bounded(config) == {"synthetic": True}
    assert len(closed) == 3


@pytest.mark.parametrize("error", [runner.RunCancelled("synthetic"),
                                   runner.PilotError("synthetic"), ValueError("synthetic"),
                                   RuntimeError("synthetic")])
def test_worker_failure_protocol_preserves_failure_category(monkeypatch, error):
    runtime = importlib.import_module("personalization.semantic_pilot.runtime")
    sent = []
    closed = []

    def fail(config):
        raise error

    class Sender:
        def send(self, message):
            sent.append(message)

        def close(self):
            closed.append(True)

    monkeypatch.setattr(runtime, "run_pilot", fail)
    runtime._run_worker(runner.PilotConfig(), Sender())
    expected = ("cancelled" if isinstance(error, runner.RunCancelled) else
                type(error).__name__ if isinstance(error, (runner.PilotError, ValueError)) else "error")
    assert sent[0][0] == expected
    assert closed == [True]


def test_bounded_library_entry_without_deadline_keeps_diagnostic_unqualified(defenses):
    runtime = importlib.import_module("personalization.semantic_pilot.runtime")
    report = runtime.run_pilot_bounded(runner.PilotConfig())
    assert report["provider"]["embedding_qualification"]["state"] == "executed_diagnostic_only"


@pytest.mark.parametrize("start_error,kill_works", [(True, True), (False, True), (False, False)])
def test_watchdog_cleans_up_start_failure_and_escalates_stuck_worker(monkeypatch, start_error, kill_works):
    runtime = importlib.import_module("personalization.semantic_pilot.runtime")
    killed = []
    closed = []

    class Pipe:
        def poll(self, timeout):
            return False

        def close(self):
            closed.append("pipe")

    class Worker:
        alive = True

        def start(self):
            if start_error:
                raise OSError("synthetic spawn failure")

        def join(self, timeout):
            pass

        def is_alive(self):
            return self.alive

        def terminate(self):
            pass

        def kill(self):
            killed.append(True)
            self.alive = not kill_works

        def close(self):
            closed.append("worker")

    class Context:
        def Pipe(self, duplex):
            return Pipe(), Pipe()

        def Process(self, **kwargs):
            return Worker()

    monkeypatch.setattr(runtime.multiprocessing, "get_context", lambda method: Context())
    error = OSError if start_error else runner.RunCancelled if kill_works else runner.PilotError
    with pytest.raises(error):
        runtime.run_pilot_bounded(runner.PilotConfig(bounds=Bounds(runtime_deadline_seconds=1.)))
    assert closed.count("pipe") == 2
    if not start_error:
        assert killed == [True]


@pytest.mark.parametrize("seconds", [float("nan"), float("inf")])
def test_host_deadline_requires_a_finite_limit(seconds):
    with pytest.raises(ValueError, match="finite"):
        Bounds(runtime_deadline_seconds=seconds)
