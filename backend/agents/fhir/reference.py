"""Validates reference FHIR patient measurements and bounded population summaries.
mcp_tools.py uses these helpers with client.py and renders their verified fields through presentation.py.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from agents.fhir import clinical
from agents.fhir.client import FhirClient, FhirError

MEASURES = {
    "a1c": ("Hemoglobin A1C", ("4548-4", "17856-6")),
    "glucose": ("Glucose", ("2345-7", "2339-0", "41653-7")),
    "blood_pressure": ("Blood pressure", ("85354-9",)),
}
FINAL_STATUSES = {"final", "amended", "corrected"}
MEASUREMENT_LIMIT = 100
COUNTY_LIMIT = 20
ROW_LIMIT = 100
COUNTY = re.compile(r"^[A-Za-z][A-Za-z .'-]{0,79}$")
DEMO_IDENTIFIER = re.compile(r"^DEMO-[0-9]{4,8}$")
RESOURCE_ID = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")
SYNTHETIC_TAG_SYSTEM = "urn:astraldeep:sandbox"
DEMO_IDENTIFIER_SYSTEM = "urn:myhealthsafe:demo"
POPULATION_SOURCE = "HAPI FHIR R5 + SQL on FHIR reference runner"
POPULATION_DEFINITION = (
    "Latest final/amended/corrected percent A1C per patient; latest explicit pregnancy status; missing/incompatible excluded"
)


def invalid_population() -> FhirError:
    return FhirError("FHIR_INVALID_RESPONSE", "The FHIR endpoint returned an invalid population summary")


def timestamp(value: Any) -> Optional[datetime]:
    moment = clinical.parse_time(value)
    return moment if moment is not None and moment.tzinfo is not None and moment.utcoffset() is not None else None


def patient_label(record: Dict[str, Any]) -> str:
    names = [item for item in record.get("name") or [] if isinstance(item, dict)]
    chosen = next((item for item in names if item.get("use") == "official"), names[0] if names else {})
    given = chosen.get("given")
    parts = [value for value in given if isinstance(value, str)] if isinstance(given, list) else []
    if isinstance(chosen.get("family"), str):
        parts.append(chosen["family"])
    text = chosen.get("text")
    return (text if isinstance(text, str) and text else " ".join(parts) or str(record.get("id") or "Patient"))[:256]


def provenance(record: Dict[str, Any]) -> str:
    metadata = record.get("meta")
    tags = metadata.get("tag") if isinstance(metadata, dict) else None
    tags = tags if isinstance(tags, list) else []
    codes = {item["code"] for item in tags if isinstance(item, dict) and isinstance(item.get("code"), str)
             and item.get("system") == SYNTHETIC_TAG_SYSTEM}
    return "synthetic" if "synthetic" in codes else "public-deidentified" if "public-deidentified" in codes else "unverified"


def patient_record(client: FhirClient, identifier: str) -> Dict[str, Any]:
    try:
        record = client.read("Patient", identifier)
        if record.get("id") != identifier:
            raise FhirError("FHIR_INVALID_RESPONSE", "The FHIR endpoint returned an invalid patient match")
        return record
    except FhirError as error:
        if error.code != "FHIR_NOT_FOUND" or not DEMO_IDENTIFIER.fullmatch(identifier):
            raise
    records, total = client.search("Patient", [("identifier", f"{DEMO_IDENTIFIER_SYSTEM}|{identifier}")], 2)
    if len(records) > 1 or (total is not None and total > 1):
        raise FhirError("FHIR_BAD_REQUEST", "The demo identifier matches more than one patient; select a FHIR id")
    if not records:
        raise FhirError("FHIR_NOT_FOUND", "The FHIR endpoint has no such record")
    record = records[0]
    identifiers = record.get("identifier") or []
    if record.get("resourceType") != "Patient" or not RESOURCE_ID.fullmatch(str(record.get("id") or "")) or not any(
        isinstance(item, dict) and item.get("system") == DEMO_IDENTIFIER_SYSTEM and item.get("value") == identifier
        for item in identifiers
    ):
        raise FhirError("FHIR_INVALID_RESPONSE", "The FHIR endpoint returned an invalid patient match")
    return record


def finite_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def quantity(holder: Dict[str, Any], units: Tuple[str, ...]) -> Optional[Tuple[float, str]]:
    item = holder.get("valueQuantity")
    if not isinstance(item, dict) or item.get("comparator"):
        return None
    value = item.get("value")
    unit = item.get("code")
    if (
        not finite_number(value) or value < 0
        or unit not in units or item.get("system") != "http://unitsofmeasure.org"
    ):
        return None
    return float(value), str(unit)


def measurement_values(observation: Dict[str, Any], measure: str) -> List[Dict[str, Any]]:
    if measure != "blood_pressure":
        units = ("%",) if measure == "a1c" else ("mg/dL", "mmol/L")
        found = quantity(observation, units)
        if found is None or (measure == "a1c" and found[0] > 100):
            return []
        return [{"label": MEASURES[measure][0], "value": found[0], "unit": found[1]}]
    rows = []
    for code, label in (("8480-6", "Systolic blood pressure"), ("8462-4", "Diastolic blood pressure")):
        components = [item for item in observation.get("component") or [] if isinstance(item, dict)
                      and code in clinical.loinc_codes(item.get("code"))]
        if len(components) != 1:
            return []
        found = quantity(components[0], ("mm[Hg]",))
        if found is None:
            return []
        rows.append({"label": label, "value": found[0], "unit": "mmHg"})
    return rows


def subject_matches(client: FhirClient, subject: Any, patient: str) -> bool:
    if not isinstance(subject, str):
        return False
    try:
        parts = urlsplit(subject)
    except ValueError:
        return False
    if parts.query or parts.fragment:
        return False
    if parts.scheme or parts.netloc:
        base_path = urlsplit(client.settings.base_url).path.rstrip("/")
        return (parts.scheme, parts.netloc) == client.settings.origin and parts.path == f"{base_path}/Patient/{patient}"
    return parts.path == f"Patient/{patient}"


def observation_date(observation: Dict[str, Any]) -> Tuple[Optional[datetime], str]:
    period = observation.get("effectivePeriod")
    period = period if isinstance(period, dict) else {}
    for basis, value in (("effectiveDateTime", observation.get("effectiveDateTime")), ("effectivePeriod.end", period.get("end")),
                         ("effectivePeriod.start", period.get("start")), ("issued", observation.get("issued"))):
        if value:
            return timestamp(value), basis
    return None, ""


def measurements(client: FhirClient, identifier: str, measure: str) -> Dict[str, Any]:
    record = patient_record(client, identifier)
    patient = str(record.get("id") or "")
    if not RESOURCE_ID.fullmatch(patient):
        raise FhirError("FHIR_INVALID_RESPONSE", "The FHIR endpoint returned an invalid patient id")
    resources, total = client.search("Observation", [
        ("patient", patient), ("code", ",".join(f"{clinical.LOINC}|{code}" for code in MEASURES[measure][1])),
        ("status", "final,amended,corrected"), ("_sort", "-date"),
    ], MEASUREMENT_LIMIT)
    candidates = []
    for observation in resources:
        subject_record = observation.get("subject")
        subject = subject_record.get("reference") if isinstance(subject_record, dict) else None
        moment, date_basis = observation_date(observation)
        if (
            observation.get("resourceType") != "Observation" or observation.get("status") not in FINAL_STATUSES
            or not subject_matches(client, subject, patient) or moment is None
            or not RESOURCE_ID.fullmatch(str(observation.get("id") or ""))
            or not set(MEASURES[measure][1]).intersection(clinical.loinc_codes(observation.get("code")))
        ):
            continue
        rows = measurement_values(observation, measure)
        if rows:
            candidates.append((moment, str(observation["id"]), observation, rows, date_basis))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    rows, origins = [], {provenance(record)}
    if candidates:
        moment, source_id, observation, selected, date_basis = candidates[0]
        origins.add(provenance(observation))
        rows = [{**row, "measured_at": moment.isoformat(timespec="seconds"), "date_basis": date_basis,
                 "source_status": observation["status"], "source": f"Observation/{source_id}"} for row in selected]
    origin = next(iter(origins)) if len(origins) == 1 else "mixed"
    return {
        "patient": patient, "patient_label": patient_label(record), "measure": measure,
        "measurements": rows, "synthetic": True if origin == "synthetic" else False if origin == "public-deidentified" else None,
        "data_origin": origin, "source": "FHIR REST",
        "definition": "Latest usable final, amended or corrected measurement; effective date preferred, issued used when absent",
        "returned_observations": len(resources), "observation_limit": MEASUREMENT_LIMIT,
        "limited": (total is not None and total > len(resources)) or len(resources) >= MEASUREMENT_LIMIT,
    }


def population_parameters(state: Any, counties: Any, pregnant: Any) -> Tuple[List[Tuple[str, str]], Dict[str, Any]]:
    if state is not None and (not isinstance(state, str) or not re.fullmatch(r"[A-Za-z]{2}", state.strip())):
        raise FhirError("FHIR_BAD_REQUEST", "state must be a two-letter state code")
    if counties is not None and (not isinstance(counties, list) or len(counties) > COUNTY_LIMIT):
        raise FhirError("FHIR_BAD_REQUEST", "counties must be a list of at most 20 county names")
    selected = []
    for county in counties or []:
        if not isinstance(county, str) or not COUNTY.fullmatch(county.strip()):
            raise FhirError("FHIR_BAD_REQUEST", "Each county must be a name of at most 80 characters")
        county = county.strip()
        if county.lower() not in {item.lower() for item in selected}:
            selected.append(county)
    if pregnant is not None and not isinstance(pregnant, bool):
        raise FhirError("FHIR_BAD_REQUEST", "pregnant must be true, false or omitted")
    code = state.strip().upper() if state is not None else None
    params = [("state", code)] if code else []
    params += [("county", county) for county in selected]
    if pregnant is not None:
        params.append(("pregnant", str(pregnant).lower()))
    return params, {"state": code, "counties": selected, "pregnant": pregnant}


def population_result(payload: Dict[str, Any], scope: Dict[str, Any]) -> Dict[str, Any]:
    entries = payload.get("parameter")
    if not isinstance(entries, list):
        raise invalid_population()
    results = [item for item in entries if isinstance(item, dict) and item.get("name") == "result"]
    if len(results) != 1 or not isinstance(results[0].get("valueString"), str) or len(results[0]["valueString"]) > 128_000:
        raise invalid_population()
    try:
        data = json.loads(results[0]["valueString"])
    except ValueError as error:
        raise invalid_population() from error
    returned_scope = data.get("scope") if isinstance(data, dict) else None
    if (
        not isinstance(data, dict) or data.get("query_id") != "a1c-by-county" or data.get("synthetic") is not True
        or data.get("source") != POPULATION_SOURCE or data.get("definition") != POPULATION_DEFINITION
        or timestamp(data.get("generated_at")) is None or not isinstance(returned_scope, dict) or returned_scope != scope
        or (returned_scope.get("pregnant") is not None and not isinstance(returned_scope["pregnant"], bool))
        or not isinstance(data.get("rows"), list) or len(data["rows"]) > ROW_LIMIT
    ):
        raise invalid_population()
    rows, seen = [], set()
    for row in data["rows"]:
        if (
            not isinstance(row, dict) or not {"county", "patient_count", "with_a1c_count", "average_a1c"} <= row.keys()
            or not isinstance(row.get("county"), str) or not COUNTY.fullmatch(row["county"])
        ):
            raise invalid_population()
        county, patients, measured, average = row["county"], row.get("patient_count"), row.get("with_a1c_count"), row.get("average_a1c")
        if (
            county.lower() in seen or type(patients) is not int or type(measured) is not int
            or not 0 <= measured <= patients <= 1_000_000
            or (measured == 0 and average is not None)
            or (measured > 0 and (not finite_number(average) or not 0 <= average <= 100))
        ):
            raise invalid_population()
        seen.add(county.lower())
        rows.append({"county": county, "patient_count": patients, "with_a1c_count": measured, "average_a1c": average})
    return {key: data[key] for key in ("query_id", "generated_at", "synthetic", "source", "definition", "scope")} | {"rows": rows}
