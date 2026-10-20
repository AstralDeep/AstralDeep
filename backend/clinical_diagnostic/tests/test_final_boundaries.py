"""Synthetic regressions for diagnostic accounting, active cancellation, infrastructure
reports and truthful model/workflow identity across the real runner and adapters.
"""

from __future__ import annotations

import json
import threading
import ssl
from types import SimpleNamespace

import pytest
from websockets.exceptions import InvalidHandshake

from clinical_diagnostic.budget import BudgetTracker, ExecutionLimits
from clinical_diagnostic.dispatcher import DispatcherProducer, WebSocketDispatcher
from clinical_diagnostic.grading import CallableGrader, MarkerGrader
from clinical_diagnostic.healthbench import parse_example
from clinical_diagnostic.models import ClinicalCase, ConversationMessage, Partition, RubricItem, Setting
from clinical_diagnostic.provider import BareModelProducer, CallableProvider, ProviderReply
from clinical_diagnostic.runner import DiagnosticConfig, _execute_jobs, _provider_config, run


def case():
    return ClinicalCase(
        case_id="synthetic-boundary", partition=Partition.DEVELOPMENT,
        setting=Setting.CONSULTATION, specialty="synthetic",
        conversation=(ConversationMessage("user", "Synthetic question?"),),
        rubric_items=(RubricItem("criterion", "States the fact.", 1, evidence_terms=("fact",)),),
    )


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


class Transport:
    def __init__(self, failure=None):
        self.sent = []
        self.closed = False
        self.failure = failure

    def send(self, text):
        if self.failure:
            raise self.failure
        self.sent.append(json.loads(text))

    def recv(self, timeout):
        raise TimeoutError()

    def close(self):
        self.closed = True


def preflight(method, url, headers, body):
    if method == "POST":
        payload = {"chat_id": "synthetic-chat"}
    elif url.endswith("/components"):
        payload = {"components": []}
    else:
        payload = {"chat": {"id": "synthetic-chat", "messages": []}}
    return SimpleNamespace(status=201 if method == "POST" else 200,
                           body=json.dumps(payload).encode())


def producer(transport=None, factory=None, http=preflight, **kwargs):
    fields = dict(
        token="synthetic-token", ws_uri="wss://backend.invalid/ws",
        http_uri="https://backend.invalid", http=http,
        transport_factory=factory or (lambda: transport or Transport()))
    fields.update(kwargs)
    return DispatcherProducer(WebSocketDispatcher(**fields))


def test_invalid_completed_grade_cost_blocks_the_next_paid_job():
    answers = BareModelProducer(CallableProvider(lambda _: ProviderReply("fact", "answer"), "answer"))
    calls = []

    def invalid(_messages):
        calls.append("completed")
        return ProviderReply("invalid JSON", "grader", cost=1)

    budget = BudgetTracker(ExecutionLimits(max_cost=0.25, max_calls=4, concurrency=1))
    results = _execute_jobs([(answers, case()), (answers, case())],
                            CallableGrader(CallableProvider(invalid, "grader")), budget, 0)
    assert budget.cost == 1
    assert calls == ["completed"]
    assert [r.failure.code.value for r in results] == ["invalid_grade", "budget_exhausted"]


@pytest.mark.parametrize("cancel", [False, True])
def test_preflight_exhaustion_never_submits_registration_or_chat(cancel):
    clock, event, transport = Clock(), threading.Event(), Transport()

    def http(*args):
        response = preflight(*args)
        clock.now = 2
        if cancel:
            event.set()
        return response

    budget = BudgetTracker(ExecutionLimits(max_elapsed_seconds=1), clock=clock, cancel_event=event)
    result = _execute_jobs([(producer(transport, http=http), case())], MarkerGrader(), budget, 0)[0]
    assert transport.sent == []
    assert result.failure.code.value == ("cancelled" if cancel else "budget_exhausted")


