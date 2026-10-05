"""Exercises the view-model helpers: thresholds, readings, laboratory rows, trend series and
timeline events built from FHIR resources.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from agents.fhir import clinical
from agents.fhir.tests.conftest import NOW, at, build_resources, observation, pressure, quantity


@pytest.fixture
def observations():
    return [item for item in build_resources()["Observation"] if item["subject"]["reference"] == "Patient/002-1"]


@pytest.mark.parametrize(("key", "value", "variant"), [
    ("heart_rate", 80, "default"), ("heart_rate", 45, "warning"), ("heart_rate", 130, "warning"),
    ("heart_rate", 30, "error"), ("heart_rate", 150, "error"), ("heart_rate", None, "default"),
    ("oxygen_saturation", 99, "default"), ("oxygen_saturation", 90, "warning"), ("oxygen_saturation", 80, "error"),
    ("mean_arterial_pressure", 60, "warning"), ("mean_arterial_pressure", 50, "error"), ("diastolic", 20, "default"),
    ("temperature", 38.6, "warning"), ("temperature", 40, "error"), ("glasgow_coma_score", 8, "error"),
])
def test_vital_display_thresholds(key, value, variant):
    assert clinical.VITALS_BY_KEY[key].variant(value) == variant


def test_laboratory_reference_intervals():
    potassium = clinical.LAB_RANGES["2823-3"]
    assert [potassium.variant(value) for value in (4.0, 3.2, 5.5, 2.0, 7.0, None)] == [
        "default", "warning", "warning", "error", "error", "default"]
    assert potassium.text() == "3.5–5"
    assert clinical.LAB_RANGES["10839-9"].text() == "< 0.04"
    assert clinical.LabRange("x", 2, None).text() == "> 2" and clinical.LabRange("x", None, None).text() == ""
    assert set(clinical.KEY_LABS) <= set(clinical.LAB_RANGES)


def test_time_and_number_formatting():
    moment = datetime(2026, 10, 5, 9, 5, tzinfo=NOW.tzinfo)
    assert clinical.format_time(moment) == "Oct 5, 09:05" and clinical.format_time(None) == "unknown"
    assert clinical.parse_time("2026-10-05T09:05:00Z").utcoffset() == timedelta(0)
    assert clinical.parse_time("2026") is None and clinical.parse_time(5) is None and clinical.parse_time("not-a-date-at-all") is None
    assert [clinical.format_number(value) for value in (5, 5.0, 3.25, 0.039999)] == ["5", "5", "3.25", "0.04"]
    assert [clinical.ago(NOW - timedelta(minutes=minutes), NOW) for minutes in (0, 12, 150, 4000)] == [
        "just now", "12 min ago", "2 h ago", "2 d ago"]
    assert clinical.ago(None, NOW) == "" and clinical.ago(NOW, None) == ""
    assert [clinical.duration(minutes) for minutes in (-5, 45, 600, 5000)] == ["0 min", "45 min", "10.0 h", "3.5 d"]


def test_concept_and_reference_helpers():
    concept = {"coding": [{"system": clinical.LOINC, "code": "8867-4", "display": "Heart rate"}, "junk"]}
    assert clinical.loinc_codes(concept) == ["8867-4"] and clinical.code_of(concept, clinical.LOINC) == "8867-4"
    assert clinical.code_of(concept, "http://other") is None and clinical.codings(None) == []
    assert clinical.concept_text(concept) == "Heart rate" and clinical.concept_text({"text": "x"}) == "x"
    assert clinical.concept_text({"coding": [{"code": "only-code"}]}) == "only-code"
    assert clinical.concept_text(None) == "" and clinical.concept_text({}) == ""
    assert clinical.reference_id({"reference": "Patient/002-1"}) == "002-1"
    assert clinical.reference_id({"display": "x"}) == "" and clinical.reference_id(None) == ""


def test_readings_cover_every_value_shape(observations):
    by_id = {item["id"]: clinical.reading(item) for item in observations}
    assert (by_id["hr-1"].value, by_id["hr-1"].display, by_id["hr-1"].unit, by_id["hr-1"].code) == (148.0, "148", "/min", "8867-4")
    assert by_id["hr-1"].category == ("vital-signs",) and by_id["hr-1"].when == NOW - timedelta(minutes=5)
    assert (by_id["tn-1"].display, by_id["tn-1"].comparator) == ("<0.04", "<")
    assert (by_id["ket-1"].display, by_id["ket-1"].value, by_id["ket-1"].key) == ("POSITIVE", None, "Serum ketones")
    assert by_id["dev-1"].display == "nasal cannula"
    assert by_id["bp-1"].display == "92/51 (62)" and by_id["bp-1"].unit == "mmHg"
    assert by_id["bp-2"].display == "71"
    empty = clinical.reading({"resourceType": "Observation", "dataAbsentReason": {"text": "Unknown"}})
    assert (empty.display, empty.label, empty.when) == ("Unknown", "Observation", None)
    assert clinical.reading({"valueQuantity": {"value": True}}).display == "not recorded"


def test_latest_vitals_pick_the_newest_value_and_panel_components(observations):
    latest = clinical.latest_vitals(observations)
    assert {key: item.value for key, item in latest.items()} == {
        "heart_rate": 148.0, "oxygen_saturation": 91.0, "respiratory_rate": 22.0, "systolic": 92.0, "diastolic": 51.0,
        "mean_arterial_pressure": 62.0, "temperature": 38.6, "glasgow_coma_score": 14.0,
    }
    assert clinical.worst_variant(latest) == "error"
    assert clinical.worst_variant(latest, ("oxygen_saturation", "mean_arterial_pressure")) == "warning"
    assert clinical.worst_variant({}) == "default"
    assert clinical.vital_value(observations[0], clinical.VITALS_BY_KEY["oxygen_saturation"]) is None
    assert clinical.component_value(observations[0], "8480-6") is None


def test_series_are_ordered_and_downsampled(observations):
    heart = clinical.series(observations, clinical.VITALS_BY_KEY["heart_rate"])
    assert [value for _, value in heart] == [88.0, 96.0, 148.0]
    assert [value for _, value in clinical.series(observations, clinical.VITALS_BY_KEY["mean_arterial_pressure"])] == [71.0, 62.0]
    many = [observation(f"x{index}", "p", "8867-4", "Heart rate", "vital-signs", 1000 - index, **quantity(index, "/min")) for index in range(1000)]
    reduced = clinical.series(many, clinical.VITALS_BY_KEY["heart_rate"], maximum=50)
    assert len(reduced) == 50 and reduced[0][1] == 0 and reduced[-1][1] == 999


def test_laboratory_rows_flag_and_compare(observations):
    rows = {row["label"]: row for row in clinical.lab_rows([item for item in observations if "laboratory" in clinical.categories(item)])}
    assert (rows["Lactate"]["flag"], rows["Lactate"]["variant"], rows["Lactate"]["reference"]) == ("Critical", "error", "0.5–2")
    assert (rows["Potassium"]["flag"], rows["Potassium"]["change"], rows["Potassium"]["value"]) == ("Moderate", "↓ 0.9", "3.2")
    assert [value for _, value in rows["Potassium"]["history"]] == [4.1, 3.2]
    assert (rows["Troponin I"]["flag"], rows["Troponin I"]["value"]) == ("Stable", "<0.04")
    assert (rows["Serum ketones"]["flag"], rows["Serum ketones"]["reference"], rows["Serum ketones"]["change"]) == ("", "", "")
    ordered = [row["label"] for row in clinical.lab_rows([item for item in observations if "laboratory" in clinical.categories(item)])]
    assert ordered == ["Lactate", "Potassium", "Troponin I", "Serum ketones"]
    same = [observation(f"n{index}", "p", "2951-2", "Sodium", "laboratory", index, **quantity(140, "mmol/L")) for index in (1, 2)]
    assert clinical.lab_rows(same)[0]["change"] == "→"
    assert clinical.lab_rows([{"resourceType": "Observation", "code": {"text": "undated"}}]) == []


def test_age_text(observations):
    assert clinical.age_text(observations) == "67-year-old"
    older = [observation("a", "p", "30525-0", "Age", "social-history", 1, **quantity(89, "years", ">"))]
    assert clinical.age_text(older) == "over 89-year-old" and clinical.age_text([]) == ""
    assert clinical.age_text([observation("a", "p", "30525-0", "Age", "social-history", 1, valueString="?")]) == ""


def test_encounter_and_medication_helpers():
    resources = build_resources()
    unit, hospital = resources["Encounter"][0], resources["Encounter"][1]
    assert clinical.encounter_class(unit) == "ACUTE" and clinical.encounter_unit(unit) == "Med-Surg ICU"
    assert clinical.encounter_unit(hospital) == "" and clinical.encounter_class({}) == ""
    assert clinical.admission_reason(unit) == "Sepsis, pulmonary" and clinical.admission_reason(hospital) == ""
    assert clinical.encounter_period(unit) == (NOW - timedelta(minutes=30), None)
    orders = resources["MedicationRequest"]
    assert [clinical.medication_text(order) for order in orders] == ["NOREPINEPHRINE", "ASPIRIN", "m9"]
    assert clinical.medication_text({}) == "Unspecified medication"
    assert clinical.dosage_text(orders[0]) == "4 mg IV Continuous" and clinical.dosage_text(orders[1]) == ""
    assert clinical.dosage_text({"dosage": {"text": "8 mcg/min"}}) == "8 mcg/min"


def test_timeline_events_are_newest_first_and_typed():
    resources = build_resources()
    everything = [item for name in ("Encounter", "Condition", "Procedure", "MedicationRequest", "Observation") for item in resources[name]]
    events = clinical.timeline_events(everything)
    assert [event["when"] for event in events] == sorted((event["when"] for event in events), reverse=True)
    titles = [event["title"] for event in events]
    assert "Admitted: Med-Surg ICU" in titles and "Admitted: Hospital stay" in titles and "Discharged: Cardiac ICU" in titles
    assert "Diagnosis: acute respiratory failure" in titles and "Treatment: mechanical ventilation" in titles
    assert "Order: NOREPINEPHRINE" in titles and "Critical result: Lactate 5.1 mmol/L" in titles
    assert not any("Potassium" in title for title in titles)
    critical = next(event for event in events if event["title"].startswith("Critical"))
    assert (critical["variant"], critical["description"]) == ("error", "Reference 0.5–2")
    assert clinical.timeline_events([{"resourceType": "Encounter"}, {"resourceType": "Condition"}, {"resourceType": "Procedure"},
                                     {"resourceType": "MedicationRequest"}, {"resourceType": "Unknown"}]) == []
    bare = clinical.timeline_events([{"resourceType": "Encounter", "actualPeriod": {"start": at(1)}}])
    assert bare[0]["title"] == "Admitted: Encounter"


def test_pressure_helper_marks_missing_components():
    panel = pressure("x", "p", 1, None, None, 70)
    assert clinical.component_value(panel, "8478-0") == 70 and clinical.component_value(panel, "8480-6") is None
