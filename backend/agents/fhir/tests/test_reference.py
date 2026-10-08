"""Exercises patient measurements and population queries over bounded synthetic FHIR fixtures.
The tests verify provenance, input denials, malformed responses, transport failures and MCP delivery.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.fhir import mcp_tools, reference
from agents.fhir.client import FhirError
from agents.fhir.mcp_server import MCPServer
from agents.fhir.tests.conftest import BASE, NOW, at, observation, pressure, quantity
from agents.fhir.tests.test_tools import card_of, of_type
from shared import external_http
from shared.protocol import MCPRequest

TAG = {"tag": [{"system": reference.SYNTHETIC_TAG_SYSTEM, "code": "synthetic"}]}
EMPTY_SCOPE = {"state": None, "counties": [], "pregnant": None}


@pytest.fixture
def patient_fixture(connected, monkeypatch):
    patient = connected.resources["Patient"][0]
    patient.update(name=[{"use": "official", "given": ["Jane"], "family": "Demo"}], meta=copy.deepcopy(TAG))
    patient["identifier"] = [{"system": reference.DEMO_IDENTIFIER_SYSTEM, "value": "DEMO-1001"}]
    observations = [
        observation("a1c-old", "002-1", "4548-4", "Hemoglobin A1C", "laboratory", 90, **quantity(7.1, "%")),
        observation("a1c-current", "002-1", "4548-4", "Hemoglobin A1C", "laboratory", 30, **quantity(6.3, "%")),
        observation("a1c-preliminary", "002-1", "4548-4", "Hemoglobin A1C", "laboratory", 5, **quantity(99, "%")),
    ]
    for item in observations:
        item["meta"] = copy.deepcopy(TAG)
        item["valueQuantity"].update(system="http://unitsofmeasure.org", code="%")
    observations[1]["status"] = "corrected"
    observations[2]["status"] = "preliminary"
    connected.resources["Observation"] = observations
    original = connected.matches

    def matches(resource, query):
        if query.get("identifier"):
            expected = query["identifier"][0].split("|", 1)
            if not any([item.get("system"), item.get("value")] == expected for item in resource.get("identifier", [])):
                return False
        if query.get("name") and query["name"][0].lower() not in reference.patient_label(resource).lower():
            return False
        return original(resource, query)

    monkeypatch.setattr(connected, "matches", matches)
    return connected


@pytest.fixture
def population_data():
    return {
        "query_id": "a1c-by-county", "generated_at": NOW.isoformat(), "synthetic": True,
        "source": reference.POPULATION_SOURCE, "definition": reference.POPULATION_DEFINITION,
        "scope": copy.deepcopy(EMPTY_SCOPE), "rows": [
            {"county": "Wolfe", "patient_count": 3, "with_a1c_count": 2, "average_a1c": 6.4},
            {"county": "Casey", "patient_count": 1, "with_a1c_count": 0, "average_a1c": None},
        ],
    }


def population_payload(data):
    return {"resourceType": "Parameters", "parameter": [{"name": "result", "valueString": json.dumps(data)}]}


def serve_population(monkeypatch, connected, data):
    original = connected.get
    monkeypatch.setattr(connected, "get", lambda path, query: population_payload(data) if path == "$aggregate-a1c" else original(path, query))


def test_patient_measurements_returns_latest_corrected_result_and_provenance(patient_fixture):
    patient_fixture.failures["$lastn"] = external_http.ServiceUnreachableError("unsupported")
    output = mcp_tools.patient_measurements(patient="002-1")
    card, components = card_of(output)
    data = output["_data"]
    assert data["patient_label"] == "Jane Demo" and data["synthetic"] is True and data["data_origin"] == "synthetic"
    assert data["measurements"] == [{"label": "Hemoglobin A1C", "value": 6.3, "unit": "%",
                                     "measured_at": at(30), "date_basis": "effectiveDateTime", "source_status": "corrected",
                                     "source": "Observation/a1c-current"}]
    assert card["id"] == "fhir-patient-measurements-002-1-a1c" and card["title"] == "Patient measurements"
    assert of_type(components, "table")[0]["rows"][0] == ["Hemoglobin A1C", "6.3", "%", at(30), "effectiveDateTime", "Observation/a1c-current"]
    assert of_type(components, "hero")[0]["eyebrow"] == "Patient-level FHIR REST"
    assert not any("$lastn" in call[1] or "$replay-status" in call[1] for call in patient_fixture.calls)
    parameters = patient_fixture.calls[-1][2]
    assert parameters["patient"] == ["002-1"] and parameters["status"] == ["final,amended,corrected"]
    assert parameters["_sort"] == ["-date"] and parameters["_count"] == ["100"]


def test_exact_demo_identifier_is_system_scoped(patient_fixture):
    data = mcp_tools.patient_measurements(patient="DEMO-1001")["_data"]
    assert data["patient"] == "002-1"
    searches = [call for call in patient_fixture.calls if call[1] == "Patient"]
    assert searches[0][2]["identifier"] == ["urn:myhealthsafe:demo|DEMO-1001"]


def test_demo_identifier_denies_ambiguous_or_missing_matches(patient_fixture):
    duplicate = copy.deepcopy(patient_fixture.resources["Patient"][0])
    duplicate["id"] = "duplicate"
    patient_fixture.resources["Patient"].append(duplicate)
    assert mcp_tools.patient_measurements(patient="DEMO-1001")["_error"]["code"] == "FHIR_BAD_REQUEST"
    assert mcp_tools.patient_measurements(patient="DEMO-9999")["_error"]["code"] == "FHIR_NOT_FOUND"
    assert mcp_tools.patient_measurements(patient="unmatched")["_error"]["code"] == "FHIR_NOT_FOUND"


@pytest.mark.parametrize("record", [
    {"resourceType": "Patient", "id": "bad/id"},
    {"resourceType": "Patient", "id": "valid", "identifier": [{"system": "other", "value": "DEMO-1001"}]},
    {"resourceType": "Observation", "id": "valid"},
])
def test_demo_identifier_rejects_unusable_patient_match(record):
    def read(*args):
        raise FhirError("FHIR_NOT_FOUND", "missing")

    client = SimpleNamespace(read=read, search=lambda *args: ([record], 1))
    with pytest.raises(FhirError, match="invalid patient match"):
        reference.patient_record(client, "DEMO-1001")


def test_patient_identity_mismatch_is_refused():
    with pytest.raises(FhirError, match="invalid patient match"):
        reference.patient_record(SimpleNamespace(read=lambda *args: {"id": "other"}), "002-1")


@pytest.mark.parametrize(("patient", "measure"), [(True, "a1c"), ("../../x", "a1c"), ("", "a1c"), ("002-1", "other"), ("002-1", [])])
def test_invalid_patient_measurement_arguments_do_not_dial(connected, patient, measure):
    assert mcp_tools.patient_measurements(patient=patient, measure=measure)["_error"]["code"] == "FHIR_BAD_REQUEST"
    assert connected.calls == []


@pytest.mark.parametrize(("code", "value", "unit"), [("2345-7", 105, "mg/dL"), ("2339-0", 90, "mg/dL"), ("41653-7", 5.2, "mmol/L")])
def test_patient_glucose_preserves_unit_and_date(patient_fixture, code, value, unit):
    item = observation("glucose", "002-1", code, "Glucose", "laboratory", 10, **quantity(value, unit))
    item["meta"] = copy.deepcopy(TAG)
    item["valueQuantity"].update(system="http://unitsofmeasure.org", code=unit)
    patient_fixture.resources["Observation"] = [item]
    result = mcp_tools.patient_measurements(patient="002-1", measure="glucose")["_data"]
    assert result["measurements"][0]["value"] == value and result["measurements"][0]["unit"] == unit


def test_patient_blood_pressure_uses_one_complete_panel(patient_fixture):
    old = pressure("bp-old", "002-1", 30, 120, 80, 95)
    incomplete = pressure("bp-new", "002-1", 10, 125, None, 90)
    old["meta"] = copy.deepcopy(TAG)
    for item in (old, incomplete):
        for component in item["component"]:
            if "valueQuantity" in component:
                component["valueQuantity"].update(system="http://unitsofmeasure.org", code="mm[Hg]")
    patient_fixture.resources["Observation"] = [incomplete, old]
    result = mcp_tools.patient_measurements(patient="002-1", measure="blood_pressure")["_data"]
    assert [row["value"] for row in result["measurements"]] == [120, 80]
    assert {row["source"] for row in result["measurements"]} == {"Observation/bp-old"}


@pytest.mark.parametrize("patch", [
    {"valueQuantity": None}, {"valueQuantity": {"value": True, "unit": "%"}},
    {"valueQuantity": {"value": float("nan"), "unit": "%"}}, {"valueQuantity": {"value": -1, "unit": "%"}},
    {"valueQuantity": {"value": 101, "unit": "%"}}, {"valueQuantity": {"value": 7, "unit": "mmol/mol"}},
    {"valueQuantity": {"value": 7, "unit": "%", "comparator": "<"}},
    {"valueQuantity": {"value": 7, "unit": "%", "system": "untrusted-unit-system"}},
    {"subject": {"reference": "Patient/other"}}, {"subject": "not-a-reference"},
    {"subject": {"reference": "http://other.example/fhir/Patient/002-1"}},
    {"effectiveDateTime": "bad"}, {"effectiveDateTime": "2026-10-05"}, {"status": "entered-in-error"},
    {"code": {"coding": [{"system": "other", "code": "4548-4"}]}},
    {"id": "bad/id"}, {"resourceType": "Patient"},
])
def test_patient_measurements_excludes_unusable_results(patient_fixture, patch, monkeypatch):
    item = copy.deepcopy(patient_fixture.resources["Observation"][0])
    patch = copy.deepcopy(patch)
    if isinstance(patch.get("valueQuantity"), dict):
        supplied = patch["valueQuantity"]
        patch["valueQuantity"] = {"system": "http://unitsofmeasure.org", "code": supplied.get("unit", "%")} | supplied
    item.update(patch)
    client = mcp_tools.connect()
    monkeypatch.setattr(client, "search", lambda *args: ([item], 1))
    monkeypatch.setattr(mcp_tools, "connect", lambda: client)
    output = mcp_tools.patient_measurements(patient="002-1")
    _, components = card_of(output)
    assert output["_data"]["measurements"] == []
    assert of_type(components, "alert")[0]["message"].startswith("No usable measurement")


def test_quantity_handles_overflow_and_standard_ucum_codes():
    assert reference.quantity({"valueQuantity": {"value": 10 ** 1000, "unit": "%"}}, ("%",)) is None
    assert reference.quantity({"valueQuantity": {"value": 6.1, "code": "%", "unit": "percent",
                                               "system": "http://unitsofmeasure.org"}}, ("%",)) == (6.1, "%")


@pytest.mark.parametrize("change", [
    {"value": float("nan")}, {"value": float("inf")}, {"value": -1}, {"value": True},
    {"value": 10 ** 1000}, {"comparator": "<"}, {"code": "mmol/mol"}, {"system": "other"},
    {"system": None}, {"code": None},
])
def test_quantity_requires_valid_ucum_values(change):
    item = {"value": 7, "code": "%", "system": "http://unitsofmeasure.org"} | change
    assert reference.quantity({"valueQuantity": item}, ("%",)) is None


def test_alternate_a1c_code_is_supported(patient_fixture):
    item = patient_fixture.resources["Observation"][1]
    item["code"]["coding"][0]["code"] = "17856-6"
    assert mcp_tools.patient_measurements(patient="002-1")["_data"]["measurements"][0]["source"] == "Observation/a1c-current"


def test_duplicate_blood_pressure_component_is_excluded(patient_fixture):
    item = pressure("bp", "002-1", 10, 120, 80, 90)
    for component in item["component"]:
        component["valueQuantity"].update(system="http://unitsofmeasure.org", code="mm[Hg]")
    item["component"].append(copy.deepcopy(item["component"][0]))
    patient_fixture.resources["Observation"] = [item]
    assert mcp_tools.patient_measurements(patient="002-1", measure="blood_pressure")["_data"]["measurements"] == []


@pytest.mark.parametrize(("subject", "expected"), [
    (f"{BASE}/Patient/002-1", True), ("Patient/002-1", True),
    ("https://eicu-fhir:8080/fhir/Patient/002-1", False), ("//other/Patient/002-1", False),
    ("Patient/002-1?other=true", False), ("Patient/002-1#fragment", False), ("http://[bad", False),
    (None, False), (f"{BASE}/Patient/002-1/_history/1", False),
])
def test_subject_reference_stays_on_the_configured_origin(connected, subject, expected):
    assert reference.subject_matches(mcp_tools.connect(), subject, "002-1") is expected


def test_patient_dates_do_not_invent_collection_time():
    assert reference.observation_date({"issued": at(5)}) == (datetime(2026, 10, 5, 15, 55, tzinfo=NOW.tzinfo), "issued")
    assert reference.observation_date({"effectivePeriod": {"start": at(30)}})[1] == "effectivePeriod.start"
    assert reference.observation_date({}) == (None, "")


def test_effective_period_and_issued_dates_are_supported(patient_fixture):
    item = patient_fixture.resources["Observation"][0]
    item.pop("effectiveDateTime")
    item["effectivePeriod"] = {"end": at(10)}
    patient_fixture.resources["Observation"] = [item]
    assert mcp_tools.patient_measurements(patient="002-1")["_data"]["measurements"][0]["measured_at"] == at(10)
    item["effectivePeriod"] = "invalid"
    item["issued"] = at(5)
    assert mcp_tools.patient_measurements(patient="002-1")["_data"]["measurements"][0]["measured_at"] == at(5)


@pytest.mark.parametrize(("patient_origin", "observation_origin", "expected", "synthetic"), [
    ("synthetic", "synthetic", "synthetic", True), ("public-deidentified", "public-deidentified", "public-deidentified", False),
    (None, None, "unverified", None), ("synthetic", None, "mixed", None),
])
def test_patient_provenance_never_invents_synthetic_origin(patient_fixture, patient_origin, observation_origin, expected, synthetic):
    for record, code in [(patient_fixture.resources["Patient"][0], patient_origin)] + [(item, observation_origin) for item in patient_fixture.resources["Observation"]]:
        record["meta"] = {"tag": [{"system": reference.SYNTHETIC_TAG_SYSTEM, "code": code}]} if code else {}
    data = mcp_tools.patient_measurements(patient="002-1")["_data"]
    assert data["synthetic"] is synthetic and data["data_origin"] == expected
    assert reference.provenance({"meta": "bad"}) == "unverified"


def test_patient_measurement_reports_limits(patient_fixture, monkeypatch):
    client = mcp_tools.connect()
    resources = patient_fixture.resources["Observation"][:1]
    monkeypatch.setattr(client, "search", lambda *args: (resources, 101))
    monkeypatch.setattr(mcp_tools, "connect", lambda: client)
    result = mcp_tools.patient_measurements(patient="002-1")
    assert result["_data"]["limited"] is True
    assert any("additional records may exist" in item["content"] for item in of_type(card_of(result)[1], "text"))


def test_patient_lookup_exposes_names_for_disambiguation(patient_fixture):
    output = mcp_tools.query_fhir_records(resource_type="Patient", filters={"name": "Jane"})
    assert output["_data"]["columns"][:2] == ["Patient", "Name"]
    assert output["_data"]["rows"][0][:2] == ["002-1", "Jane Demo"]
    assert reference.patient_label({"name": [{"text": "Jane Example"}]}) == "Jane Example"


def test_malformed_demographics_or_provenance_do_not_invent_labels():
    assert reference.patient_label({"id": "patient", "name": [{"given": "Jane", "text": {"untrusted": True}}]}) == "patient"
    assert reference.provenance({"meta": {"tag": "synthetic"}}) == "unverified"
    assert reference.provenance({"meta": {"tag": [{"system": reference.SYNTHETIC_TAG_SYSTEM, "code": {"bad": True}}]}}) == "unverified"


def test_population_query_transports_separate_filters_and_renders_provenance(connected, population_data, monkeypatch):
    population_data["scope"] = {"state": "KY", "counties": ["Wolfe", "Casey"], "pregnant": True}
    serve_population(monkeypatch, connected, population_data)
    result = mcp_tools.aggregate_a1c(state=" ky ", counties=[" Wolfe ", "Casey", "wolfe"], pregnant=True)
    card, components = card_of(result)
    assert card["title"] == "Population A1C" and card["id"] == "fhir-population-a1c"
    assert of_type(components, "hero")[0]["eyebrow"] == "Population-level SQL on FHIR"
    assert connected.calls[-1][2] == {"state": ["KY"], "county": ["Wolfe", "Casey"], "pregnant": ["true"]}
    assert result["_data"]["scope"] == population_data["scope"] and result["_data"]["synthetic"] is True
    assert of_type(components, "table")[0]["rows"] == [["Wolfe", "3", "2", "6.4"], ["Casey", "1", "0", "Not recorded"]]


def test_population_query_supports_empty_cohorts_and_not_pregnant_filter(connected, population_data, monkeypatch):
    population_data["rows"] = []
    population_data["scope"]["pregnant"] = False
    serve_population(monkeypatch, connected, population_data)
    result = mcp_tools.aggregate_a1c(pregnant=False)
    assert result["_data"]["rows"] == []
    assert connected.calls[-1][2]["pregnant"] == ["false"]
    assert "Explicitly not pregnant" in of_type(card_of(result)[1], "hero")[0]["subtitle"]


@pytest.mark.parametrize("arguments", [
    {"state": "Kentucky"}, {"state": True}, {"state": "K1"}, {"counties": "Wolfe"}, {"counties": ["Wolfe"] * 21},
    {"counties": [""]}, {"counties": ["x" * 81]}, {"counties": [1]}, {"counties": ["Wolfe\nCounty"]},
    {"pregnant": "true"}, {"pregnant": 1},
])
def test_population_invalid_arguments_do_not_dial(connected, arguments):
    assert mcp_tools.aggregate_a1c(**arguments)["_error"]["code"] == "FHIR_BAD_REQUEST"
    assert connected.calls == []


@pytest.mark.parametrize("payload", [
    {}, {"parameter": "bad"}, {"parameter": []}, {"parameter": [{"name": "result", "valueString": 1}]},
    {"parameter": [{"name": "result", "valueString": "["}]},
    {"parameter": [{"name": "result", "valueString": "x" * 128_001}]},
    {"parameter": [{"name": "result", "valueString": "{}"}] * 2},
])
def test_population_rejects_malformed_parameters(payload):
    with pytest.raises(FhirError) as error:
        reference.population_result(payload, EMPTY_SCOPE)
    assert error.value.code == "FHIR_INVALID_RESPONSE"


@pytest.mark.parametrize(("field", "value"), [
    ("query_id", "other"), ("synthetic", False), ("synthetic", 1), ("source", "untrusted"), ("definition", "wrong"),
    ("generated_at", "invalid"), ("generated_at", "2026-10-05T16:00:00"), ("scope", {}), ("rows", "bad"),
    ("rows", [{}] * 101),
])
def test_population_rejects_metadata_or_scope_mismatch(population_data, field, value):
    population_data[field] = value
    with pytest.raises(FhirError):
        reference.population_result(population_payload(population_data), EMPTY_SCOPE)


@pytest.mark.parametrize("row", [
    None, {}, {"county": ""}, {"county": "Wolfe\nCounty"},
    {"county": "Wolfe", "patient_count": True, "with_a1c_count": 0, "average_a1c": None},
    {"county": "Wolfe", "patient_count": 1, "with_a1c_count": True, "average_a1c": 1},
    {"county": "Wolfe", "patient_count": -1, "with_a1c_count": 0, "average_a1c": None},
    {"county": "Wolfe", "patient_count": 1, "with_a1c_count": 2, "average_a1c": 6},
    {"county": "Wolfe", "patient_count": 1_000_001, "with_a1c_count": 0, "average_a1c": None},
    {"county": "Wolfe", "patient_count": 1, "with_a1c_count": 0, "average_a1c": 0},
    {"county": "Wolfe", "patient_count": 1, "with_a1c_count": 1, "average_a1c": None},
    {"county": "Wolfe", "patient_count": 1, "with_a1c_count": 1, "average_a1c": True},
    {"county": "Wolfe", "patient_count": 1, "with_a1c_count": 1, "average_a1c": float("inf")},
    {"county": "Wolfe", "patient_count": 1, "with_a1c_count": 1, "average_a1c": -1},
    {"county": "Wolfe", "patient_count": 1, "with_a1c_count": 1, "average_a1c": 101},
])
def test_population_rejects_invalid_counts_and_means(population_data, row):
    population_data["rows"] = [row]
    with pytest.raises(FhirError):
        reference.population_result(population_payload(population_data), EMPTY_SCOPE)


def test_population_rejects_non_object_json_and_duplicate_counties(population_data):
    for data in [[], population_data | {"rows": [population_data["rows"][0]] * 2}]:
        with pytest.raises(FhirError):
            reference.population_result(population_payload(data), EMPTY_SCOPE)


def test_population_rejects_boolean_scope_coercion(population_data):
    population_data["scope"]["pregnant"] = 1
    with pytest.raises(FhirError):
        reference.population_result(population_payload(population_data), EMPTY_SCOPE | {"pregnant": True})


def test_population_requires_an_explicit_null_for_missing_a1c(population_data):
    population_data["rows"] = [{"county": "Casey", "patient_count": 1, "with_a1c_count": 0}]
    with pytest.raises(FhirError):
        reference.population_result(population_payload(population_data), EMPTY_SCOPE)


def test_population_drops_unrecognized_fields(connected, population_data, monkeypatch):
    population_data["patient_details"] = "never expose"
    population_data["rows"][0]["patient_details"] = "never expose"
    serve_population(monkeypatch, connected, population_data)
    output = mcp_tools.aggregate_a1c()
    assert "never expose" not in json.dumps(output)


@pytest.mark.parametrize(("failure", "code"), [
    (external_http.AuthFailedError("token"), "FHIR_AUTH_FAILED"),
    (external_http.EgressBlockedError("policy"), "FHIR_BLOCKED"),
    (external_http.ServiceUnreachableError("down"), "FHIR_UNAVAILABLE"),
])
@pytest.mark.parametrize("tool", ["patient_measurements", "aggregate_a1c"])
def test_new_tools_keep_transport_denials_and_failures(connected, failure, code, tool):
    connected.failures["Patient" if tool == "patient_measurements" else "$aggregate-a1c"] = failure
    arguments = {"patient": "002-1"} if tool == "patient_measurements" else {}
    response = MCPServer().process_request(MCPRequest(request_id="denied", method="tools/call", params={"name": tool, "arguments": arguments}))
    assert response.error["code"] == code and response.ui_components is None


def test_population_invalid_response_is_a_coded_mcp_failure(connected, monkeypatch):
    monkeypatch.setattr(connected, "get", lambda *args: population_payload({"invalid": True}))
    response = MCPServer().process_request(MCPRequest(request_id="bad", method="tools/call", params={"name": "aggregate_a1c"}))
    assert response.error["code"] == "FHIR_INVALID_RESPONSE"


@pytest.mark.asyncio
async def test_unsupported_subscriptions_report_a_terminal_failure(connected):
    connected.failures["SubscriptionTopic"] = external_http.BadRequestError("Upstream returned 404: unsupported")
    chunks = [chunk async for chunk in mcp_tools.watch_icu_activity({"minutes": 1}, {})]
    assert len(chunks) == 1 and chunks[0].terminal and chunks[0].components == []
    assert chunks[0].error["code"] == "FHIR_BAD_REQUEST" and "does not support" in chunks[0].error["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["patient_measurements", "aggregate_a1c"])
async def test_normal_dispatch_denies_new_tools_when_user_revokes_permission(monkeypatch, tool):
    from audit import recorder
    from orchestrator.tests.test_dispatch_local_agents_040 import _build_orch, _make_card, _make_tool_call, _pin_gates_off

    _pin_gates_off(monkeypatch)
    isolated_recorder = SimpleNamespace(record=AsyncMock())
    monkeypatch.setattr(recorder, "get_recorder", lambda: isolated_recorder)
    orchestrator = _build_orch()
    orchestrator.audit_recorder = isolated_recorder
    orchestrator.agent_cards = {"fhir-1": _make_card("fhir-1", [tool])}
    orchestrator.local_agents = {"fhir-1": MagicMock()}
    orchestrator.tool_permissions.is_tool_allowed.return_value = False
    response = await orchestrator.execute_single_tool(
        websocket=MagicMock(), tool_call=_make_tool_call(tool), tool_to_agent={tool: "fhir-1"}, chat_id="reference-chat", user_id="alice",
    )
    assert response.error is not None and "permission" in response.error["message"].lower()
    orchestrator._execute_with_retry.assert_not_awaited()