@pytest.mark.parametrize("failure", [TimeoutError(), ConnectionError(), ssl.SSLError(), InvalidHandshake()])
def test_open_and_send_failures_still_write_non_success_reports(tmp_path, monkeypatch, failure):
    import clinical_diagnostic.runner as runner

    monkeypatch.setattr(runner, "_public_cases", lambda _: (
        [case()], SimpleNamespace(to_dict=lambda: {}, license_name="synthetic"), ""))
    if isinstance(failure, TimeoutError):
        def factory():
            raise failure
        selected = producer(factory=factory)
    else:
        selected = producer(Transport(failure))
    outcome = run(DiagnosticConfig(
        mode="public", public_opt_in=True, acknowledge_unreviewed_public=True,
        producers=(selected,), grader=MarkerGrader(), artifacts_root=str(tmp_path),
        limits=ExecutionLimits(max_retries=0),
    ))
    assert outcome.exit_code == 3
    assert outcome.record.results[0].status == "infrastructure_failure"
    assert outcome.record.results[0].score is None
    assert (tmp_path / "local" / "clinical_diagnostic.record.json").exists()
    assert (tmp_path / "local" / "CLINICAL_DIAGNOSTIC_REPORT.md").exists()


def test_dispatcher_config_identity_changes_with_the_actual_backend_endpoint():
    first = _provider_config(producer())
    second = _provider_config(DispatcherProducer(WebSocketDispatcher(
        token="synthetic-token", ws_uri="wss://other.invalid/ws", http_uri="https://other.invalid")))
    assert first is not None
    assert first != second
    assert "synthetic-token" not in json.dumps(first)


@pytest.mark.parametrize("workflow", ["documentation", "research", "unmapped-workflow"])
def test_official_workflow_is_not_silently_consultation(workflow):
    payload = {
        "id": "synthetic-official", "specialty": "synthetic",
        "conversation": {"messages": [{"role": "user", "content": "Synthetic question?"}]},
        "rubric_items": [{"criterion_text": "States the fact.", "points": 1}],
        "use_case": workflow, "type": "synthetic", "difficulty": "synthetic",
        "physician_response": "excluded synthetic reference", "canary_string": "excluded synthetic marker",
    }
    parsed = parse_example(payload)
    assert parsed.setting is Setting.UNKNOWN
    assert parsed.official_use_case == workflow
    assert "excluded synthetic" not in json.dumps(parsed.to_dict())


def test_cancellation_between_registration_and_chat_never_submits_chat():
    event, transport = threading.Event(), Transport()
    original = transport.send

    def send(text):
        original(text)
        event.set()

    transport.send = send
    budget = BudgetTracker(ExecutionLimits(), cancel_event=event)
    result = _execute_jobs([(producer(transport), case())], MarkerGrader(), budget, 0)[0]
    assert [frame["type"] for frame in transport.sent] == ["register_ui"]
    assert result.failure.code.value == "cancelled"
    assert transport.closed


def public_run(tmp_path, monkeypatch, selected, grader=None):
    import clinical_diagnostic.runner as runner

    monkeypatch.setattr(runner, "_public_cases", lambda _: (
        [case()], SimpleNamespace(to_dict=lambda: {}, license_name="synthetic"), ""))
    return run(DiagnosticConfig(
        mode="public", public_opt_in=True, acknowledge_unreviewed_public=True,
        producers=(selected,), grader=grader or MarkerGrader(), artifacts_root=str(tmp_path),
        limits=ExecutionLimits(max_retries=0),
    ))


@pytest.mark.parametrize("usage,text,known", [
    ("invalid", "fact", 0), (-1, "fact", 0), (True, "fact", 0),
    (2**54, "fact", 0), (1, "x" * 65537, 0.5),
])
def test_malformed_provider_reply_is_reported_with_only_known_usage(
        tmp_path, monkeypatch, usage, text, known):
    from clinical_diagnostic.provider import CompletionConfig, OpenAICompletionProvider
    from clinical_diagnostic.tests.test_provider import _Choice, _patch_client

    _patch_client(monkeypatch, SimpleNamespace(
        choices=[_Choice(text)], usage=SimpleNamespace(prompt_tokens=usage, completion_tokens=0)))
    selected = BareModelProducer(OpenAICompletionProvider(CompletionConfig(
        base_url="https://provider.invalid/v1", model="synthetic-model", input_cost_per_token=0.5)))
    outcome = public_run(tmp_path, monkeypatch, selected)
    assert outcome.exit_code == 3
    assert outcome.record.results[0].failure.code.value == "provider_error"
    assert outcome.record.summary["accounting"]["known_cost"] == known
    assert json.loads(open(outcome.record_path).read())["results"][0]["score"] is None
    assert "Infrastructure" in open(outcome.report_path).read()


