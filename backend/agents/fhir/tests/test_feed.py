"""Exercises the live activity feed: subscription setup and cleanup, event polling, the card
it streams, and the first-chunk path used when the tool is called without streaming.
"""

from __future__ import annotations

import json

import pytest

from agents.fhir import mcp_tools
from agents.fhir.mcp_server import MCPServer
from agents.fhir.tests.conftest import TOPIC_BASE, at, encounter, observation, quantity
from shared import external_http
from shared.protocol import MCPRequest


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(mcp_tools, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(mcp_tools, "SECONDS_PER_MINUTE", 0.08)


def feed_events():
    admitted = encounter("icu-9", "009-9", "in-progress", "ACUTE", 1, unit="MICU")
    left = encounter("icu-8", "008-8", "completed", "ACUTE", 900, 2, unit="SICU")
    hospital = encounter("hosp-9", "009-9", "in-progress", "IMP", 60)
    return {
        "sub-1": [(admitted, True), (left, False), (hospital, True)],
        "sub-2": [
            (observation("o1", "009-9", "8867-4", "Heart rate", "vital-signs", 1, **quantity(82, "/min")), True),
            (observation("o2", "009-9", "8867-4", "Heart rate", "vital-signs", 0, **quantity(151, "/min")), True),
            (observation("o3", "008-8", "2524-7", "Lactate", "laboratory", 0, **quantity(6.2, "mmol/L")), True),
        ],
    }


async def test_feed_streams_counts_events_and_cleans_up(connected, fast):
    stream = mcp_tools.watch_icu_activity({"minutes": 1}, {})
    first = await stream.__anext__()
    assert first.terminal is False and first.raw["watching"] is True and first.raw["events"] == 0
    assert first.components[0]["type"] == "card" and first.components[0]["title"] == "ICU activity feed"
    assert "id" not in first.components[0]
    created = [call for call in connected.calls if call[0] == "POST"]
    assert len(created) == 2 and set(connected.subscriptions) == {"sub-1", "sub-2"}
    connected.subscriptions.update(feed_events())
    chunks = [chunk async for chunk in stream]
    final = chunks[-1]
    assert final.terminal is True and final.raw["watching"] is False
    assert {key: final.raw[key] for key in ("events", "admissions", "discharges", "observations", "flagged")} == {
        "events": 6, "admissions": 1, "discharges": 1, "observations": 3, "flagged": 2,
    }
    titles = [event["event"] for event in final.raw["notable_events"]]
    assert titles == [
        "Flagged: Lactate 6.2 mmol/L for patient 008-8", "Flagged: Heart rate 151 /min for patient 009-9",
        "Left SICU: patient 008-8", "Admitted to MICU: patient 009-9",
    ]
    card = final.components[0]
    hero, stats, timeline, table = card["content"][0], card["content"][1], card["content"][2], card["content"][3]
    assert hero["subtitle"].startswith("Finished") and hero["badges"][0] == "6 notifications"
    assert stats["items"][3] == {"label": "Flagged readings", "value": "2", "variant": "error"}
    assert timeline["items"][0]["variant"] == "error" and timeline["items"][3]["description"] == "Sepsis, pulmonary"
    assert table["rows"][0][1:] == ["008-8", "Lactate", "6.2 mmol/L"] and len(table["rows"]) == 3
    assert all(chunk.raw["watching"] for chunk in chunks[:-1])
    assert connected.deleted == ["sub-1", "sub-2"]
    assert len(json.dumps(card)) < 65536


async def test_feed_reads_long_backlogs_in_batches(connected, fast, monkeypatch):
    monkeypatch.setattr(mcp_tools, "EVENT_BATCH", 2)
    connected.event_batch = 2
    state = mcp_tools.FeedState()
    client = mcp_tools.connect()
    connected.subscriptions["sub-x"] = feed_events()["sub-2"]
    assert mcp_tools.drain(client, "sub-x", 0, state) == 3
    assert state.counters["observations"] == 3
    assert mcp_tools.drain(client, "sub-x", 3, state) == 3
    polls = [call for call in connected.calls if call[1].endswith("$events")]
    assert [call[2]["eventsSinceNumber"] for call in polls] == [["1"], ["3"], ["4"]]


async def test_feed_reports_a_failure_before_it_starts(connected, fast):
    connected.failures["SubscriptionTopic"] = external_http.ServiceUnreachableError("down")
    chunks = [chunk async for chunk in mcp_tools.watch_icu_activity({}, {})]
    assert len(chunks) == 1 and chunks[0].terminal is True and chunks[0].components == []
    assert chunks[0].error == {"code": "FHIR_UNAVAILABLE", "message": "The FHIR endpoint could not be reached", "phase": "failed", "retryable": True}
    assert connected.deleted == []


async def test_feed_needs_the_subscription_topics(connected, fast, monkeypatch):
    monkeypatch.setattr(connected, "get", lambda path, query: {"resourceType": "Bundle", "entry": []} if path == "SubscriptionTopic" else {})
    chunks = [chunk async for chunk in mcp_tools.watch_icu_activity({"minutes": "x"}, {})]
    assert chunks[0].error["code"] == "FHIR_BAD_REQUEST"


async def test_feed_stops_cleanly_when_polling_fails(connected, fast):
    stream = mcp_tools.watch_icu_activity({"minutes": 5}, {})
    await stream.__anext__()
    connected.failures["$events"] = external_http.ServiceUnreachableError("down")
    chunks = [chunk async for chunk in stream]
    assert len(chunks) == 1 and chunks[0].terminal is True and chunks[0].raw["watching"] is False
    assert connected.deleted == ["sub-1", "sub-2"]


async def test_feed_closes_its_subscriptions_when_abandoned(connected, fast):
    stream = mcp_tools.watch_icu_activity({"minutes": 5}, {})
    await stream.__anext__()
    connected.failures["Subscription/sub-1"] = external_http.ServiceUnreachableError("down")
    await stream.aclose()
    assert connected.deleted == ["sub-2"]


def test_feed_state_ignores_what_it_cannot_classify():
    state = mcp_tools.FeedState()
    state.absorb({"resourceType": "Encounter", "class": [{"coding": [{"code": "IMP"}]}], "subject": {"reference": "Patient/p"}}, True)
    state.absorb({"resourceType": "Encounter", "class": [{"coding": [{"code": "ACUTE"}]}], "subject": {"reference": "Patient/p"}}, False)
    state.absorb({"resourceType": "Condition"}, True)
    assert state.counters == {"events": 3, "admissions": 0, "discharges": 0, "observations": 0, "flagged": 0}
    waiting = state.card(None, True)
    assert waiting["content"][2]["message"] == "Waiting for the next notifications from the feed."


def test_server_returns_the_first_chunk_when_called_without_streaming(connected):
    response = MCPServer().process_request(MCPRequest(request_id="r1", method="tools/call", params={
        "name": "watch_icu_activity", "arguments": {"minutes": 3, "_runtime": object(), "session_id": "s"},
    }))
    assert response.error is None and response.result["watching"] is True
    assert response.ui_components[0]["title"] == "ICU activity feed" and "id" not in response.ui_components[0]
    assert connected.deleted == ["sub-1", "sub-2"]
    topics = [call[2] for call in connected.calls if call[1] == "SubscriptionTopic"]
    assert topics == [{}] and TOPIC_BASE


def test_server_maps_a_failed_first_chunk_to_a_coded_error(connected):
    connected.failures["SubscriptionTopic"] = external_http.AuthFailedError("401")
    response = MCPServer().process_request(MCPRequest(request_id="r2", method="tools/call", params={"name": "watch_icu_activity"}))
    assert response.error == {"code": "FHIR_AUTH_FAILED", "message": "The FHIR endpoint rejected the access token", "retryable": False}
    assert response.ui_components is None


def test_server_handles_a_stream_that_yields_nothing(connected, monkeypatch):
    async def silent(args, credentials):
        return
        yield

    monkeypatch.setitem(mcp_tools.TOOL_REGISTRY["watch_icu_activity"], "function", silent)
    response = MCPServer().process_request(MCPRequest(request_id="r3", method="tools/call", params={"name": "watch_icu_activity"}))
    assert response.result is None and response.ui_components == []
    assert at(0)


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(mcp_tools, "LIVE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(mcp_tools, "SECONDS_PER_MINUTE", 0.08)


async def test_vitals_stream_updates_one_card_until_it_ends(connected, live):
    stream = mcp_tools.stream_patient_vitals({"patient": "Patient/002-1", "minutes": 1}, {})
    first = await stream.__anext__()
    card = first.components[0]
    assert first.terminal is False and card["title"] == "Live vitals: patient 002-1" and "id" not in card
    assert first.raw["watching"] is True and first.raw["new_readings"] == 0
    assert first.raw["latest_vitals"]["heart_rate"]["value"] == 148.0 and "presentation" in first.raw
    hero = card["content"][0]
    assert hero["subtitle"] == "Streaming for 1 min · as of Oct 5, 16:00"
    assert hero["badges"] == ["0 new readings since the stream began", "Updates as readings arrive"]
    assert [part["type"] for part in card["content"]] == ["hero", "grid", "plotly_chart", "plotly_chart", "action_group", "text"]
    assert [chart["title"] for chart in card["content"][2:4]] == ["Heart rate · Respiratory rate · SpO2", "Blood pressure"]
    assert [button["label"] for button in card["content"][-2]["buttons"]] == ["Patient overview"]
    connected.resources["Observation"].append(
        observation("hr-new", "002-1", "8867-4", "Heart rate", "vital-signs", -1, **quantity(101, "/min")))
    chunks = [chunk async for chunk in stream]
    final = chunks[-1]
    assert final.terminal is True and final.raw["watching"] is False and final.raw["new_readings"] == 1
    assert final.raw["latest_vitals"]["heart_rate"]["value"] == 101.0
    ended = final.components[0]["content"]
    assert ended[0]["subtitle"].startswith("Stream ended") and ended[0]["badges"][0] == "1 new reading since the stream began"
    again = ended[-2]["buttons"][0]
    assert (again["label"], again["action"]) == ("Stream again", "stream_subscribe")
    assert again["payload"] == {"tool_name": "stream_patient_vitals", "params": {"patient": "002-1", "minutes": 1}}
    assert chunks[0].components == chunks[1].components or len(chunks) < 3
    assert json.dumps(final.components, allow_nan=False) and final.serialized_size() < 65536


async def test_vitals_stream_shows_latest_known_values_outside_the_chart_window(connected, live):
    connected.resources["Observation"] = [item for item in connected.resources["Observation"] if item["id"] in ("hr-9",)]
    connected.resources["Observation"][0]["subject"] = {"reference": "Patient/002-1"}
    stream = mcp_tools.stream_patient_vitals({"patient": "002-1"}, {})
    first = await stream.__anext__()
    await stream.aclose()
    card = first.components[0]
    assert first.raw["latest_vitals"]["heart_rate"]["value"] == 70.0
    assert [part["type"] for part in card["content"]] == ["hero", "grid", "action_group", "text"]
    assert card["content"][0]["subtitle"].startswith("Streaming for 5 min")


async def test_vitals_stream_reports_bad_patients_and_outages(connected, live):
    refused = [chunk async for chunk in mcp_tools.stream_patient_vitals({"patient": "not a patient!"}, {})]
    assert len(refused) == 1 and refused[0].terminal is True and refused[0].error["code"] == "FHIR_BAD_REQUEST"
    missing = [chunk async for chunk in mcp_tools.stream_patient_vitals({"patient": "999-9"}, {})]
    assert missing[0].error["code"] == "FHIR_NOT_FOUND" and missing[0].components == []
    stream = mcp_tools.stream_patient_vitals({"patient": "002-1", "minutes": 1}, {})
    first = await stream.__anext__()
    connected.failures["Observation"] = external_http.ServiceUnreachableError("down")
    chunks = [chunk async for chunk in stream]
    assert len(chunks) == 1 and chunks[0].terminal is True
    assert chunks[0].raw["watching"] is False and chunks[0].components == first.components


def test_vitals_stream_first_chunk_serves_the_non_streaming_path(connected):
    response = MCPServer().process_request(MCPRequest(
        request_id="live", method="tools/call", params={"name": "stream_patient_vitals", "arguments": {"patient": "002-1", "_runtime": object()}}))
    assert response.error is None and response.result["patient"] == "002-1" and response.result["watching"] is True
    assert response.ui_components[0]["title"] == "Live vitals: patient 002-1"
    refused = MCPServer().process_request(MCPRequest(
        request_id="live", method="tools/call", params={"name": "stream_patient_vitals", "arguments": {"patient": ""}}))
    assert refused.error["code"] == "FHIR_BAD_REQUEST"
