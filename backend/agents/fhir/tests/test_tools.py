"""Exercises every request/response tool against the in-memory FHIR server: the summary
returned to the model, the single card returned to the canvas, and coded failures.
"""

from __future__ import annotations

import json
import math

import pytest

from agents.fhir import mcp_tools
from agents.fhir.client import RESOURCE_TYPES, FhirError
from agents.fhir.tests.conftest import NOW
from shared import external_http

ALLOWED_TYPES = {
    "card", "hero", "stat_group", "plotly_chart", "table", "action_group", "button", "text", "grid", "metric", "keyvalue",
    "gauge", "alert", "timeline",
}


def walk(component):
    yield component
    for key in ("content", "children", "buttons"):
        nested = component.get(key)
        for child in nested if isinstance(nested, list) else []:
            yield from walk(child)


def card_of(result):
    assert set(result) == {"_ui_components", "_data"}
    assert len(result["_ui_components"]) == 1
    card = result["_ui_components"][0]
    assert card["type"] == "card" and card["id"].startswith("fhir-")
    components = list(walk(card))
    assert {component["type"] for component in components} <= ALLOWED_TYPES
    assert not any(component.get("variant") == "error" and component["type"] == "alert" for component in card["content"])
    encoded = json.dumps(result, allow_nan=False)
    assert "NaN" not in encoded and len(encoded) < 400_000
    for component in components:
        for value in component.values():
            assert not (isinstance(value, float) and math.isnan(value))
    return card, components


def of_type(components, kind):
    return [component for component in components if component["type"] == kind]


def test_census_lists_active_stays_with_latest_vitals(connected):
    result = mcp_tools.icu_census(limit=5, session_id="s", user_id="u")
    card, components = card_of(result)
    data = result["_data"]
    assert data["as_of"] == NOW.isoformat(timespec="seconds")
    assert data["patients_in_icu"] == 1 and data["admitted_last_hour"] == 1 and data["by_unit_type"] == {"Med-Surg ICU": 1}
    row = data["most_recently_admitted"][0]
    assert (row["patient"], row["heart_rate"], row["oxygen_saturation"], row["mean_arterial_pressure"]) == ("002-1", "148", "91", "62")
    assert (row["flag"], row["stay"], row["reason"], row["admitted"]) == ("Critical", "30 min", "Sepsis, pulmonary", "Oct 5, 15:30")
    assert card["id"] == "fhir-icu-census"
    hero = of_type(components, "hero")[0]
    assert hero["title"] == "1 patients in intensive care" and "1 of 1 shown flagged critical" in hero["badges"]
    table = of_type(components, "table")[0]
    assert table["title"] == "Most recently admitted"
    assert table["headers"] == ["Patient", "Vitals", "HR", "SpO2", "MAP", "Unit", "In unit", "Admission diagnosis"]
    assert table["rows"] == [["002-1", "Critical", "148", "91", "62", "Med-Surg ICU", "30 min", "Sepsis, pulmonary"]]
    chart = of_type(components, "plotly_chart")[0]
    assert chart["data"][0]["type"] == "bar" and chart["data"][0]["x"] == ["2026-10-05 15:00"] and chart["data"][0]["y"] == [1]
    buttons = of_type(components, "button")
    assert [(button["label"], button["action"], button["payload"]) for button in buttons] == [
        ("Refresh census", "component_action", {"kind": "refresh"}),
        ("Open patient 002-1", "chat_message", {"message": "Show the FHIR patient overview for patient 002-1"}),
    ]
    assert "do not output UI components" in data["presentation"]


def test_cards_offer_live_streams_only_when_the_platform_can_show_them(connected, monkeypatch):
    def labels(result):
        return [button["label"] for button in of_type(card_of(result)[1], "button")]

    assert mcp_tools.live_available() is False
    assert "Watch live feed" not in labels(mcp_tools.icu_census())
    assert "Stream live vitals" not in labels(mcp_tools.patient_overview(patient="002-1"))
    assert "Stream live" not in labels(mcp_tools.vital_sign_trends(patient="002-1"))
    monkeypatch.setitem(mcp_tools.flags._flags, "tool_streaming", True)
    assert mcp_tools.live_available() is False
    monkeypatch.setitem(mcp_tools.flags._flags, "stream_progress", True)
    assert mcp_tools.live_available() is True
    census = {button["label"]: button for button in of_type(card_of(mcp_tools.icu_census())[1], "button")}
    assert census["Watch live feed"]["action"] == "stream_subscribe"
    assert census["Watch live feed"]["payload"] == {"tool_name": "watch_icu_activity", "params": {"minutes": 5}}
    overview = {button["label"]: button for button in of_type(card_of(mcp_tools.patient_overview(patient="002-1"))[1], "button")}
    assert list(overview) == ["Vital sign trends", "Stream live vitals", "Laboratory results", "Medications", "Timeline"]
    assert overview["Stream live vitals"]["payload"] == {
        "tool_name": "stream_patient_vitals", "params": {"patient": "002-1", "minutes": 5}}
    trends = {button["label"]: button for button in of_type(card_of(mcp_tools.vital_sign_trends(patient="002-1"))[1], "button")}
    assert list(trends) == ["Refresh", "Stream live", "Patient overview"]
    assert trends["Refresh"]["action"] == "component_action" and trends["Stream live"]["action"] == "stream_subscribe"
    monkeypatch.setattr(mcp_tools.flags, "is_enabled", lambda name: 1 / 0)
    assert mcp_tools.live_available() is False