def test_remaining_allowance_reaches_factory_and_completion_and_client_closes(monkeypatch):
    from clinical_diagnostic.budget import use_control
    from clinical_diagnostic.provider import CompletionConfig, OpenAICompletionProvider
    from clinical_diagnostic.tests.test_provider import _Choice

    clock, observed, closed = Clock(), [], []
    budget = BudgetTracker(ExecutionLimits(max_elapsed_seconds=5), clock=clock)

    def completion(**kwargs):
        observed.append(kwargs["timeout"])
        return SimpleNamespace(choices=[_Choice("fact")])

    def factory(*args, **kwargs):
        observed.append(kwargs["timeout"])
        clock.now = 4
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=completion)),
                               close=lambda: closed.append(True)), None, None

    monkeypatch.setattr("llm_config.client_factory.build_llm_client", factory)
    with use_control(budget):
        reply = OpenAICompletionProvider(CompletionConfig(
            base_url="https://provider.invalid/v1", model="synthetic-model"))._complete([])
    assert reply.text == "fact"
    assert observed == [5, 1]
    assert closed == [True]


def test_deadline_after_factory_never_submits_completion_and_still_closes(monkeypatch):
    from clinical_diagnostic.budget import BudgetExhausted, use_control
    from clinical_diagnostic.provider import CompletionConfig, OpenAICompletionProvider

    clock, calls, closed = Clock(), [], []
    budget = BudgetTracker(ExecutionLimits(max_elapsed_seconds=5), clock=clock)

    def factory(*args, **kwargs):
        clock.now = 6
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kwargs: calls.append(kwargs))), close=lambda: closed.append(True)), None, None

    monkeypatch.setattr("llm_config.client_factory.build_llm_client", factory)
    with use_control(budget), pytest.raises(BudgetExhausted):
        OpenAICompletionProvider(CompletionConfig(
            base_url="https://provider.invalid/v1", model="synthetic-model"))._complete([])
    assert calls == [] and closed == [True]


@pytest.mark.parametrize("malformed", [False, True])
def test_close_failure_retains_completed_charge_and_primary_protocol_failure(monkeypatch, malformed):
    from clinical_diagnostic.provider import CompletionConfig, OpenAICompletionProvider, ProducerError
    from clinical_diagnostic.tests.test_provider import _Choice

    def close():
        raise ConnectionError("synthetic close failure")

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs:
        SimpleNamespace(choices=[_Choice(None if malformed else "fact")],
                        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=0)))), close=close)
    monkeypatch.setattr("llm_config.client_factory.build_llm_client", lambda *args, **kwargs: (client, None, None))
    with pytest.raises(ProducerError) as raised:
        OpenAICompletionProvider(CompletionConfig(base_url="https://provider.invalid/v1", model="m",
                                                  input_cost_per_token=0.5))._complete([])
    assert raised.value.completed_cost == 1
    assert raised.value.detail == ("provider returned a malformed response" if malformed else "ConnectionError")


