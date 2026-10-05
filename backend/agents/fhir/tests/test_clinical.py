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


def test_text_helpers_tidy_labels_for_display():
    assert clinical.sentence_case("atrial fibrillation with rapid ventricular response") == (
        "Atrial fibrillation with rapid ventricular response")
    assert [clinical.sentence_case(text) for text in ("COPD", "s/p CABG", "pH", "Type II", "", "non-insulin dependent")] == [
        "COPD", "s/p CABG", "pH", "Type II", "", "Non-insulin dependent"]
    assert clinical.sentence_case("hypokalemia, severe ( < 2.8 meq/dl)") == "Hypokalemia, severe ( < 2.8 meq/dl)"
    assert clinical.sentence_case("5% dextrose") == "5% dextrose"
    long = "Cardiac arrest (with or without respiratory arrest; for respiratory arrest see Respiratory System)"
    assert clinical.shorten(long, 40) == "Cardiac arrest (with or without…"
    assert clinical.shorten("Sepsis,   pulmonary", 40) == "Sepsis, pulmonary" and clinical.shorten("", 10) == ""
    assert clinical.shorten("Pneumonoultramicroscopic", 10) == "Pneumonoul…"
    assert [clinical.count_of(number, "reading") for number in (0, 1, 2)] == ["0 readings", "1 reading", "2 readings"]


def test_concept_and_reference_helpers():
    concept = {"coding": [{"system": clinical.LOINC, "code": "8867-4", "display": "Heart rate"}, "junk"]}
    assert clinical.loinc_codes(concept) == ["8867-4"] and clinical.code_of(concept, clinical.LOINC) == "8867-4"
    assert clinical.code_of(concept, "http://other") is None and clinical.codings(None) == []
    assert clinical.concept_text(concept) == "Heart rate" and clinical.concept_text({"text": "x"}) == "x"
    assert clinical.concept_text({"coding": [{"code": "only-code"}]}) == "only-code"
    assert clinical.concept_text(None) == "" and clinical.concept_text({}) == ""
    assert clinical.reference_id({"reference": "Patient/002-1"}) == "002-1"
    assert clinical.reference_id({"display": "x"}) == "" and clinical.reference_id(None) == ""
    named = {"reference": "Organization/hospital-10", "display": " eICU Hospital 10 "}
    assert clinical.reference_label(named) == "eICU Hospital 10"
    assert clinical.reference_label({"reference": "Organization/hospital-10", "display": " "}) == "hospital-10"
    assert clinical.reference_label({"reference": "Organization/hospital-10"}) == "hospital-10"
    assert clinical.reference_label(None) == ""


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


def test_reading_labels_prefer_plain_names():
    hemoglobin = observation("h", "p", "718-7", "Hgb", "laboratory", 1, **quantity(12.2, "g/dL"))
    assert clinical.reading(hemoglobin).label == "Hemoglobin"
    cells = observation("w", "p", "6690-2", "WBC x 1000", "laboratory", 1, **quantity(7.6, "K/µL"))
    assert (clinical.reading(cells).label, clinical.reading(cells).unit) == ("White blood cells", "K/µL")
    assert clinical.reading(observation("c", "p", "17861-6", "calcium", "laboratory", 1, **quantity(7.9, "mg/dL"))).label == "Calcium"
    assert clinical.reading(observation("a", "p", "local", "alkaline phos.", "laboratory", 1, **quantity(80, "U/L"))).label == "Alkaline phos."
    assert clinical.reading(observation("m", "p", "local", "MCV", "laboratory", 1, **quantity(90, "fL"))).label == "MCV"
    ratio = clinical.reading(observation("i", "p", "6301-6", "PT - INR", "laboratory", 1, **quantity(1.6, "INR")))
    assert (ratio.label, ratio.display, ratio.unit) == ("INR", "1.6", "")
    assert clinical.distinct([["a", "1"], ("a", "1"), ["b", "2"], ["a", "1"]]) == [["a", "1"], ["b", "2"]]


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
    assert [value for _, value in clinical.series(observations, clinical.VITALS_BY_KEY["mean_arterial_pressure"])] == [82.0, 71.0, 62.0]
    many = [observation(f"x{index}", "p", "8867-4", "Heart rate", "vital-signs", 1000 - index, **quantity(index, "/min")) for index in range(1000)]
    complete = clinical.series(many, clinical.VITALS_BY_KEY["heart_rate"])
    assert len(complete) == 1000 and complete == sorted(complete)
    reduced = clinical.thin(complete, 50)
    assert len(reduced) <= 50 and reduced[0] == complete[0] and reduced[-1] == complete[-1] and reduced == sorted(reduced)
    assert clinical.thin(complete[:50], 50) == complete[:50] and clinical.thin([], 50) == []