def test_census_filters_by_unit_and_handles_an_empty_unit(connected):
    assert mcp_tools.icu_census(unit="med-surg")["_data"]["patients_in_icu"] == 1
    empty = mcp_tools.icu_census(unit="neuro", limit="many")
    card, components = card_of(empty)
    assert empty["_data"]["patients_in_icu"] == 0 and empty["_data"]["most_recently_admitted"] == []
    assert of_type(components, "table")[0]["rows"] == []
    assert len(of_type(components, "button")) == 1


def test_census_without_vitals_or_start_times(connected):
    connected.resources["Observation"] = []
    connected.resources["Encounter"][0]["actualPeriod"] = {}
    connected.resources["Encounter"][0]["reason"][0]["value"][0]["concept"]["text"] = (
        "Cardiac arrest (with or without respiratory arrest; for respiratory arrest see Respiratory System)")
    result = mcp_tools.icu_census()
    row = result["_data"]["most_recently_admitted"][0]
    assert (row["heart_rate"], row["flag"], row["stay"], row["admitted"]) == ("—", "", "", "unknown")
    assert row["reason"] == "Cardiac arrest (with or without…"
    _, components = card_of(result)
    assert of_type(components, "table")[0]["rows"] == [
        ["002-1", "—", "—", "—", "—", "Med-Surg ICU", "—", "Cardiac arrest (with or without…"]]
    connected.resources["Encounter"][0].pop("reason")
    connected.resources["Encounter"][0].pop("type")
    _, components = card_of(mcp_tools.icu_census())
    assert of_type(components, "table")[0]["rows"][0][5:] == ["—", "—", "—"]


def test_patient_overview_assembles_the_whole_picture(connected):
    result = mcp_tools.patient_overview(patient="Patient/002-1")
    card, components = card_of(result)
    data = result["_data"]
    assert card["id"] == "fhir-patient-002-1"
    assert (data["age"], data["sex"], data["in_icu_now"], data["unit"], data["vitals_flag"]) == (
        "67-year-old", "female", True, "Med-Surg ICU", "Critical")
    assert data["latest_vitals"]["heart_rate"] == {"value": 148.0, "unit": "/min", "time": "2026-10-05T15:55:00-04:00"}
    assert data["predicted_risk"] == {"ICU mortality": 0.31, "Hospital mortality": 0.42}
    assert data["problems"] == ["Acute respiratory failure", "Hypertension"]
    assert data["allergies"] == ["Penicillins"] and data["active_medications"] == ["NOREPINEPHRINE"]
    assert [lab["test"] for lab in data["key_labs"]] == ["Lactate", "Potassium", "Troponin I"]
    hero = of_type(components, "hero")[0]
    assert hero["subtitle"] == "67-year-old female · admitted Oct 5, 15:30"
    assert hero["badges"] == ["In ICU now", "Vitals: Critical", "Sepsis, pulmonary"]
    tiles = {tile["title"]: tile for tile in of_type(components, "metric")}
    assert (tiles["Heart rate"]["value"], tiles["Heart rate"]["variant"], tiles["Heart rate"]["subtitle"]) == ("148 /min", "error", "5 min ago")
    assert (tiles["SpO2"]["variant"], tiles["Mean arterial pressure"]["variant"], tiles["Respiratory rate"]["variant"]) == (
        "warning", "warning", "default")
    facts = {item["label"]: item["value"] for item in of_type(components, "keyvalue")[0]["items"]}
    assert facts == {
        "Age": "67 years", "Sex": "female", "Height": "165 cm", "Weight": "70 kg", "Unit": "Med-Surg ICU",
        "Hospital": "eICU Hospital 10", "Admitted": "Oct 5, 15:30", "Time in unit": "30 min", "Admitted from": "Emergency Department",
    }
    gauges = of_type(components, "gauge")
    assert [(gauge["label"], gauge["display_value"]) for gauge in gauges] == [("ICU mortality", "31.0%"), ("Hospital mortality", "42.0%")]
    assert of_type(components, "alert")[0]["message"] == "Penicillins"
    tables = {table["title"]: table for table in of_type(components, "table")}
    assert list(tables) == ["Problems", "Key laboratory results", "Active orders"]
    assert tables["Problems"]["rows"][0] == ["Acute respiratory failure", "Active", "Oct 5, 15:35"]
    assert tables["Key laboratory results"]["headers"] == ["Test", "Result", "Reference", "Flag", "Collected"]
    assert tables["Key laboratory results"]["rows"][0] == ["Lactate", "5.1 mmol/L", "0.5–2", "Critical", "Oct 5, 15:05"]
    assert len(of_type(components, "button")) == 4


