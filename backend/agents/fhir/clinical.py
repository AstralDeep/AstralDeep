"""Turns FHIR R5 resources into the plain view models the FHIR agent displays: readings,
display thresholds, laboratory reference intervals, trends and timeline events.
mcp_tools.py feeds it resources from client.py and presentation.py renders the results.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

LOINC = "http://loinc.org"
OBSERVATION_CATEGORY = "http://terminology.hl7.org/CodeSystem/observation-category"

NORMAL, WARNING, CRITICAL = "default", "warning", "error"
FLAG_WORDS = {NORMAL: "Stable", WARNING: "Moderate", CRITICAL: "Critical"}
SEVERITY = {NORMAL: 0, WARNING: 1, CRITICAL: 2}


@dataclass(frozen=True)
class Vital:
    key: str
    label: str
    codes: Tuple[str, ...]
    unit: str
    low_critical: Optional[float] = None
    low_warning: Optional[float] = None
    high_warning: Optional[float] = None
    high_critical: Optional[float] = None
    component: Optional[str] = None
    color: str = "#6366F1"

    def variant(self, value: Optional[float]) -> str:
        if value is None:
            return NORMAL
        if (self.low_critical is not None and value < self.low_critical) or (
            self.high_critical is not None and value > self.high_critical
        ):
            return CRITICAL
        if (self.low_warning is not None and value < self.low_warning) or (
            self.high_warning is not None and value > self.high_warning
        ):
            return WARNING
        return NORMAL


VITALS: Tuple[Vital, ...] = (
    Vital("heart_rate", "Heart rate", ("8867-4",), "/min", 40, 50, 120, 140, color="#EF4444"),
    Vital("oxygen_saturation", "SpO2", ("59408-5", "2708-6"), "%", 88, 92, None, None, color="#3B82F6"),
    Vital("respiratory_rate", "Respiratory rate", ("9279-1",), "/min", 8, 10, 28, 35, color="#10B981"),
    Vital("systolic", "Systolic BP", ("85354-9",), "mmHg", 80, 90, 180, 200, component="8480-6", color="#F59E0B"),
    Vital("diastolic", "Diastolic BP", ("85354-9",), "mmHg", None, None, None, None, component="8462-4", color="#FBBF24"),
    Vital("mean_arterial_pressure", "Mean arterial pressure", ("85354-9",), "mmHg", 55, 65, 130, None, component="8478-0", color="#D97706"),
    Vital("temperature", "Temperature", ("8310-5",), "°C", 35.0, 36.0, 38.3, 39.5, color="#A855F7"),
    Vital("glasgow_coma_score", "Glasgow coma score", ("9269-2",), "", 9, 13, None, None, color="#14B8A6"),
)
VITALS_BY_KEY: Dict[str, Vital] = {vital.key: vital for vital in VITALS}
VITAL_CODES: Tuple[str, ...] = tuple(dict.fromkeys(code for vital in VITALS for code in vital.codes))
CENSUS_VITALS = ("heart_rate", "oxygen_saturation", "mean_arterial_pressure")


@dataclass(frozen=True)
class LabRange:
    label: str
    low: Optional[float]
    high: Optional[float]
    low_critical: Optional[float] = None
    high_critical: Optional[float] = None

    def variant(self, value: Optional[float]) -> str:
        if value is None:
            return NORMAL
        if (self.low_critical is not None and value < self.low_critical) or (
            self.high_critical is not None and value > self.high_critical
        ):
            return CRITICAL
        if (self.low is not None and value < self.low) or (self.high is not None and value > self.high):
            return WARNING
        return NORMAL

    def text(self) -> str:
        if self.low is not None and self.high is not None:
            return f"{self.low:g}–{self.high:g}"
        if self.high is not None:
            return f"< {self.high:g}"
        return f"> {self.low:g}" if self.low is not None else ""


LAB_RANGES: Dict[str, LabRange] = {
    "2823-3": LabRange("Potassium", 3.5, 5.0, 2.5, 6.5),
    "2951-2": LabRange("Sodium", 135, 145, 120, 160),
    "2075-0": LabRange("Chloride", 98, 107),
    "1963-8": LabRange("Bicarbonate", 22, 29, 10, 40),
    "3094-0": LabRange("Urea nitrogen", 7, 20, None, 100),
    "2160-0": LabRange("Creatinine", 0.6, 1.3, None, 5.0),
    "2345-7": LabRange("Glucose", 70, 140, 40, 450),
    "41653-7": LabRange("Bedside glucose", 70, 180, 40, 450),
    "17861-6": LabRange("Calcium", 8.5, 10.5, 6.5, 13.0),
    "19123-9": LabRange("Magnesium", 1.7, 2.4, 1.0, 4.9),
    "2777-1": LabRange("Phosphate", 2.5, 4.5, 1.0, None),
    "718-7": LabRange("Hemoglobin", 12.0, 17.5, 7.0, 20.0),
    "4544-3": LabRange("Hematocrit", 36, 52, 21, 60),
    "6690-2": LabRange("White blood cells", 4.0, 11.0, 1.0, 30.0),
    "777-3": LabRange("Platelets", 150, 400, 20, 1000),
    "2524-7": LabRange("Lactate", 0.5, 2.0, None, 4.0),
    "10839-9": LabRange("Troponin I", None, 0.04, None, 0.4),
    "6301-6": LabRange("INR", 0.8, 1.2, None, 5.0),
    "2744-1": LabRange("Arterial pH", 7.35, 7.45, 7.2, 7.6),
    "2703-7": LabRange("Arterial pO2", 80, 100, 55, None),
    "2019-8": LabRange("Arterial pCO2", 35, 45, 20, 70),
    "1960-4": LabRange("Arterial bicarbonate", 22, 26, 10, 40),
    "1975-2": LabRange("Total bilirubin", 0.2, 1.2, None, 12.0),
    "1920-8": LabRange("AST", 10, 40, None, 1000),
    "1742-6": LabRange("ALT", 7, 56, None, 1000),
    "1751-7": LabRange("Albumin", 3.5, 5.0, 1.5, None),
    "30934-4": LabRange("BNP", None, 100),
}
KEY_LABS: Tuple[str, ...] = (
    "2823-3", "2951-2", "2160-0", "3094-0", "2345-7", "718-7", "6690-2", "777-3", "2524-7", "10839-9", "2744-1", "6301-6",
)


@dataclass(frozen=True)
class Reading:
    key: str
    label: str
    value: Optional[float]
    display: str
    unit: str
    when: Optional[datetime]
    code: Optional[str] = None
    category: Tuple[str, ...] = ()
    comparator: str = ""


def parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def format_time(moment: Optional[datetime]) -> str:
    return f"{moment:%b} {moment.day}, {moment:%H:%M}" if moment else "unknown"


def format_number(value: float) -> str:
    rounded = round(float(value), 2)
    return str(int(rounded)) if rounded == int(rounded) else f"{rounded:g}"


def ago(moment: Optional[datetime], now: Optional[datetime]) -> str:
    if moment is None or now is None:
        return ""
    minutes = int((now - moment).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} h ago"
    return f"{minutes // 1440} d ago"


def duration(minutes: float) -> str:
    minutes = max(0, int(minutes))
    if minutes < 60:
        return f"{minutes} min"
    if minutes < 48 * 60:
        return f"{minutes / 60:.1f} h"
    return f"{minutes / 1440:.1f} d"


def codings(concept: Any) -> List[Dict[str, Any]]:
    if not isinstance(concept, dict):
        return []
    return [coding for coding in concept.get("coding") or [] if isinstance(coding, dict)]


def code_of(concept: Any, system: str) -> Optional[str]:
    for coding in codings(concept):
        if coding.get("system") == system and coding.get("code"):
            return str(coding["code"])
    return None


def loinc_codes(concept: Any) -> List[str]:
    return [str(coding["code"]) for coding in codings(concept) if coding.get("system") == LOINC and coding.get("code")]


def concept_text(concept: Any) -> str:
    if not isinstance(concept, dict):
        return ""
    if concept.get("text"):
        return str(concept["text"])
    for coding in codings(concept):
        if coding.get("display") or coding.get("code"):
            return str(coding.get("display") or coding["code"])
    return ""


def reference_id(reference: Any) -> str:
    if not isinstance(reference, dict) or not isinstance(reference.get("reference"), str):
        return ""
    return reference["reference"].rsplit("/", 1)[-1]


def categories(observation: Dict[str, Any]) -> Tuple[str, ...]:
    found = []
    for concept in observation.get("category") or []:
        code = code_of(concept, OBSERVATION_CATEGORY)
        if code:
            found.append(code)
    return tuple(found)


def quantity(holder: Dict[str, Any]) -> Tuple[Optional[float], str, str]:
    value = holder.get("valueQuantity")
    if not isinstance(value, dict) or not isinstance(value.get("value"), (int, float)) or isinstance(value.get("value"), bool):
        return None, "", ""
    return float(value["value"]), str(value.get("unit") or ""), str(value.get("comparator") or "")


def observation_time(observation: Dict[str, Any]) -> Optional[datetime]:
    return parse_time(observation.get("effectiveDateTime") or observation.get("issued"))


def display_value(observation: Dict[str, Any]) -> Tuple[Optional[float], str, str, str]:
    value, unit, comparator = quantity(observation)
    if value is not None:
        return value, f"{comparator}{format_number(value)}", unit, comparator
    if isinstance(observation.get("valueString"), str):
        return None, observation["valueString"], "", ""
    if isinstance(observation.get("valueCodeableConcept"), dict):
        return None, concept_text(observation["valueCodeableConcept"]), "", ""
    components = []
    for component in observation.get("component") or []:
        number, component_unit, _ = quantity(component)
        if number is not None:
            components.append(format_number(number))
            unit = component_unit
    if components:
        return None, "/".join(components[:2]) + (f" ({components[2]})" if len(components) > 2 else ""), unit, ""
    absent = concept_text(observation.get("dataAbsentReason"))
    return None, absent or "not recorded", "", ""


def reading(observation: Dict[str, Any]) -> Reading:
    value, display, unit, comparator = display_value(observation)
    codes = loinc_codes(observation.get("code"))
    label = concept_text(observation.get("code")) or "Observation"
    key = codes[0] if codes else label
    return Reading(key, label, value, display, unit, observation_time(observation), codes[0] if codes else None,
                   categories(observation), comparator)


def component_value(observation: Dict[str, Any], code: str) -> Optional[float]:
    for component in observation.get("component") or []:
        if code in loinc_codes(component.get("code")):
            return quantity(component)[0]
    return None


def vital_value(observation: Dict[str, Any], vital: Vital) -> Optional[float]:
    if not set(vital.codes) & set(loinc_codes(observation.get("code"))):
        return None
    if vital.component:
        return component_value(observation, vital.component)
    return quantity(observation)[0]


def latest_vitals(observations: Iterable[Dict[str, Any]]) -> Dict[str, Reading]:
    latest: Dict[str, Reading] = {}
    for observation in observations:
        when = observation_time(observation)
        for vital in VITALS:
            value = vital_value(observation, vital)
            if value is None:
                continue
            current = latest.get(vital.key)
            if current is None or (when is not None and (current.when is None or when > current.when)):
                latest[vital.key] = Reading(vital.key, vital.label, value, format_number(value), vital.unit, when)
    return latest


def worst_variant(readings: Dict[str, Reading], keys: Sequence[str] = ()) -> str:
    worst = NORMAL
    for key, item in readings.items():
        if keys and key not in keys:
            continue
        variant = VITALS_BY_KEY[key].variant(item.value)
        if SEVERITY[variant] > SEVERITY[worst]:
            worst = variant
    return worst


def series(observations: Iterable[Dict[str, Any]], vital: Vital, maximum: int = 300) -> List[Tuple[datetime, float]]:
    points = []
    for observation in observations:
        when, value = observation_time(observation), vital_value(observation, vital)
        if when is not None and value is not None:
            points.append((when, value))
    points.sort(key=lambda point: point[0])
    if len(points) > maximum:
        step = len(points) / maximum
        points = [points[int(index * step)] for index in range(maximum - 1)] + [points[-1]]
    return points


def lab_rows(observations: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Reading]] = {}
    for observation in observations:
        item = reading(observation)
        if item.when is not None:
            grouped.setdefault(item.key, []).append(item)
    rows = []
    for key, items in grouped.items():
        items.sort(key=lambda item: item.when, reverse=True)
        newest = items[0]
        previous = next((item for item in items[1:] if item.value is not None), None)
        interval = LAB_RANGES.get(newest.code or "")
        variant = interval.variant(newest.value) if interval else NORMAL
        change = ""
        if previous is not None and newest.value is not None and previous.value is not None:
            delta = newest.value - previous.value
            change = "→" if abs(delta) < 1e-9 else f"{'↑' if delta > 0 else '↓'} {format_number(abs(delta))}"
        rows.append({
            "key": key,
            "label": newest.label,
            "value": newest.display,
            "number": newest.value,
            "unit": newest.unit,
            "when": newest.when,
            "reference": interval.text() if interval else "",
            "variant": variant,
            "flag": FLAG_WORDS[variant] if interval and newest.value is not None else "",
            "change": change,
            "history": [(item.when, item.value) for item in reversed(items) if item.value is not None],
        })
    rows.sort(key=lambda row: (-SEVERITY[row["variant"]], KEY_LABS.index(row["key"]) if row["key"] in KEY_LABS else 99, row["label"].lower()))
    return rows


def age_text(observations: Iterable[Dict[str, Any]]) -> str:
    for observation in observations:
        if "30525-0" in loinc_codes(observation.get("code")):
            value, _, comparator = quantity(observation)
            if value is not None:
                return f"{'over ' if comparator == '>' else ''}{format_number(value)}-year-old"
    return ""


def encounter_period(encounter: Dict[str, Any]) -> Tuple[Optional[datetime], Optional[datetime]]:
    period = encounter.get("actualPeriod") or {}
    return parse_time(period.get("start")), parse_time(period.get("end"))


def encounter_class(encounter: Dict[str, Any]) -> str:
    for concept in encounter.get("class") or []:
        for coding in codings(concept):
            if coding.get("code"):
                return str(coding["code"])
    return ""


def encounter_unit(encounter: Dict[str, Any]) -> str:
    types = encounter.get("type") or []
    return concept_text(types[0]) if types else ""


def admission_reason(encounter: Dict[str, Any]) -> str:
    for reason in encounter.get("reason") or []:
        for value in reason.get("value") or []:
            text = concept_text(value.get("concept"))
            if text:
                return text
    return ""


def medication_text(resource: Dict[str, Any]) -> str:
    medication = resource.get("medication") or {}
    return concept_text(medication.get("concept")) or reference_id(medication.get("reference")) or "Unspecified medication"


def dosage_text(resource: Dict[str, Any]) -> str:
    dosage = resource.get("dosageInstruction") or resource.get("dosage") or []
    first = dosage[0] if isinstance(dosage, list) and dosage else dosage if isinstance(dosage, dict) else {}
    return str(first.get("text") or "")


def timeline_events(resources: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for resource in resources:
        kind = resource.get("resourceType")
        if kind == "Encounter":
            start, end = encounter_period(resource)
            place = encounter_unit(resource) or ("Hospital stay" if encounter_class(resource) == "IMP" else "Encounter")
            if start:
                events.append({"when": start, "title": f"Admitted: {place}", "description": admission_reason(resource), "variant": "info"})
            if end:
                disposition = concept_text((resource.get("admission") or {}).get("dischargeDisposition"))
                events.append({"when": end, "title": f"Discharged: {place}", "description": disposition, "variant": "success"})
        elif kind == "Condition":
            when = parse_time(resource.get("recordedDate"))
            if when:
                events.append({"when": when, "title": f"Diagnosis: {concept_text(resource.get('code'))}", "description": "", "variant": "warning"})
        elif kind == "Procedure":
            when = parse_time((resource.get("occurrencePeriod") or {}).get("start") or resource.get("occurrenceDateTime"))
            if when:
                events.append({"when": when, "title": f"Treatment: {concept_text(resource.get('code'))}", "description": "", "variant": "default"})
        elif kind == "MedicationRequest":
            when = parse_time(resource.get("authoredOn"))
            if when:
                events.append({"when": when, "title": f"Order: {medication_text(resource)}", "description": dosage_text(resource), "variant": "default"})
        elif kind == "Observation":
            item = reading(resource)
            interval = LAB_RANGES.get(item.code or "")
            if item.when and interval and interval.variant(item.value) == CRITICAL:
                events.append({
                    "when": item.when, "title": f"Critical result: {item.label} {item.display} {item.unit}".strip(),
                    "description": f"Reference {interval.text()}", "variant": "error",
                })
    events.sort(key=lambda event: event["when"], reverse=True)
    return events