def test_thinning_a_series_keeps_its_peaks_and_troughs():
    values = [70.0] * 2000
    values[777], values[1403] = 188.0, 31.0
    points = [(NOW - timedelta(minutes=2000 - index), value) for index, value in enumerate(values)]
    reduced = clinical.thin(points, 300)
    assert len(reduced) <= 300 and {188.0, 31.0} <= {value for _, value in reduced}
    assert [when for when, _ in reduced] == sorted(when for when, _ in reduced)
    assert len(clinical.thin(points, 3)) <= 4 and clinical.thin(points, 3)[0] == points[0]


def test_series_average_readings_taken_at_the_same_moment():
    monitored = [observation(f"m{index}", "p", "8867-4", "Heart rate", "vital-signs", minutes, **quantity(value, "/min"))
                 for index, (minutes, value) in enumerate(((10, 60), (5, 64), (5, 67), (5, 70), (0, 58)))]
    points = clinical.series(monitored, clinical.VITALS_BY_KEY["heart_rate"])
    assert [value for _, value in points] == [60.0, 67.0, 58.0]
    assert [when for when, _ in points] == sorted(when for when, _ in points) and len({when for when, _ in points}) == 3


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
    listed = {"medication": {"reference": {"reference": "Medication/m9", "display": "Heparin 5,000 units"}}}
    assert clinical.medication_text(listed) == "Heparin 5,000 units"
    assert clinical.medication_text({}) == "Unspecified medication"
    assert clinical.dosage_text(orders[0]) == "4 mg IV Continuous" and clinical.dosage_text(orders[1]) == ""
    assert clinical.dosage_text({"dosage": {"text": "8 mcg/min"}}) == "8 mcg/min"


def test_timeline_events_are_newest_first_and_typed():
    resources = build_resources()
    everything = [item for name in ("Encounter", "Condition", "Procedure", "MedicationRequest", "Observation") for item in resources[name]]
    events = clinical.timeline_events(everything)
    assert [event["when"] for event in events] == sorted((event["when"] for event in events), reverse=True)
    titles = [event["title"] for event in events]
    assert "Admitted to Med-Surg ICU" in titles and "Admitted to hospital" in titles and "Left Cardiac ICU" in titles
    assert "Diagnosis: Acute respiratory failure" in titles and "Treatment: Mechanical ventilation" in titles
    left = next(event for event in events if event["title"] == "Left Cardiac ICU")
    assert (left["description"], left["variant"]) == ("To Floor", "success")
    admitted = next(event for event in events if event["title"] == "Admitted to Med-Surg ICU")
    assert (admitted["description"], admitted["variant"]) == ("Sepsis, pulmonary", "info")
    assert "Order: NOREPINEPHRINE" in titles and "Critical result: Lactate 5.1 mmol/L" in titles
    assert not any("Potassium" in title for title in titles)
    critical = next(event for event in events if event["title"].startswith("Critical"))
    assert (critical["variant"], critical["description"]) == ("error", "Reference 0.5–2")
    assert clinical.timeline_events([{"resourceType": "Encounter"}, {"resourceType": "Condition"}, {"resourceType": "Procedure"},
                                     {"resourceType": "MedicationRequest"}, {"resourceType": "Unknown"}]) == []
    bare = clinical.timeline_events([{"resourceType": "Encounter", "actualPeriod": {"start": at(2), "end": at(1)}}])
    assert [(event["title"], event["description"]) for event in bare] == [("Encounter ended", ""), ("Encounter started", "")]
    unnamed = {"resourceType": "Encounter", "class": [{"coding": [{"code": "ACUTE"}]}], "actualPeriod": {"start": at(1)}}
    assert clinical.timeline_events([unnamed])[0]["title"] == "Admitted to intensive care"
    unnamed["type"] = [{"text": "Med-Surg ICU"}, {"text": "stepdown/other"}]
    moved = clinical.timeline_events([unnamed])[0]
    assert (moved["title"], moved["description"]) == ("Admitted to Med-Surg ICU", "Stepdown/other")
    unnamed["type"][1] = {"text": "admit"}
    assert clinical.timeline_events([unnamed])[0]["description"] == "" and clinical.encounter_kind({}) == ""
    assert [clinical.status_text(code) for code in ("in-progress", "active", None, "entered-in-error")] == [
        "In progress", "Active", "", "Entered in error"]
    stay = {"resourceType": "Encounter", "class": [{"coding": [{"code": "IMP"}]}], "actualPeriod": {"start": at(9), "end": at(1)},
            "admission": {"dischargeDisposition": {"text": "Home"}}}
    assert [(event["title"], event["description"]) for event in clinical.timeline_events([stay])] == [
        ("Discharged from hospital", "To Home"), ("Admitted to hospital", "")]


def test_pressure_helper_marks_missing_components():
    panel = pressure("x", "p", 1, None, None, 70)
    assert clinical.component_value(panel, "8478-0") == 70 and clinical.component_value(panel, "8480-6") is None