def test_patient_overview_for_a_deceased_patient_without_recent_data(connected):
    result = mcp_tools.patient_overview(patient="002-2")
    card, components = card_of(result)
    data = result["_data"]
    assert data["in_icu_now"] is False and data["deceased"] and data["unit"] == "Cardiac ICU" and data["predicted_risk"] == {}
    assert of_type(components, "hero")[0]["badges"][0] == "Deceased"
    assert of_type(components, "metric") != [] and of_type(components, "alert") == []
    facts = {item["label"]: item["value"] for item in of_type(components, "keyvalue")[0]["items"]}
    assert facts["Age"] == "not recorded" and facts["Discharged to"] == "Floor" and "Died" in facts
    assert of_type(components, "gauge") == [] and [table["title"] for table in of_type(components, "table")] == []


def test_patient_overview_without_any_encounter(connected):
    connected.resources["Encounter"] = []
    connected.resources["Observation"] = []
    connected.resources["Patient"][0]["managingOrganization"] = {"reference": "Organization/hospital-10"}
    result = mcp_tools.patient_overview(patient="002-1")
    data = result["_data"]
    assert (data["unit"], data["admitted"], data["vitals_flag"], data["in_icu_now"]) == ("", None, None, False)
    _, components = card_of(result)
    facts = {item["label"]: item["value"] for item in of_type(components, "keyvalue")[0]["items"]}
    assert facts["Hospital"] == "hospital-10" and facts["Unit"] == "not recorded"
    assert of_type(components, "metric") == []
    assert of_type(components, "alert")[0]["message"] == "No vital signs have been charted for this patient yet."


@pytest.mark.parametrize("patient", ["", "  ", "not a valid id!", "x" * 80, None])
def test_patient_tools_reject_unusable_identifiers(connected, patient):
    for tool in (mcp_tools.patient_overview, mcp_tools.vital_sign_trends, mcp_tools.laboratory_results,
                 mcp_tools.medication_review, mcp_tools.patient_timeline):
        assert tool(patient=patient) == {"_error": {
            "code": "FHIR_BAD_REQUEST", "message": "A patient identifier such as 002-10145 is required", "retryable": False,
        }}
    assert connected.calls == []


def test_unknown_patient_is_reported_as_not_found(connected):
    assert mcp_tools.patient_overview(patient="999-9")["_error"]["code"] == "FHIR_NOT_FOUND"