def test_observed_configuration_changes_digest_and_keeps_returned_model_identities(tmp_path, monkeypatch):
    from clinical_diagnostic.identity import AstralModelObservation

    first = producer()
    first.adapter.model_observation = AstralModelObservation(
        "custom", "configured-model", "https://provider.invalid/v1", "synthetic-observation-1")
    config = _provider_config(first)
    assert config["model"]["status"] == "operator_verified_input"
    from clinical_diagnostic.run_record import RunIdentity

    def digest(value):
        return RunIdentity("public", {}, ("astral_dispatcher",), "grader", value).config_digest

    before = digest(config)
    first.adapter.model_observation = AstralModelObservation(
        "custom", "another-model", "https://provider.invalid/v1", "synthetic-observation-2")
    assert digest(_provider_config(first)) != before
    answers = BareModelProducer(CallableProvider(lambda _: ProviderReply("fact", "returned-model"), "adapter"))
    grader = CallableGrader(CallableProvider(lambda _: ProviderReply(
        '{"assessments":[{"criterion_id":"criterion","met":true}]}', "returned-grader"), "grader-adapter"))
    outcome = public_run(tmp_path, monkeypatch, answers, grader)
    result = outcome.record.results[0].to_dict()
    assert result["model_identity"] == "returned-model"
    assert result["grader_identity"] == "returned-grader"


@pytest.mark.parametrize("url", ["https://user:password@backend.invalid", "https://backend.invalid?token=secret",
                                 "https://backend.invalid#secret", "relative"])
def test_credential_bearing_or_ambiguous_endpoints_are_refused(url):
    from clinical_diagnostic.identity import AstralModelObservation

    with pytest.raises(ValueError):
        producer(http_uri=url)
    with pytest.raises(ValueError):
        AstralModelObservation("custom", "model", url, "synthetic-receipt")


def test_model_observation_closed_parser_and_unknown_workflow_aggregation(tmp_path):
    from dataclasses import replace
    from clinical_diagnostic.identity import load_model_observation
    from clinical_diagnostic.report import summarize

    path = tmp_path / "observation.json"
    payload = {"provider": "custom", "model": "m", "base_url": "https://provider.invalid", "verification_reference": "r"}
    path.write_text(json.dumps(payload))
    assert load_model_observation(path).model == "m"
    path.write_text(json.dumps({**payload, "api_key": "excluded"}))
    with pytest.raises(ValueError):
        load_model_observation(path)
    path.write_text(json.dumps({**payload, "model": ""}))
    with pytest.raises(ValueError):
        load_model_observation(path)
    selected = replace(case(), setting=Setting.UNKNOWN, official_use_case="research")
    answers = BareModelProducer(CallableProvider(lambda _: ProviderReply("fact", "model"), "model"))
    result = _execute_jobs([(answers, selected)], MarkerGrader(), BudgetTracker(ExecutionLimits()), 0)[0]
    summary = summarize([result], {})
    assert summary["by_setting"] == {"unknown": 1}
    assert summary["by_official_use_case"] == {"research": 1}
    assert result.to_dict()["official_use_case"] == "research"


def test_programming_failure_finishes_partial_report_as_internal_error(tmp_path, monkeypatch):
    def broken(_):
        raise RuntimeError("do not expose exception payloads")

    outcome = public_run(tmp_path, monkeypatch, BareModelProducer(CallableProvider(broken, "broken")))
    assert outcome.exit_code == 3
    result = outcome.record.results[0]
    assert result.failure.code.value == "internal_error"
    assert result.failure.detail == "RuntimeError"
    assert result.score is None


def test_concrete_dispatcher_routes_the_complete_operation_through_supervision(monkeypatch):
    from clinical_diagnostic.budget import use_control
    from clinical_diagnostic.dispatcher import DispatchedAnswer

    captured = []

    def supervisor(kind, payload, control):
        captured.append((kind, payload, control))
        return DispatchedAnswer("fact", "chat")

    monkeypatch.setattr("clinical_diagnostic.dispatcher.supervise", supervisor)
    clock = Clock()
    budget = BudgetTracker(ExecutionLimits(max_elapsed_seconds=7), clock=clock)
    clock.now = 4
    selected = WebSocketDispatcher(token="synthetic-token", ws_uri="wss://backend.invalid/ws",
                                   http_uri="https://backend.invalid", timeout_seconds=120)
    with use_control(budget):
        assert selected.answer(case()).text == "fact"
    kind, payload, control = captured[0]
    assert kind == "dispatcher" and payload["case"] == case()
    assert control.remaining_seconds() == 3
    assert control.cancel_event is budget.cancel_event


