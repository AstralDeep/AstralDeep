"""Read-only tools of the FHIR agent: ICU census, patient overview, vital trends, laboratory
results, medication review, timeline, record search, source status and a live activity feed.
Each tool queries the configured server through client.py, shapes the data with clinical.py
and returns one presentation.py card plus a compact summary for the model.
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter, deque
from datetime import datetime, timedelta
from functools import wraps
from typing import Any, AsyncIterator, Callable, Deque, Dict, List, Optional, Tuple

from shared.feature_flags import flags
from shared.stream_sdk import StreamComponents, get_stream_metadata, streaming_tool

from agents.fhir import clinical, presentation
from agents.fhir.client import RESOURCE_TYPES, FhirClient, FhirError, bundle_resources, load_settings

IDENTIFIER = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")
PARAMETER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:\-]{0,63}$")
CHANNEL = {"system": "http://terminology.hl7.org/CodeSystem/subscription-channel-type", "code": "websocket"}
POLL_SECONDS = 10.0
SECONDS_PER_MINUTE = 60.0
MAX_WATCH_MINUTES = 10
EVENT_BATCH = 200
CENSUS_REASON_LENGTH = 40
LISTED_PARAMETERS = 6
MAX_TREND_READINGS = 6000
MAX_ADMINISTRATIONS = 3000
ORDER_ROWS = 15
LIVE_POLL_SECONDS = 15.0
LIVE_WINDOW_HOURS = 2
LIVE_READINGS = 1500
MAX_LIVE_MINUTES = 15
PROGRESS_SECONDS = 15
CANVAS_NOTE = (
    "A card with this information is already on the user's canvas. Reply with one or two plain sentences; "
    "do not repeat the data as a table and do not output UI components or JSON."
)


def tool_guard(function: Callable[..., Dict[str, Any]]) -> Callable[..., Dict[str, Any]]:
    @wraps(function)
    def guarded(*args: Any, **kwargs: Any) -> Dict[str, Any]:
        try:
            return function(*args, **kwargs)
        except FhirError as error:
            return error.as_tool_error()

    return guarded


def connect() -> FhirClient:
    return FhirClient(load_settings())


def result(card: Any, data: Dict[str, Any]) -> Dict[str, Any]:
    return {"_ui_components": [card.to_dict()], "_data": {**data, "presentation": CANVAS_NOTE}}


def live_available() -> bool:
    try:
        return bool(flags.is_enabled("tool_streaming") and flags.is_enabled("stream_progress"))
    except Exception:  # noqa: BLE001
        return False


def patient_identifier(value: Any) -> str:
    text = str(value or "").strip()
    text = text.split("/", 1)[1] if text.lower().startswith("patient/") else text
    if not IDENTIFIER.match(text):
        raise FhirError("FHIR_BAD_REQUEST", "A patient identifier such as 002-10145 is required")
    return text


def bounded(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def server_time(client: FhirClient) -> datetime:
    try:
        status = client.get("$replay-status", expected="Parameters")
    except FhirError:
        return datetime.now().astimezone()
    for parameter in status.get("parameter") or []:
        if parameter.get("name") == "now":
            moment = clinical.parse_time(parameter.get("valueInstant"))
            if moment is not None:
                return moment
    return datetime.now().astimezone()


def since(now: datetime, hours: int) -> str:
    return "ge" + (now - timedelta(hours=hours)).isoformat(timespec="seconds")


def loinc_tokens(codes: Tuple[str, ...]) -> str:
    return ",".join(f"{clinical.LOINC}|{code}" for code in codes)


def iso(moment: Optional[datetime]) -> Optional[str]:
    return moment.isoformat(timespec="seconds") if moment else None


def reading_text(item: Optional[clinical.Reading]) -> str:
    return item.display if item else "—"


def most_recent(shown: int, total: Optional[int], noun: str) -> str:
    return f"Showing the most recent {shown:,} of {total:,} {noun}." if total is not None and total > shown else ""


@tool_guard
def icu_census(limit: int = 15, unit: str = "", **_: Any) -> Dict[str, Any]:
    client = connect()
    now = server_time(client)
    limit = bounded(limit, 15, 1, 40)
    encounters, _total = client.search("Encounter", [("status", "in-progress"), ("class", "ACUTE"), ("_sort", "-date")], 500)
    wanted = unit.strip().lower()
    if wanted:
        encounters = [encounter for encounter in encounters if wanted in clinical.encounter_unit(encounter).lower()]
    units = Counter(clinical.encounter_unit(encounter) or "Unspecified" for encounter in encounters).most_common()
    recent, _ = client.search("Encounter", [("class", "ACUTE"), ("date-start", since(now, 24)), ("_sort", "date")], 500)
    buckets: Counter = Counter()
    admitted_last_hour = 0
    for encounter in recent:
        start, _end = clinical.encounter_period(encounter)
        if start is None:
            continue
        buckets[start.replace(minute=0, second=0, microsecond=0)] += 1
        if now - start <= timedelta(hours=1):
            admitted_last_hour += 1
    admissions = sorted(buckets.items())
    rows = []
    for encounter in encounters[:limit]:
        patient = clinical.reference_id(encounter.get("subject"))
        start, _end = clinical.encounter_period(encounter)
        observations = client.latest([("patient", patient), ("code", loinc_tokens(("8867-4", "59408-5", "85354-9")))])
        latest = clinical.latest_vitals(observations)
        rows.append({
            "patient": patient,
            "encounter": encounter.get("id", ""),
            "unit": clinical.encounter_unit(encounter),
            "admitted": clinical.format_time(start),
            "stay": clinical.duration((now - start).total_seconds() / 60) if start else "",
            "reason": clinical.shorten(clinical.admission_reason(encounter), CENSUS_REASON_LENGTH),
            "heart_rate": reading_text(latest.get("heart_rate")),
            "oxygen_saturation": reading_text(latest.get("oxygen_saturation")),
            "mean_arterial_pressure": reading_text(latest.get("mean_arterial_pressure")),
            "flag": clinical.FLAG_WORDS[clinical.worst_variant(latest, clinical.CENSUS_VITALS)] if latest else "",
        })
    card = presentation.census_card(now, len(encounters), rows, units, admissions, admitted_last_hour, live_available())
    return result(card, {
        "as_of": iso(now),
        "patients_in_icu": len(encounters),
        "admitted_last_hour": admitted_last_hour,
        "by_unit_type": dict(units),
        "most_recently_admitted": rows,
    })


def current_stay(encounters: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    units = [encounter for encounter in encounters if clinical.encounter_class(encounter) == "ACUTE"]
    active = [encounter for encounter in units if encounter.get("status") == "in-progress"]
    return (active or units or encounters or [None])[0]


def first_reading(observations: List[Dict[str, Any]], code: str) -> str:
    for observation in observations:
        if code in clinical.loinc_codes(observation.get("code")):
            item = clinical.reading(observation)
            return f"{item.display} {item.unit}".strip()
    return ""


@tool_guard
def patient_overview(patient: str = "", **_: Any) -> Dict[str, Any]:
    identifier = patient_identifier(patient)
    client = connect()
    now = server_time(client)
    record = client.read("Patient", identifier)
    encounters, _ = client.search("Encounter", [("patient", identifier), ("_sort", "-date")], 30)
    observations = client.latest([("patient", identifier)])
    conditions, _ = client.search("Condition", [("patient", identifier), ("_sort", "-recorded-date")], 40)
    orders, _ = client.search("MedicationRequest", [("patient", identifier), ("status", "active"), ("_sort", "-authoredon")], 40)
    allergies, _ = client.search("AllergyIntolerance", [("patient", identifier)], 20)
    assessments, _ = client.search("RiskAssessment", [("patient", identifier), ("_sort", "-date")], 4)

    stay = current_stay(encounters)
    start, end = clinical.encounter_period(stay) if stay else (None, None)
    latest = clinical.latest_vitals(observations)
    age = clinical.age_text(observations)
    gender = str(record.get("gender") or "")
    deceased = clinical.parse_time(record.get("deceasedDateTime"))
    in_unit = bool(stay and stay.get("status") == "in-progress")
    unit = clinical.encounter_unit(stay) if stay else ""
    reason = clinical.admission_reason(stay) if stay else ""
    headline = " ".join(part for part in (age, gender) if part) or "Demographics not recorded"
    if start:
        headline += f" · admitted {clinical.format_time(start)}"
    badges = ["Deceased" if deceased else "In ICU now" if in_unit else "ICU stay ended"]
    worst = clinical.worst_variant(latest)
    if latest:
        badges.append(f"Vitals: {clinical.FLAG_WORDS[worst]}")
    if reason:
        badges.append(reason)

    stay_minutes = ((end or now) - start).total_seconds() / 60 if start else None
    admission = (stay or {}).get("admission") or {}
    facts = [
        {"label": "Age", "value": age.replace("-year-old", " years") or "not recorded"},
        {"label": "Sex", "value": gender or "not recorded"},
        {"label": "Height", "value": first_reading(observations, "8302-2") or "not recorded"},
        {"label": "Weight", "value": first_reading(observations, "29463-7") or "not recorded"},
        {"label": "Unit", "value": unit or "not recorded"},
        {"label": "Hospital", "value": clinical.reference_label(record.get("managingOrganization")) or "not recorded"},
        {"label": "Admitted", "value": clinical.format_time(start)},
        {"label": "Time in unit", "value": clinical.duration(stay_minutes) if stay_minutes is not None else "not recorded"},
        {"label": "Admitted from", "value": clinical.concept_text(admission.get("admitSource")) or "not recorded"},
    ]
    if end:
        facts.append({"label": "Discharged to", "value": clinical.concept_text(admission.get("dischargeDisposition")) or "not recorded"})
    if deceased:
        facts.append({"label": "Died", "value": clinical.format_time(deceased)})

    risks: List[Tuple[str, float]] = []
    preferred = sorted(assessments, key=lambda item: "IVa" not in clinical.concept_text(item.get("method")))
    for prediction in (preferred[0].get("prediction") or []) if preferred else []:
        probability = prediction.get("probabilityDecimal")
        if isinstance(probability, (int, float)) and not isinstance(probability, bool):
            risks.append((clinical.concept_text(prediction.get("outcome")) or "Risk", float(probability)))

    seen: set = set()
    problems = []
    for condition in conditions:
        label = clinical.sentence_case(clinical.concept_text(condition.get("code")))
        if not label or label.lower() in seen:
            continue
        seen.add(label.lower())
        status = clinical.status_text(clinical.concept_text(condition.get("clinicalStatus")))
        problems.append([label, status, clinical.format_time(clinical.parse_time(condition.get("recordedDate")))])
    medications = clinical.distinct(
        [clinical.medication_text(order), clinical.dosage_text(order), clinical.format_time(clinical.parse_time(order.get("authoredOn")))]
        for order in orders
    )
    laboratory = [item for item in observations if "laboratory" in clinical.categories(item)]
    labs = [row for row in clinical.lab_rows(laboratory) if row["key"] in clinical.KEY_LABS or row["flag"] in ("Critical", "Moderate")]
    allergy_names = sorted({clinical.sentence_case(clinical.concept_text(item.get("code"))) for item in allergies} - {""})

    card = presentation.patient_card(
        identifier, headline, unit or "Intensive care", badges, latest, now, facts, risks,
        problems[:8], medications[:8], labs[:10], allergy_names, live_available(),
    )
    return result(card, {
        "patient": identifier,
        "as_of": iso(now),
        "age": age,
        "sex": gender,
        "in_icu_now": in_unit,
        "deceased": iso(deceased),
        "unit": unit,
        "admission_diagnosis": reason,
        "admitted": iso(start),
        "discharged": iso(end),
        "vitals_flag": clinical.FLAG_WORDS[worst] if latest else None,
        "latest_vitals": {
            key: {"value": item.value, "unit": item.unit, "time": iso(item.when)} for key, item in latest.items()
        },
        "predicted_risk": {label: probability for label, probability in risks},
        "problems": [row[0] for row in problems[:12]],
        "active_medications": list(dict.fromkeys(row[0] for row in medications))[:12],
        "allergies": allergy_names,
        "key_labs": [
            {"test": row["label"], "value": row["number"], "unit": row["unit"], "flag": row["flag"]} for row in labs[:10]
        ],
    })


@tool_guard
def vital_sign_trends(patient: str = "", hours: int = 24, **_: Any) -> Dict[str, Any]:
    identifier = patient_identifier(patient)
    hours = bounded(hours, 24, 1, 168)
    client = connect()
    now = server_time(client)
    observations, total = client.search(
        "Observation",
        [("patient", identifier), ("code", loinc_tokens(clinical.VITAL_CODES)), ("date", since(now, hours)), ("_sort", "-date")],
        MAX_TREND_READINGS,
    )
    latest = clinical.latest_vitals(observations)
    traces: Dict[str, List[Tuple[datetime, float]]] = {}
    summary = []
    data: Dict[str, Any] = {}
    for vital in clinical.VITALS:
        points = clinical.series(observations, vital)
        if not points:
            continue
        traces[vital.key] = clinical.thin(points)
        values = [value for _, value in points]
        newest = latest[vital.key]
        low, high = clinical.format_number(min(values)), clinical.format_number(max(values))
        spread = "" if len(values) == 1 else f"Steady at {low} · " if low == high else f"Range {low}–{high} · "
        summary.append({
            "label": vital.label,
            "value": f"{newest.display} {vital.unit}".strip(),
            "hint": spread + clinical.count_of(len(values), "reading"),
            "variant": vital.variant(newest.value),
        })
        data[vital.key] = {
            "latest": newest.value, "minimum": min(values), "maximum": max(values),
            "mean": round(sum(values) / len(values), 2), "readings": len(values), "unit": vital.unit,
            "latest_time": iso(newest.when),
        }
    note = most_recent(len(observations), total, "readings")
    card = presentation.vitals_card(identifier, hours, now, summary, traces, note, live_available())
    return result(card, {
        "patient": identifier, "as_of": iso(now), "window_hours": hours, "vitals": data, "complete": not note,
    })


@tool_guard
def laboratory_results(patient: str = "", hours: int = 72, **_: Any) -> Dict[str, Any]:
    identifier = patient_identifier(patient)
    hours = bounded(hours, 72, 1, 720)
    client = connect()
    now = server_time(client)
    observations, _ = client.search(
        "Observation",
        [("patient", identifier), ("category", "laboratory"), ("date", since(now, hours)), ("_sort", "-date")],
        2000,
    )
    rows = clinical.lab_rows(observations)
    card = presentation.labs_card(identifier, hours, now, rows[:40])
    return result(card, {
        "patient": identifier,
        "as_of": iso(now),
        "window_hours": hours,
        "tests": len(rows),
        "results": [
            {
                "test": row["label"], "value": row["value"], "unit": row["unit"], "flag": row["flag"],
                "reference": row["reference"], "collected": iso(row["when"]),
            }
            for row in rows[:40]
        ],
    })


@tool_guard
def medication_review(patient: str = "", **_: Any) -> Dict[str, Any]:
    identifier = patient_identifier(patient)
    client = connect()
    now = server_time(client)
    orders, _ = client.search("MedicationRequest", [("patient", identifier), ("_sort", "-authoredon")], 300)
    administrations, charted = client.search(
        "MedicationAdministration", [("patient", identifier), ("_sort", "-date")], MAX_ADMINISTRATIONS,
    )
    statements, _ = client.search("MedicationStatement", [("patient", identifier)], 100)
    counts = Counter(str(order.get("status") or "unknown") for order in orders)
    ranked = sorted(orders, key=lambda order: order.get("status") != "active")
    order_rows = clinical.distinct(
        [clinical.medication_text(order), clinical.dosage_text(order), clinical.status_text(order.get("status")),
         clinical.format_time(clinical.parse_time(order.get("authoredOn")))]
        for order in ranked
    )
    listed = order_rows[:ORDER_ROWS]
    active = list(dict.fromkeys(clinical.medication_text(order) for order in ranked if order.get("status") == "active"))
    series: Dict[str, List[Tuple[datetime, float]]] = {}
    units: Dict[str, str] = {}
    for administration in administrations:
        rate = (administration.get("dosage") or {}).get("rateQuantity") or {}
        when = clinical.parse_time(administration.get("occurenceDateTime"))
        value = rate.get("value")
        if when is None or not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        label = clinical.medication_text(administration)
        series.setdefault(label, []).append((when, float(value)))
        units.setdefault(label, str(rate.get("unit") or ""))
    busiest = {label: sorted(points) for label, points in sorted(series.items(), key=lambda item: len(item[1]), reverse=True)[:6]}
    home = [[clinical.sentence_case(clinical.medication_text(statement)), clinical.dosage_text(statement)] for statement in statements]
    notes = {
        "orders": f"Showing {len(listed)} of {len(order_rows)} orders, active and most recent first." if len(order_rows) > len(listed) else "",
        "infusions": most_recent(len(administrations), charted, "charted doses"),
    }
    card = presentation.medications_card(
        identifier, now, dict(counts), listed, {label: clinical.thin(points) for label, points in busiest.items()}, units, home, notes,
    )
    return result(card, {
        "patient": identifier,
        "as_of": iso(now),
        "order_counts": dict(counts),
        "active_orders": active[:40],
        "infusions": {label: {"readings": len(points), "latest_rate": points[-1][1], "unit": units.get(label, "")} for label, points in busiest.items()},
        "home_medications": [row[0] for row in home],
    })


@tool_guard
def patient_timeline(patient: str = "", hours: int = 48, **_: Any) -> Dict[str, Any]:
    identifier = patient_identifier(patient)
    hours = bounded(hours, 48, 1, 720)
    client = connect()
    now = server_time(client)
    start = since(now, hours)
    resources: List[Dict[str, Any]] = []
    for resource_type, parameters, limit in (
        ("Encounter", [("patient", identifier)], 30),
        ("Condition", [("patient", identifier), ("recorded-date", start), ("_sort", "-recorded-date")], 100),
        ("Procedure", [("patient", identifier), ("date", start), ("_sort", "-date")], 100),
        ("MedicationRequest", [("patient", identifier), ("authoredon", start), ("_sort", "-authoredon")], 200),
        ("Observation", [("patient", identifier), ("category", "laboratory"), ("date", start), ("_sort", "-date")], 1000),
    ):
        found, _ = client.search(resource_type, parameters, limit)
        resources += found
    floor = now - timedelta(hours=hours)
    events = [event for event in clinical.timeline_events(resources) if floor <= event["when"] <= now]
    card = presentation.timeline_card(identifier, hours, now, events[:40], len(events))
    return result(card, {
        "patient": identifier,
        "as_of": iso(now),
        "window_hours": hours,
        "event_count": len(events),
        "events": [{"time": iso(event["when"]), "event": event["title"], "detail": event["description"]} for event in events[:40]],
    })


@tool_guard
def fhir_source_status(**_: Any) -> Dict[str, Any]:
    client = connect()
    statement = client.get("metadata", expected="CapabilityStatement")
    replay: Dict[str, Any] = {}
    try:
        for parameter in client.get("$replay-status", expected="Parameters").get("parameter") or []:
            replay[str(parameter.get("name"))] = next((value for key, value in parameter.items() if key != "name"), None)
    except FhirError:
        replay = {}
    rest = (statement.get("rest") or [{}])[0]
    resources = []
    for entry in rest.get("resource") or []:
        interactions = ", ".join(str(item.get("code")) for item in entry.get("interaction") or [])
        names = [str(item.get("name")) for item in entry.get("searchParam") or []]
        parameters = ", ".join(names[:LISTED_PARAMETERS])
        if len(names) > LISTED_PARAMETERS:
            parameters += f" and {len(names) - LISTED_PARAMETERS} more"
        resources.append([str(entry.get("type")), interactions, parameters])
    stats = [
        {"label": label, "value": f"{replay[key]:,}"}
        for label, key in (("Patients", "patients"), ("ICU stays", "icuStays"), ("In ICU now", "activeIcuEncounters"), ("Observations", "observations"))
        if isinstance(replay.get(key), int)
    ]
    facts = [
        {"label": "FHIR version", "value": str(statement.get("fhirVersion") or "unknown")},
        {"label": "Software", "value": " ".join(str(part) for part in ((statement.get("software") or {}).get("name"), (statement.get("software") or {}).get("version")) if part) or "unknown"},
        {"label": "Address", "value": client.settings.base_url},
    ]
    for label, key in (("Server time", "now"), ("Time zone", "timezone"), ("Replay cycle (days)", "cycleDays"), ("Dataset", "dataset"), ("Definitions", "definitionsSource"), ("Definitions tag", "definitionsTag")):
        if replay.get(key) is not None:
            moment = clinical.parse_time(replay[key]) if key == "now" else None
            facts.append({"label": label, "value": clinical.format_time(moment) if moment else str(replay[key])})
    badges = [f"FHIR {statement.get('fhirVersion')}", f"{len(resources)} resource types"]
    card = presentation.source_card(
        str(statement.get("title") or statement.get("name") or "FHIR server"),
        str(statement.get("description") or "")[:240],
        badges, stats, facts, resources,
    )
    return result(card, {
        "fhir_version": statement.get("fhirVersion"),
        "title": statement.get("title"),
        "resource_types": [row[0] for row in resources],
        "replay": replay,
    })


def record_row(resource: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    kind = resource.get("resourceType")
    patient = clinical.reference_id(resource.get("subject") or resource.get("patient"))
    if kind == "Patient":
        return ["Patient", "Sex", "Born", "Died"], [
            str(resource.get("id", "")), str(resource.get("gender", "")), str(resource.get("birthDate", "")),
            clinical.format_time(clinical.parse_time(resource.get("deceasedDateTime"))) if resource.get("deceasedDateTime") else "",
        ]
    if kind == "Encounter":
        start, end = clinical.encounter_period(resource)
        return ["Encounter", "Patient", "Status", "Unit", "Start", "End"], [
            str(resource.get("id", "")), patient, clinical.status_text(resource.get("status")), clinical.encounter_unit(resource),
            clinical.format_time(start), clinical.format_time(end) if end else "",
        ]
    if kind == "Observation":
        item = clinical.reading(resource)
        return ["Time", "Patient", "Measurement", "Value"], [
            clinical.format_time(item.when), patient, item.label, f"{item.display} {item.unit}".strip(),
        ]
    if kind == "Condition":
        return ["Recorded", "Patient", "Condition", "Status"], [
            clinical.format_time(clinical.parse_time(resource.get("recordedDate"))), patient,
            clinical.sentence_case(clinical.concept_text(resource.get("code"))),
            clinical.status_text(clinical.concept_text(resource.get("clinicalStatus"))),
        ]
    if kind == "Procedure":
        return ["Started", "Patient", "Treatment", "Status"], [
            clinical.format_time(clinical.parse_time((resource.get("occurrencePeriod") or {}).get("start"))), patient,
            clinical.sentence_case(clinical.concept_text(resource.get("code"))), clinical.status_text(resource.get("status")),
        ]
    if kind == "AllergyIntolerance":
        return ["Recorded", "Patient", "Allergy"], [
            clinical.format_time(clinical.parse_time(resource.get("recordedDate"))), patient,
            clinical.sentence_case(clinical.concept_text(resource.get("code"))),
        ]
    if kind in ("MedicationRequest", "MedicationStatement", "MedicationAdministration"):
        when = resource.get("authoredOn") or resource.get("dateAsserted") or resource.get("occurenceDateTime")
        return ["Time", "Patient", "Medication", "Dose", "Status"], [
            clinical.format_time(clinical.parse_time(when)), patient, clinical.medication_text(resource),
            clinical.dosage_text(resource), clinical.status_text(resource.get("status")),
        ]
    if kind == "RiskAssessment":
        predictions = "; ".join(
            f"{clinical.concept_text(item.get('outcome'))} {float(item['probabilityDecimal']) * 100:.1f}%"
            for item in resource.get("prediction") or [] if isinstance(item.get("probabilityDecimal"), (int, float))
        )
        return ["Time", "Patient", "Method", "Predictions"], [
            clinical.format_time(clinical.parse_time(resource.get("occurrenceDateTime"))), patient,
            clinical.concept_text(resource.get("method")), predictions,
        ]
    return ["Record", "Label", "Details"], [
        str(resource.get("id", "")), str(resource.get("name", "")), str(resource.get("description", "")),
    ]


@tool_guard
def query_fhir_records(resource_type: str = "", filters: Optional[Dict[str, Any]] = None, limit: int = 20, **_: Any) -> Dict[str, Any]:
    wanted = next((name for name in RESOURCE_TYPES if name.lower() == str(resource_type).strip().lower()), None)
    if wanted is None:
        raise FhirError("FHIR_BAD_REQUEST", "resource_type must be one of " + ", ".join(RESOURCE_TYPES))
    limit = bounded(limit, 20, 1, 100)
    parameters: List[Tuple[str, str]] = []
    for key, value in (filters or {}).items():
        text = str(value).strip() if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""
        if not PARAMETER.match(str(key)) or str(key) in ("_format", "_count") or not text or len(text) > 256:
            raise FhirError("FHIR_BAD_REQUEST", f"Search filter '{str(key)[:40]}' is not usable")
        parameters.append((str(key), text))
    resources, total = connect().search(wanted, parameters, limit)
    headers: List[str] = []
    rows = []
    for resource in resources:
        headers, row = record_row(resource)
        rows.append(row)
    query = " · ".join(f"{key}={value}" for key, value in parameters)
    card = presentation.records_card(wanted, headers, rows, total, query)
    return result(card, {
        "resource_type": wanted,
        "filters": dict(parameters),
        "total": total,
        "returned": len(rows),
        "columns": headers,
        "rows": rows,
    })


class FeedState:
    def __init__(self) -> None:
        self.counters: Dict[str, int] = {"events": 0, "admissions": 0, "discharges": 0, "observations": 0, "flagged": 0}
        self.events: Deque[Dict[str, Any]] = deque(maxlen=12)
        self.recent: Deque[List[str]] = deque(maxlen=8)

    def absorb(self, resource: Dict[str, Any], created: bool) -> None:
        self.counters["events"] += 1
        kind = resource.get("resourceType")
        patient = clinical.reference_id(resource.get("subject"))
        if kind == "Encounter" and clinical.encounter_class(resource) == "ACUTE":
            start, end = clinical.encounter_period(resource)
            place = clinical.encounter_unit(resource) or "ICU"
            if created and start:
                self.counters["admissions"] += 1
                self.events.appendleft({
                    "when": start, "title": f"Admitted to {place}: patient {patient}",
                    "description": clinical.admission_reason(resource), "variant": "info",
                })
            elif end:
                self.counters["discharges"] += 1
                destination = clinical.concept_text((resource.get("admission") or {}).get("dischargeDisposition"))
                self.events.appendleft({
                    "when": end, "title": f"Left {place}: patient {patient}", "description": destination, "variant": "success",
                })
        elif kind == "Observation":
            self.counters["observations"] += 1
            item = clinical.reading(resource)
            self.recent.appendleft([clinical.format_time(item.when), patient, item.label, f"{item.display} {item.unit}".strip()])
            flagged = [
                (vital, value) for vital in clinical.VITALS
                for value in [clinical.vital_value(resource, vital)]
                if value is not None and vital.variant(value) == clinical.CRITICAL
            ]
            interval = clinical.LAB_RANGES.get(item.code or "")
            if interval and interval.variant(item.value) == clinical.CRITICAL:
                flagged.append((None, item.value))
            for vital, value in flagged[:1]:
                self.counters["flagged"] += 1
                label = vital.label if vital else item.label
                unit = vital.unit if vital else item.unit
                self.events.appendleft({
                    "when": item.when, "title": f"Flagged: {label} {clinical.format_number(value)} {unit}".strip() + f" for patient {patient}",
                    "description": "", "variant": "error",
                })

    def card(self, now: Optional[datetime], watching: bool) -> Dict[str, Any]:
        return presentation.feed_card(now, watching, dict(self.counters), list(self.events), list(self.recent)).to_dict()

    def summary(self, now: Optional[datetime], watching: bool) -> Dict[str, Any]:
        return {
            "as_of": iso(now),
            "watching": watching,
            **self.counters,
            "notable_events": [{"time": iso(event["when"]), "event": event["title"]} for event in self.events],
        }


def open_subscriptions(client: FhirClient) -> List[str]:
    topics = {str(topic.get("id")): str(topic.get("url")) for topic in bundle_resources(client.bundle("SubscriptionTopic"))}
    identifiers = []
    for name in ("encounter", "observation"):
        if name not in topics:
            raise FhirError("FHIR_BAD_REQUEST", "The FHIR server does not publish the subscription topics this feed needs")
        created = client.post("Subscription", {
            "resourceType": "Subscription", "status": "requested", "topic": topics[name], "channelType": CHANNEL,
            "content": "full-resource", "maxCount": EVENT_BATCH,
        }, expected="Subscription")
        identifiers.append(str(created.get("id")))
    return identifiers


def close_subscriptions(client: FhirClient, identifiers: List[str]) -> None:
    for identifier in identifiers:
        try:
            client.delete(f"Subscription/{identifier}")
        except FhirError:
            continue


def drain(client: FhirClient, identifier: str, after: int, state: FeedState) -> int:
    for _ in range(5):
        bundle = client.bundle(f"Subscription/{identifier}/$events", [("eventsSinceNumber", str(after + 1))])
        entries = bundle.get("entry") or []
        status = (entries[0].get("resource") or {}) if entries else {}
        numbers = [int(event["eventNumber"]) for event in status.get("notificationEvent") or [] if str(event.get("eventNumber", "")).isdigit()]
        for entry in entries[1:]:
            resource = entry.get("resource")
            if isinstance(resource, dict):
                state.absorb(resource, (entry.get("request") or {}).get("method") == "POST")
        if not numbers:
            break
        after = max(numbers)
        if len(numbers) < EVENT_BATCH:
            break
    return after


WATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "minutes": {
            "type": "integer", "minimum": 1, "maximum": MAX_WATCH_MINUTES, "default": 2,
            "description": "How long to keep watching the feed, in minutes (1-10)",
        },
    },
}
WATCH_DESCRIPTION = (
    "WATCH the live ICU activity feed: subscribes to the FHIR server's R5 topic subscriptions and updates one card "
    "in place with admissions, discharges, new results and flagged readings as they arrive. Use this for 'live', "
    "'watch', 'monitor' or 'stream' requests about ICU activity; use icu_census for a one-time snapshot."
)


@streaming_tool(name="watch_icu_activity", description=WATCH_DESCRIPTION, input_schema=WATCH_SCHEMA, max_fps=1, min_fps=1, scope="tools:read")
async def watch_icu_activity(args: Dict[str, Any], credentials: Dict[str, Any]) -> AsyncIterator[StreamComponents]:
    minutes = bounded(args.get("minutes"), 2, 1, MAX_WATCH_MINUTES)
    state = FeedState()
    identifiers: List[str] = []
    client: Optional[FhirClient] = None
    try:
        try:
            client = connect()
            identifiers = await asyncio.to_thread(open_subscriptions, client)
            now = await asyncio.to_thread(server_time, client)
        except FhirError as error:
            yield StreamComponents(
                components=[], terminal=True,
                error={"code": error.code, "message": error.message, "phase": "failed", "retryable": error.retryable},
            )
            return
        yield StreamComponents(components=[state.card(now, True)], raw=state.summary(now, True))
        positions = dict.fromkeys(identifiers, 0)
        deadline = asyncio.get_running_loop().time() + minutes * SECONDS_PER_MINUTE
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(POLL_SECONDS)
            try:
                for identifier in identifiers:
                    positions[identifier] = await asyncio.to_thread(drain, client, identifier, positions[identifier], state)
                now = await asyncio.to_thread(server_time, client)
            except FhirError:
                break
            yield StreamComponents(components=[state.card(now, True)], raw=state.summary(now, True))
        yield StreamComponents(components=[state.card(now, False)], raw=state.summary(now, False), terminal=True)
    finally:
        if client is not None and identifiers:
            await asyncio.to_thread(close_subscriptions, client, identifiers)


PATIENT_PROPERTY = {
    "type": "string",
    "description": "FHIR patient id from the feed, for example 002-10145 (take it from icu_census if unknown)",
}


def live_snapshot(client: FhirClient, identifier: str, started: Optional[datetime], minutes: int, watching: bool) -> Tuple[Dict[str, Any], Dict[str, Any], datetime]:
    now = server_time(client)
    observations, _ = client.search(
        "Observation",
        [("patient", identifier), ("code", loinc_tokens(clinical.VITAL_CODES)), ("date", since(now, LIVE_WINDOW_HOURS)), ("_sort", "-date")],
        LIVE_READINGS,
    )
    latest = clinical.latest_vitals(client.latest([("patient", identifier), ("code", loinc_tokens(clinical.VITAL_CODES))]))
    traces = {vital.key: clinical.thin(points) for vital in clinical.VITALS for points in [clinical.series(observations, vital)] if points}
    began = started or now
    fresh = sum(1 for item in observations for when in [clinical.observation_time(item)] if when is not None and when > began)
    card = presentation.live_vitals_card(identifier, watching, minutes, fresh, latest, now, traces)
    summary = {
        "patient": identifier, "as_of": iso(now), "watching": watching, "new_readings": fresh,
        "latest_vitals": {key: {"value": item.value, "unit": item.unit, "time": iso(item.when)} for key, item in latest.items()},
        "presentation": CANVAS_NOTE,
    }
    return card.to_dict(), summary, began


LIVE_SCHEMA = {
    "type": "object",
    "properties": {
        "patient": PATIENT_PROPERTY,
        "minutes": {
            "type": "integer", "minimum": 1, "maximum": MAX_LIVE_MINUTES, "default": 5,
            "description": "How long to keep streaming, in minutes (1-15)",
        },
    },
    "required": ["patient"],
}
LIVE_DESCRIPTION = (
    "STREAM one patient's vital signs live from the FHIR feed: a card with the latest heart rate, SpO2, respiratory "
    "rate, blood pressure and temperature and their trend over the last two hours that keeps updating in place as "
    "new readings arrive. Use this for 'live', 'stream', 'watch' or 'monitor' requests about one patient's vital "
    "signs; use vital_sign_trends for a one-time chart."
)


@streaming_tool(name="stream_patient_vitals", description=LIVE_DESCRIPTION, input_schema=LIVE_SCHEMA, max_fps=1, min_fps=1, scope="tools:read")
async def stream_patient_vitals(args: Dict[str, Any], credentials: Dict[str, Any]) -> AsyncIterator[StreamComponents]:
    minutes = bounded(args.get("minutes"), 5, 1, MAX_LIVE_MINUTES)
    try:
        identifier = patient_identifier(args.get("patient"))
        client = connect()
        await asyncio.to_thread(client.read, "Patient", identifier)
        card, summary, began = await asyncio.to_thread(live_snapshot, client, identifier, None, minutes, True)
    except FhirError as error:
        yield StreamComponents(
            components=[], terminal=True,
            error={"code": error.code, "message": error.message, "phase": "failed", "retryable": error.retryable},
        )
        return
    yield StreamComponents(components=[card], raw=summary)
    deadline = asyncio.get_running_loop().time() + minutes * SECONDS_PER_MINUTE
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(LIVE_POLL_SECONDS)
        try:
            card, summary, _ = await asyncio.to_thread(live_snapshot, client, identifier, began, minutes, True)
        except FhirError:
            break
        yield StreamComponents(components=[card], raw=summary)
    try:
        card, summary, _ = await asyncio.to_thread(live_snapshot, client, identifier, began, minutes, False)
    except FhirError:
        summary = {**summary, "watching": False}
    yield StreamComponents(components=[card], raw=summary, terminal=True)


def hours_property(default: int, maximum: int) -> Dict[str, Any]:
    return {"type": "integer", "minimum": 1, "maximum": maximum, "default": default, "description": "How many hours back to look"}


TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {
    "icu_census": {
        "function": icu_census,
        "scope": "tools:read",
        "description": (
            "Show who is in intensive care right now according to the connected HL7 FHIR R5 feed: total census, breakdown by "
            "unit type, admissions per hour and the most recently admitted patients with their latest vital signs. Start "
            "here when the user asks about the ICU, the FHIR or HL7 feed, or needs a patient id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 40, "default": 15, "description": "Patients to list"},
                "unit": {"type": "string", "description": "Optional unit type to filter by, for example MICU or Cardiac ICU"},
            },
        },
    },
    "patient_overview": {
        "function": patient_overview,
        "scope": "tools:read",
        "description": (
            "Summarize one patient from the FHIR feed: demographics, current stay, latest vital signs with alert colouring, "
            "APACHE risk predictions, problems, allergies, key laboratory results and active medication orders."
        ),
        "input_schema": {"type": "object", "properties": {"patient": PATIENT_PROPERTY}, "required": ["patient"]},
    },
    "vital_sign_trends": {
        "function": vital_sign_trends,
        "scope": "tools:read",
        "description": (
            "Chart a patient's vital signs over time from the FHIR feed: heart rate, respiratory rate, SpO2, blood pressure "
            "and temperature, with the latest value and range for each."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"patient": PATIENT_PROPERTY, "hours": hours_property(24, 168)},
            "required": ["patient"],
        },
    },
    "laboratory_results": {
        "function": laboratory_results,
        "scope": "tools:read",
        "description": (
            "List a patient's laboratory results from the FHIR feed with reference intervals, flags and the change since "
            "the previous value, and chart the tests that have a history."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"patient": PATIENT_PROPERTY, "hours": hours_property(72, 720)},
            "required": ["patient"],
        },
    },
    "medication_review": {
        "function": medication_review,
        "scope": "tools:read",
        "description": (
            "Review a patient's medication from the FHIR feed: active, completed and cancelled orders, charted infusion "
            "rates over time and medication taken before admission."
        ),
        "input_schema": {"type": "object", "properties": {"patient": PATIENT_PROPERTY}, "required": ["patient"]},
    },
    "patient_timeline": {
        "function": patient_timeline,
        "scope": "tools:read",
        "description": (
            "Show a patient's recent clinical timeline from the FHIR feed: admissions and discharges, diagnoses, "
            "treatments, medication orders and critical laboratory results in time order."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"patient": PATIENT_PROPERTY, "hours": hours_property(48, 720)},
            "required": ["patient"],
        },
    },
    "query_fhir_records": {
        "function": query_fhir_records,
        "scope": "tools:search",
        "description": (
            "Run a read-only FHIR R5 search against the connected feed and tabulate the matches. Use it for questions the "
            "other tools do not cover, such as patients who died (resource_type Patient, filters {\"deceased\": \"true\"}) "
            "or encounters in one ward. resource_type is one of " + ", ".join(RESOURCE_TYPES) + "."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "resource_type": {"type": "string", "description": "FHIR resource type to search"},
                "filters": {
                    "type": "object",
                    "description": "FHIR search parameters, for example {\"patient\": \"002-10145\", \"status\": \"active\"}",
                    "additionalProperties": {"type": "string"},
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20, "description": "Rows to return"},
            },
            "required": ["resource_type"],
        },
    },
    "fhir_source_status": {
        "function": fhir_source_status,
        "scope": "tools:read",
        "description": (
            "Describe the connected HL7 FHIR server: FHIR version, what it serves, its replay clock and dataset, and which "
            "resource types and search parameters can be queried."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    "watch_icu_activity": {
        "function": watch_icu_activity,
        "scope": "tools:read",
        "description": WATCH_DESCRIPTION,
        "input_schema": WATCH_SCHEMA,
        "metadata": {**get_stream_metadata(watch_icu_activity)["metadata"], "persist_progress_s": PROGRESS_SECONDS},
    },
    "stream_patient_vitals": {
        "function": stream_patient_vitals,
        "scope": "tools:read",
        "description": LIVE_DESCRIPTION,
        "input_schema": LIVE_SCHEMA,
        "metadata": {**get_stream_metadata(stream_patient_vitals)["metadata"], "persist_progress_s": PROGRESS_SECONDS},
    },
}