def test_vital_sign_trends_chart_each_measure(connected):
    result = mcp_tools.vital_sign_trends(patient="002-1", hours=6)
    card, components = card_of(result)
    data = result["_data"]
    assert card["id"] == "fhir-vitals-002-1" and data["window_hours"] == 6
    assert data["vitals"]["heart_rate"] == {
        "latest": 148.0, "minimum": 88.0, "maximum": 148.0, "mean": 110.67, "readings": 3, "unit": "/min",
        "latest_time": "2026-10-05T15:55:00-04:00",
    }
    assert set(data["vitals"]) == {
        "heart_rate", "oxygen_saturation", "respiratory_rate", "systolic", "diastolic", "mean_arterial_pressure",
        "temperature", "glasgow_coma_score",
    }
    charts = {chart["title"]: chart for chart in of_type(components, "plotly_chart")}
    assert list(charts) == ["Heart rate · Respiratory rate · SpO2", "Blood pressure", "Temperature"]
    cardiac = charts["Heart rate · Respiratory rate · SpO2"]
    assert [trace["name"] for trace in cardiac["data"]] == ["Heart rate", "Respiratory rate", "SpO2"]
    assert cardiac["data"][0]["x"] == ["2026-10-05 13:55", "2026-10-05 14:55", "2026-10-05 15:55"]
    assert cardiac["data"][0]["mode"] == "lines+markers" and cardiac["data"][2]["yaxis"] == "y2"
    assert cardiac["layout"]["xaxis"]["type"] == "date" and cardiac["layout"]["yaxis"]["gridcolor"]
    assert cardiac["layout"]["yaxis2"]["range"] == [85.0, 101] and cardiac["layout"]["yaxis2"]["automargin"] is True
    assert "shapes" not in cardiac["layout"]
    pressure = charts["Blood pressure"]
    assert [trace["name"] for trace in pressure["data"]] == ["Systolic BP", "Mean arterial pressure", "Diastolic BP"]
    assert len({trace["line"]["color"] for trace in pressure["data"]}) == 3
    assert pressure["layout"]["shapes"][0]["y0"] == 65 and pressure["layout"]["annotations"][0]["text"] == "MAP 65"
    band = charts["Temperature"]["layout"]["shapes"][0]
    assert (band["y0"], band["y1"]) == (36.0, 38.3) and charts["Temperature"]["data"][0]["y"] == [37.9, 38.6]
    stats = {item["label"]: item for item in of_type(components, "stat_group")[0]["items"]}
    assert stats["Heart rate"] == {"label": "Heart rate", "value": "148 /min", "hint": "Range 88–148 · 3 readings", "variant": "error"}
    assert stats["Glasgow coma score"]["hint"] == "1 reading"
    code_filter = connected.calls[-1][2]["code"][0]
    assert "http://loinc.org|8867-4" in code_filter and connected.calls[-1][2]["date"] == ["ge2026-10-05T10:00:00-04:00"]
    assert connected.calls[-1][2]["_sort"] == ["-date"] and data["complete"] is True
    assert not any("most recent" in text["content"] for text in of_type(components, "text"))


def test_vital_sign_trends_say_when_a_long_window_is_cut_short(connected, monkeypatch):
    monkeypatch.setattr(mcp_tools, "MAX_TREND_READINGS", 4)
    result = mcp_tools.vital_sign_trends(patient="002-1", hours=6)
    _, components = card_of(result)
    assert result["_data"]["complete"] is False
    assert "Showing the most recent 4 of 13 readings." in [text["content"] for text in of_type(components, "text")]


def test_vital_sign_trends_with_no_readings(connected):
    result = mcp_tools.vital_sign_trends(patient="002-2", hours=1)
    _, components = card_of(result)
    assert result["_data"]["vitals"] == {} and of_type(components, "plotly_chart") == []
    assert of_type(components, "stat_group") == []
    assert of_type(components, "alert")[0]["message"] == "No vital signs were recorded for this patient in the last 1 hours."
    assert mcp_tools.vital_sign_trends(patient="002-1", hours=99999)["_data"]["window_hours"] == 168


def test_vital_sign_trends_need_two_readings_to_draw_a_line(connected):
    kept = {"hr-1", "sp-1", "sp-2"}
    connected.resources["Observation"] = [item for item in connected.resources["Observation"] if item["id"] in kept]
    result = mcp_tools.vital_sign_trends(patient="002-1", hours=6)
    _, components = card_of(result)
    chart = of_type(components, "plotly_chart")[0]
    assert chart["title"] == "SpO2" and [trace["name"] for trace in chart["data"]] == ["SpO2"]
    assert "yaxis" not in chart["data"][0] and "yaxis2" not in chart["layout"]
    assert chart["layout"]["yaxis"]["title"] == "SpO2 %" and chart["layout"]["yaxis"]["range"] == [85.0, 101]
    assert len(of_type(components, "plotly_chart")) == 1 and of_type(components, "alert") == []
    stats = {item["label"]: item["hint"] for item in of_type(components, "stat_group")[0]["items"]}
    assert stats == {"Heart rate": "1 reading", "SpO2": "Range 91–97 · 2 readings"}
    connected.resources["Observation"] = [item for item in connected.resources["Observation"] if item["id"] == "hr-1"]
    _, components = card_of(mcp_tools.vital_sign_trends(patient="002-1", hours=6))
    assert of_type(components, "plotly_chart") == []
    assert of_type(components, "alert")[0]["message"] == "Too few readings in the last 6 hours to draw a trend."