def test_http_timeout_is_capped_by_the_shared_remaining_allowance(monkeypatch):
    from clinical_diagnostic.budget import use_control
    from clinical_diagnostic.dispatcher import _http_request

    clock, observed = Clock(), []
    budget = BudgetTracker(ExecutionLimits(max_elapsed_seconds=5), clock=clock)
    clock.now = 3

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, bound):
            return b"{}"

    def urlopen(request, timeout):
        observed.append(timeout)
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    with use_control(budget):
        _http_request("GET", "https://backend.invalid", {}, None)
    assert observed == [2]


def test_cli_loads_only_the_closed_nonsecret_model_observation(tmp_path, monkeypatch):
    import clinical_diagnostic.__main__ as cli

    path = tmp_path / "observation.json"
    path.write_text(json.dumps({"provider": "custom", "model": "observed-model",
        "base_url": "https://provider.invalid/v1", "verification_reference": "synthetic-receipt"}))
    selected = []
    monkeypatch.setenv("ASTRAL_CLINICAL_PUBLIC_RUN", "1")
    monkeypatch.setenv("ASTRAL_CLINICAL_DISPATCHER_TOKEN", "synthetic-token")
    monkeypatch.setattr(cli, "load_completion_config", lambda _: None)
    monkeypatch.setattr(cli, "OpenAICompletionProvider", lambda _: None)
    monkeypatch.setattr(cli, "build_public_grader", lambda _: MarkerGrader())

    def build(bare, dispatcher):
        selected.append(dispatcher)
        return ()

    monkeypatch.setattr(cli, "build_public_producers", build)
    monkeypatch.setattr(cli, "run", lambda _: SimpleNamespace(
        record=SimpleNamespace(summary={}), record_path="synthetic.json", report_path="synthetic.md",
        offenders=(), exit_code=0))
    assert cli.main(["--mode", "public", "--bare-config", "synthetic-bare.json",
        "--grader-config", "synthetic-grader.json", "--ws-uri", "wss://backend.invalid/ws",
        "--http-uri", "https://backend.invalid", "--astral-model-observation", str(path)]) == 0
    assert selected[0].model_observation.model == "observed-model"
    assert "synthetic-token" not in json.dumps(DispatcherProducer(selected[0]).to_dict())


@pytest.mark.parametrize("returned_model", [None, "", "observed-model"])
def test_absent_returned_model_stays_unknown_in_actual_run_records(
        tmp_path, monkeypatch, returned_model):
    import clinical_diagnostic.runner as runner
    from clinical_diagnostic.provider import CompletionConfig, OpenAICompletionProvider
    from clinical_diagnostic.tests.test_provider import _Choice, _patch_client

    payload = dict(choices=[_Choice("fact")])
    if returned_model is not None:
        payload["model"] = returned_model
    _patch_client(monkeypatch, SimpleNamespace(**payload))
    answer_provider = OpenAICompletionProvider(CompletionConfig(
        base_url="https://provider.invalid/v1", model="requested-answer"))
    grader_provider = OpenAICompletionProvider(CompletionConfig(
        base_url="https://provider.invalid/v1", model="requested-grader"))
    original = grader_provider._reply

    def grade_reply(response):
        response.choices = [_Choice('{"assessments":[{"criterion_id":"criterion","met":true}]}')]
        return original(response)

    monkeypatch.setattr(grader_provider, "complete", lambda messages: grade_reply(SimpleNamespace(**payload)))
    before = runner._provider_config(BareModelProducer(answer_provider))
    outcome = public_run(tmp_path, monkeypatch, BareModelProducer(answer_provider), CallableGrader(grader_provider))
    assert outcome.exit_code == 0
    record = json.loads(open(outcome.record_path).read())
    expected = returned_model or "unknown"
    assert record["results"][0]["model_identity"] == expected
    assert record["results"][0]["grader_identity"] == expected
    assert record["identity"]["config"]["producer_configs"]["bare_model"]["model"] == "requested-answer"
    assert record["identity"]["config"]["grader_config"]["model"] == "requested-grader"
    assert runner._provider_config(BareModelProducer(answer_provider)) == before