def test_vital_sign_trends_describe_steady_values_and_low_saturation(connected):
    steady = [item for item in connected.resources["Observation"] if item["id"] in ("hr-1", "hr-2", "sp-1", "sp-2")]
    for item in steady:
        item["valueQuantity"]["value"] = 72 if item["id"].startswith("hr") else 78
    connected.resources["Observation"] = steady
    result = mcp_tools.vital_sign_trends(patient="002-1", hours=6)
    _, components = card_of(result)
    stats = {item["label"]: item["hint"] for item in of_type(components, "stat_group")[0]["items"]}
    assert stats == {"Heart rate": "Steady at 72 · 2 readings", "SpO2": "Steady at 78 · 2 readings"}
    chart = of_type(components, "plotly_chart")[0]
    assert chart["title"] == "Heart rate · SpO2" and chart["layout"]["yaxis2"]["range"] == [76.0, 101]
    assert chart["layout"]["yaxis"]["title"] == "per minute"
    connected.resources["Observation"] = [item for item in steady if item["id"].startswith("hr")]
    _, components = card_of(mcp_tools.vital_sign_trends(patient="002-1", hours=6))
    chart = of_type(components, "plotly_chart")[0]
    assert chart["title"] == "Heart rate" and "yaxis2" not in chart["layout"] and chart["layout"]["yaxis"]["title"] == "per minute"


def test_laboratory_results_table_and_history_charts(connected):
    result = mcp_tools.laboratory_results(patient="002-1", hours=24)
    card, components = card_of(result)
    data = result["_data"]
    assert card["id"] == "fhir-labs-002-1" and data["tests"] == 4
    assert data["results"][0] == {
        "test": "Lactate", "value": "5.1", "unit": "mmol/L", "flag": "Critical", "reference": "0.5–2",
        "collected": "2026-10-05T15:05:00-04:00",
    }
    table = of_type(components, "table")[0]
    assert table["headers"] == ["Test", "Result", "Reference", "Flag", "Change", "Collected"]
    assert table["rows"][1] == ["Potassium", "3.2 mmol/L", "3.5–5", "Moderate", "↓ 0.9", "Oct 5, 15:00"]
    charts = of_type(components, "plotly_chart")
    assert [chart["title"] for chart in charts] == ["Potassium (mmol/L)"]
    assert charts[0]["data"][0]["y"] == [4.1, 3.2] and charts[0]["layout"]["shapes"][0]["y1"] == 5.0
    assert charts[0]["data"][0]["mode"] == "lines+markers"
    assert of_type(components, "hero")[0]["badges"] == ["4 tests", "2 outside reference"]
    assert connected.calls[-1][2]["category"] == ["laboratory"]


def test_laboratory_results_when_nothing_was_reported(connected):
    result = mcp_tools.laboratory_results(patient="002-2")
    _, components = card_of(result)
    assert result["_data"]["results"] == [] and of_type(components, "table") == []
    assert "No laboratory results" in of_type(components, "alert")[0]["message"]


def test_medication_review_groups_orders_infusions_and_home_medication(connected):
    result = mcp_tools.medication_review(patient="002-1")
    card, components = card_of(result)
    data = result["_data"]
    assert card["id"] == "fhir-medications-002-1"
    assert data["order_counts"] == {"active": 1, "completed": 1, "cancelled": 1} and data["active_orders"] == ["NOREPINEPHRINE"]
    assert data["infusions"] == {"Norepinephrine": {"readings": 2, "latest_rate": 12.0, "unit": "mcg/min"}}
    assert data["home_medications"] == ["LISINOPRIL"]
    tables = {table["title"]: table for table in of_type(components, "table")}
    assert tables["Orders"]["rows"][0] == ["NOREPINEPHRINE", "4 mg IV Continuous", "Active", "Oct 5, 15:40"]
    assert tables["Before admission"]["rows"] == [["LISINOPRIL", "10 mg daily"]]
    chart = of_type(components, "plotly_chart")[0]
    assert chart["title"] == "Charted infusion rates (mcg/min)"
    assert chart["data"][0]["name"] == "Norepinephrine" and chart["data"][0]["line"]["shape"] == "hv"
    assert chart["data"][0]["y"] == [8.0, 12.0]
    assert chart["layout"]["showlegend"] is True and chart["layout"]["yaxis"]["title"] == "mcg/min"
    assert chart["data"][0]["x"] == ["2026-10-05 15:42", "2026-10-05 15:52"]
    administrations = next(call for call in connected.calls if call[1] == "MedicationAdministration")
    assert administrations[2]["_sort"] == ["-date"]
    assert not any("most recent" in text["content"] for text in of_type(components, "text"))


def test_medication_review_orders_infusion_points_and_notes_truncation(connected, monkeypatch):
    connected.resources["MedicationAdministration"].reverse()
    monkeypatch.setattr(mcp_tools, "MAX_ADMINISTRATIONS", 2)
    result = mcp_tools.medication_review(patient="002-1")
    _, components = card_of(result)
    chart = of_type(components, "plotly_chart")[0]
    assert chart["data"][0]["x"] == ["2026-10-05 15:52"] and result["_data"]["infusions"]["Norepinephrine"]["latest_rate"] == 12.0
    assert "Showing the most recent 2 of 3 charted doses." in [text["content"] for text in of_type(components, "text")]
    monkeypatch.setattr(mcp_tools, "MAX_ADMINISTRATIONS", 3000)
    result = mcp_tools.medication_review(patient="002-1")
    assert result["_data"]["infusions"]["Norepinephrine"] == {"readings": 2, "latest_rate": 12.0, "unit": "mcg/min"}
    assert of_type(card_of(result)[1], "plotly_chart")[0]["data"][0]["y"] == [8.0, 12.0]


def test_medication_review_charts_infusions_in_different_units(connected):
    connected.resources["MedicationAdministration"].append({
        "resourceType": "MedicationAdministration", "id": "ma-4", "status": "completed",
        "subject": {"reference": "Patient/002-1"}, "medication": {"concept": {"text": "Propofol"}},
        "occurenceDateTime": "2026-10-05T15:50:00-04:00", "dosage": {"rateQuantity": {"value": 20, "unit": "mL/h"}},
    })
    _, components = card_of(mcp_tools.medication_review(patient="002-1"))
    chart = of_type(components, "plotly_chart")[0]
    assert [trace["name"] for trace in chart["data"]] == ["Norepinephrine (mcg/min)", "Propofol (mL/h)"]
    assert chart["layout"]["yaxis"]["title"] == "" and chart["title"] == "Charted infusion rates"
    for administration in connected.resources["MedicationAdministration"]:
        administration.get("dosage", {}).get("rateQuantity", {}).pop("unit", None)
    _, components = card_of(mcp_tools.medication_review(patient="002-1"))
    chart = of_type(components, "plotly_chart")[0]
    assert [trace["name"] for trace in chart["data"]] == ["Norepinephrine", "Propofol"] and chart["title"] == "Charted infusion rates"


def test_medication_review_lists_each_order_once_and_caps_the_table(connected, monkeypatch):
    template = connected.resources["MedicationRequest"][0]
    connected.resources["MedicationRequest"] += [dict(template, id="mr-9"), dict(template, id="mr-10", status="completed")]
    result = mcp_tools.medication_review(patient="002-1")
    _, components = card_of(result)
    rows = {table["title"]: table for table in of_type(components, "table")}["Orders"]["rows"]
    assert [row[:3:2] for row in rows] == [
        ["NOREPINEPHRINE", "Active"], ["ASPIRIN", "Completed"], ["m9", "Cancelled"], ["NOREPINEPHRINE", "Completed"]]
    assert result["_data"]["order_counts"] == {"active": 2, "completed": 2, "cancelled": 1}
    assert result["_data"]["active_orders"] == ["NOREPINEPHRINE"]
    assert not any("orders, active" in text["content"] for text in of_type(components, "text"))
    monkeypatch.setattr(mcp_tools, "ORDER_ROWS", 2)
    _, components = card_of(mcp_tools.medication_review(patient="002-1"))
    assert len({table["title"]: table for table in of_type(components, "table")}["Orders"]["rows"]) == 2
    assert "Showing 2 of 4 orders, active and most recent first." in [text["content"] for text in of_type(components, "text")]
    overview = mcp_tools.patient_overview(patient="002-1")
    tables = {table["title"]: table for table in of_type(card_of(overview)[1], "table")}
    assert tables["Active orders"]["rows"] == [["NOREPINEPHRINE", "4 mg IV Continuous", "Oct 5, 15:40"]]
    assert overview["_data"]["active_medications"] == ["NOREPINEPHRINE"]


def test_medication_review_with_no_records(connected):
    result = mcp_tools.medication_review(patient="002-2")
    _, components = card_of(result)
    assert result["_data"]["order_counts"] == {} and "No medication records" in of_type(components, "alert")[0]["message"]


def test_patient_timeline_keeps_events_inside_the_window(connected):
    result = mcp_tools.patient_timeline(patient="002-1", hours=1)
    card, components = card_of(result)
    data = result["_data"]
    assert card["id"] == "fhir-timeline-002-1" and data["event_count"] == len(data["events"]) == 9
    events = [event["event"] for event in data["events"]]
    assert events[0] == "Treatment: Mechanical ventilation" and "Admitted to Med-Surg ICU" in events
    assert "Critical result: Lactate 5.1 mmol/L" in events and "Admitted to hospital" not in events
    sorts = {call[1]: call[2].get("_sort") for call in connected.calls if call[0] == "GET"}
    assert (sorts["Condition"], sorts["Procedure"], sorts["MedicationRequest"], sorts["Observation"]) == (
        ["-recorded-date"], ["-date"], ["-authoredon"], ["-date"])
    items = of_type(components, "timeline")[0]["items"]
    assert items[0] == {"title": "Treatment: Mechanical ventilation", "time": "Oct 5, 15:45", "variant": "default"}
    assert any(item.get("description") == "4 mg IV Continuous" for item in items)


def test_patient_timeline_truncates_and_reports_empty_windows(connected, monkeypatch):
    monkeypatch.setattr(mcp_tools.clinical, "timeline_events", lambda resources: [
        {"when": NOW, "title": f"Event {index}", "description": "", "variant": "default"} for index in range(55)
    ])
    result = mcp_tools.patient_timeline(patient="002-1")
    _, components = card_of(result)
    assert result["_data"]["event_count"] == 55 and len(result["_data"]["events"]) == 40
    assert "Showing the 40 most recent of 55 events." in [text["content"] for text in of_type(components, "text")]
    monkeypatch.setattr(mcp_tools.clinical, "timeline_events", lambda resources: [])
    _, components = card_of(mcp_tools.patient_timeline(patient="002-1"))
    assert "Nothing was recorded" in of_type(components, "alert")[0]["message"]


def test_source_status_describes_the_server(connected):
    result = mcp_tools.fhir_source_status()
    card, components = card_of(result)
    data = result["_data"]
    assert card["id"] == "fhir-source" and data["fhir_version"] == "5.0.0"
    assert data["resource_types"] == ["Observation", "Encounter"]
    assert data["replay"]["activeIcuEncounters"] == 189
    stats = {item["label"]: item["value"] for item in of_type(components, "stat_group")[0]["items"]}
    assert stats == {"Patients": "1,841", "ICU stays": "2,520", "In ICU now": "189", "Observations": "6,960,940"}
    facts = {item["label"]: item["value"] for item in of_type(components, "keyvalue")[0]["items"]}
    assert facts["FHIR version"] == "5.0.0" and facts["Software"] == "eicu-fhir 0.1.0" and facts["Replay cycle (days)"] == "28"
    assert facts["Address"] == "http://eicu-fhir:8080/fhir" and facts["Server time"] == "Oct 5, 16:00"
    assert facts["Time zone"] == "America/New_York" and facts["Dataset"] == "eICU demo 2.0.1"
    assert of_type(components, "table")[0]["rows"] == [
        ["Observation", "read, search-type", "code, patient"],
        ["Encounter", "read", "class, date, date-start, end-date, identifier, location and 2 more"],
    ]


def test_source_status_for_a_plain_fhir_server(connected):
    connected.replay_available = False
    result = mcp_tools.fhir_source_status()
    _, components = card_of(result)
    assert result["_data"]["replay"] == {} and of_type(components, "stat_group") == []
    assert mcp_tools.server_time(mcp_tools.connect()).tzinfo is not None


def test_server_time_falls_back_when_the_clock_is_missing(connected, monkeypatch):
    client = mcp_tools.connect()
    monkeypatch.setattr(client, "get", lambda *a, **k: {"resourceType": "Parameters", "parameter": [{"name": "now", "valueInstant": "?"}]})
    assert mcp_tools.server_time(client).tzinfo is not None


@pytest.mark.parametrize(("resource_type", "filters", "headers", "first"), [
    ("patient", {"deceased": "true"}, ["Patient", "Sex", "Born", "Died"], ["002-2", "male", ""]),
    ("Encounter", {"patient": "002-1"}, ["Encounter", "Patient", "Status", "Unit", "Start", "End"], ["icu-1", "002-1", "In progress", "Med-Surg ICU", "Oct 5, 15:30", ""]),
    ("Observation", {"patient": "002-1", "code": "8867-4"}, ["Time", "Patient", "Measurement", "Value"], ["Oct 5, 15:55", "002-1", "Heart rate", "148 /min"]),
    ("Condition", {"patient": "002-1"}, ["Recorded", "Patient", "Condition", "Status"], ["Oct 5, 15:35", "002-1", "Acute respiratory failure", "Active"]),
    ("Procedure", None, ["Started", "Patient", "Treatment", "Status"], ["Oct 5, 15:45", "002-1", "Mechanical ventilation", "In progress"]),
    ("AllergyIntolerance", {}, ["Recorded", "Patient", "Allergy"], ["Oct 5, 15:31", "002-1", "Penicillins"]),
    ("MedicationRequest", {"status": "active"}, ["Time", "Patient", "Medication", "Dose", "Status"], ["Oct 5, 15:40", "002-1", "NOREPINEPHRINE", "4 mg IV Continuous", "Active"]),
    ("MedicationAdministration", {}, ["Time", "Patient", "Medication", "Dose", "Status"], ["Oct 5, 15:42", "002-1", "Norepinephrine", "", "Completed"]),
    ("MedicationStatement", {}, ["Time", "Patient", "Medication", "Dose", "Status"], ["Oct 5, 15:32", "002-1", "LISINOPRIL", "10 mg daily", "Recorded"]),
    ("RiskAssessment", {}, ["Time", "Patient", "Method", "Predictions"], ["Oct 5, 15:50", "002-1", "APACHE IV", "ICU mortality 10.0%"]),
    ("Organization", {}, ["Record", "Label", "Details"], ["hospital-10", "eICU Hospital 10", "Teaching hospital"]),
])
def test_record_search_tabulates_each_resource_type(connected, resource_type, filters, headers, first):
    result = mcp_tools.query_fhir_records(resource_type=resource_type, filters=filters, limit=5)
    card, components = card_of(result)
    data = result["_data"]
    assert data["columns"] == headers and data["rows"][0][:len(first)] == first
    assert card["id"] == f"fhir-records-{data['resource_type'].lower()}" and data["resource_type"] in RESOURCE_TYPES
    assert of_type(components, "table")[0]["headers"] == headers


def test_record_search_reports_totals_and_empty_results(connected):
    connected.page_size = 2
    result = mcp_tools.query_fhir_records(resource_type="Observation", filters={"patient": "002-1"}, limit=2)
    _, components = card_of(result)
    assert (result["_data"]["total"], result["_data"]["returned"]) == (22, 2)
    assert of_type(components, "hero")[0]["subtitle"] == "Showing 2 of 22 · patient=002-1"
    empty = mcp_tools.query_fhir_records(resource_type="Observation", filters={"patient": "none"})
    _, components = card_of(empty)
    assert empty["_data"]["rows"] == [] and of_type(components, "alert")[0]["message"] == "No records matched this search."
    assert of_type(components, "hero")[0]["subtitle"] == "Showing 0 · patient=none"


@pytest.mark.parametrize(("resource_type", "filters"), [
    ("Spaceship", {}), ("", None), ("Patient", {"bad key!": "x"}), ("Patient", {"_format": "xml"}), ("Patient", {"_count": "5"}),
    ("Patient", {"gender": ""}), ("Patient", {"gender": ["male"]}), ("Patient", {"gender": True}), ("Patient", {"gender": "x" * 300}),
])
def test_record_search_refuses_unusable_requests(connected, resource_type, filters):
    result = mcp_tools.query_fhir_records(resource_type=resource_type, filters=filters)
    assert result["_error"]["code"] == "FHIR_BAD_REQUEST" and connected.calls == []


def test_numeric_filter_values_are_accepted(connected):
    assert mcp_tools.query_fhir_records(resource_type="Patient", filters={"_id": 2})["_data"]["filters"] == {"_id": "2"}


@pytest.mark.parametrize(("failure", "code", "retryable"), [
    (external_http.ServiceUnreachableError("down"), "FHIR_UNAVAILABLE", True),
    (external_http.AuthFailedError("401"), "FHIR_AUTH_FAILED", False),
])
def test_server_failures_become_coded_tool_errors(connected, failure, code, retryable):
    connected.failures["Encounter"] = failure
    assert mcp_tools.icu_census()["_error"] == {"code": code, "message": mcp_tools.icu_census()["_error"]["message"], "retryable": retryable}


def test_tools_report_a_missing_configuration(monkeypatch):
    monkeypatch.delenv("FHIR_BASE_URL", raising=False)
    monkeypatch.delenv("FHIR_ACCESS_TOKEN", raising=False)
    for tool in (mcp_tools.icu_census, mcp_tools.fhir_source_status):
        assert tool()["_error"]["code"] == "FHIR_NOT_CONFIGURED"
    with pytest.raises(FhirError):
        mcp_tools.connect()


def test_small_helpers():
    assert mcp_tools.bounded("7", 3, 1, 5) == 5 and mcp_tools.bounded(None, 3, 1, 5) == 3 and mcp_tools.bounded(-4, 3, 1, 5) == 1
    assert mcp_tools.iso(None) is None and mcp_tools.reading_text(None) == "—"
    assert mcp_tools.current_stay([]) is None
    assert mcp_tools.first_reading([], "8302-2") == ""
    assert mcp_tools.loinc_tokens(("a", "b")) == "http://loinc.org|a,http://loinc.org|b"
    assert mcp_tools.most_recent(6000, 7560, "readings") == "Showing the most recent 6,000 of 7,560 readings."
    assert mcp_tools.most_recent(10, 10, "readings") == "" and mcp_tools.most_recent(10, None, "readings") == ""
