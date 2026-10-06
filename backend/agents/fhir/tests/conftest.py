"""Shared fixtures for the FHIR agent suite: an in-memory FHIR R5 server that speaks through
the agent's transport hook, so tools and the client run without a network.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import pytest

from agents.fhir import mcp_tools
from agents.fhir.client import FhirClient, FhirSettings
from shared import external_http

BASE = "http://eicu-fhir:8080/fhir"
TOKEN = "t" * 40
ZONE = timezone(timedelta(hours=-4))
NOW = datetime(2026, 10, 5, 16, 0, tzinfo=ZONE)
LOINC = "http://loinc.org"
CATEGORY = "http://terminology.hl7.org/CodeSystem/observation-category"
TOPIC_BASE = "https://example.org/fhir/SubscriptionTopic"


def at(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def coding(code: str, display: str) -> Dict[str, Any]:
    return {"coding": [{"system": LOINC, "code": code, "display": display}], "text": display}


def observation(identifier: str, patient: str, code: str, label: str, category: str, minutes_ago: float, **value: Any) -> Dict[str, Any]:
    return {
        "resourceType": "Observation", "id": identifier, "status": "final",
        "category": [{"coding": [{"system": CATEGORY, "code": category}]}],
        "code": coding(code, label), "subject": {"reference": f"Patient/{patient}"},
        "encounter": {"reference": "Encounter/icu-1"}, "effectiveDateTime": at(minutes_ago), **value,
    }


def quantity(value: float, unit: str, comparator: Optional[str] = None) -> Dict[str, Any]:
    result: Dict[str, Any] = {"value": value, "unit": unit}
    if comparator:
        result["comparator"] = comparator
    return {"valueQuantity": result}


def pressure(identifier: str, patient: str, minutes_ago: float, systolic: Optional[float], diastolic: Optional[float], mean: Optional[float]) -> Dict[str, Any]:
    components = []
    for code, label, value in (("8480-6", "Systolic", systolic), ("8462-4", "Diastolic", diastolic), ("8478-0", "Mean", mean)):
        entry: Dict[str, Any] = {"code": coding(code, label)}
        if value is None:
            entry["dataAbsentReason"] = {"text": "Unknown"}
        else:
            entry["valueQuantity"] = {"value": value, "unit": "mmHg"}
        components.append(entry)
    return observation(identifier, patient, "85354-9", "Blood pressure", "vital-signs", minutes_ago, component=components)


def encounter(identifier: str, patient: str, status: str, kind: str, start: float, end: Optional[float] = None, unit: str = "Med-Surg ICU") -> Dict[str, Any]:
    period = {"start": at(start)}
    admission: Dict[str, Any] = {"admitSource": {"text": "Emergency Department"}}
    if end is not None:
        period["end"] = at(end)
        admission["dischargeDisposition"] = {"text": "Floor"}
    resource: Dict[str, Any] = {
        "resourceType": "Encounter", "id": identifier, "status": status,
        "class": [{"coding": [{"code": kind}]}], "subject": {"reference": f"Patient/{patient}"},
        "actualPeriod": period, "admission": admission,
    }
    if kind == "ACUTE":
        resource["type"] = [{"text": unit}]
        resource["reason"] = [{"value": [{"concept": {"text": "Sepsis, pulmonary"}}]}]
    return resource


def build_resources() -> Dict[str, List[Dict[str, Any]]]:
    observations = [
        observation("hr-1", "002-1", "8867-4", "Heart rate", "vital-signs", 5, **quantity(148, "/min")),
        observation("hr-2", "002-1", "8867-4", "Heart rate", "vital-signs", 65, **quantity(96, "/min")),
        observation("hr-3", "002-1", "8867-4", "Heart rate", "vital-signs", 125, **quantity(88, "/min")),
        observation("sp-1", "002-1", "59408-5", "SpO2", "vital-signs", 5, **quantity(91, "%")),
        observation("sp-2", "002-1", "59408-5", "SpO2", "vital-signs", 65, **quantity(97, "%")),
        observation("rr-1", "002-1", "9279-1", "Respiratory rate", "vital-signs", 5, **quantity(22, "/min")),
        observation("tp-1", "002-1", "8310-5", "Body temperature", "vital-signs", 30, **quantity(38.6, "Cel")),
        pressure("bp-1", "002-1", 10, 92, 51, 62),
        pressure("bp-2", "002-1", 70, None, None, 71),
        observation("gcs-1", "002-1", "9269-2", "Glasgow coma score total", "survey", 40, **quantity(14, "{score}")),
        observation("age-1", "002-1", "30525-0", "Age", "social-history", 600, **quantity(67, "years")),
        observation("ht-1", "002-1", "8302-2", "Body height", "vital-signs", 600, **quantity(165, "cm")),
        observation("wt-1", "002-1", "29463-7", "Body weight", "vital-signs", 600, **quantity(70, "kg")),
        observation("k-1", "002-1", "2823-3", "Potassium", "laboratory", 60, **quantity(3.2, "mmol/L")),
        observation("k-2", "002-1", "2823-3", "Potassium", "laboratory", 420, **quantity(4.1, "mmol/L")),
        observation("lac-1", "002-1", "2524-7", "Lactate", "laboratory", 55, **quantity(5.1, "mmol/L")),
        observation("tn-1", "002-1", "10839-9", "Troponin I", "laboratory", 50, **quantity(0.04, "ng/mL", "<")),
        observation("ket-1", "002-1", "local-ketones", "Serum ketones", "laboratory", 45, valueString="POSITIVE"),
        observation("dev-1", "002-1", "local-device", "Oxygen device", "therapy", 20, valueCodeableConcept={"text": "nasal cannula"}),
        observation("hr-9", "002-2", "8867-4", "Heart rate", "vital-signs", 3000, **quantity(70, "/min")),
        observation("rr-2", "002-1", "9279-1", "Respiratory rate", "vital-signs", 65, **quantity(18, "/min")),
        observation("tp-2", "002-1", "8310-5", "Body temperature", "vital-signs", 150, **quantity(37.9, "Cel")),
        pressure("bp-3", "002-1", 130, 118, 64, 82),
    ]
    observations[17]["code"] = {"text": "Serum ketones"}
    observations[18]["code"] = {"text": "Oxygen device"}
    return {
        "Patient": [
            {"resourceType": "Patient", "id": "002-1", "gender": "female", "birthDate": "1959",
             "managingOrganization": {"reference": "Organization/hospital-10", "display": "eICU Hospital 10"}},
            {"resourceType": "Patient", "id": "002-2", "gender": "male", "deceasedDateTime": at(2000)},
        ],
        "Encounter": [
            encounter("icu-1", "002-1", "in-progress", "ACUTE", 30),
            encounter("hosp-1", "002-1", "in-progress", "IMP", 150),
            encounter("icu-2", "002-2", "completed", "ACUTE", 4000, 2000, "Cardiac ICU"),
        ],
        "Observation": observations,
        "Condition": [
            {"resourceType": "Condition", "id": "dx-1", "subject": {"reference": "Patient/002-1"},
             "clinicalStatus": {"coding": [{"code": "active"}]}, "code": {"text": "acute respiratory failure"},
             "recordedDate": at(25)},
            {"resourceType": "Condition", "id": "dx-2", "subject": {"reference": "Patient/002-1"},
             "clinicalStatus": {"coding": [{"code": "active"}]}, "code": {"text": "Acute respiratory failure"},
             "recordedDate": at(20)},
            {"resourceType": "Condition", "id": "hx-1", "subject": {"reference": "Patient/002-1"},
             "clinicalStatus": {"coding": [{"code": "unknown"}]}, "code": {"text": "hypertension"}, "recordedDate": at(28)},
        ],
        "Procedure": [
            {"resourceType": "Procedure", "id": "tx-1", "status": "in-progress", "subject": {"reference": "Patient/002-1"},
             "code": {"text": "mechanical ventilation"}, "occurrencePeriod": {"start": at(15)}},
        ],
        "AllergyIntolerance": [
            {"resourceType": "AllergyIntolerance", "id": "al-1", "patient": {"reference": "Patient/002-1"},
             "code": {"text": "penicillins"}, "recordedDate": at(29)},
        ],
        "MedicationRequest": [
            {"resourceType": "MedicationRequest", "id": "mr-1", "status": "active", "intent": "order",
             "subject": {"reference": "Patient/002-1"}, "medication": {"concept": {"text": "NOREPINEPHRINE"}},
             "authoredOn": at(20), "dosageInstruction": [{"text": "4 mg IV Continuous"}]},
            {"resourceType": "MedicationRequest", "id": "mr-2", "status": "completed", "intent": "order",
             "subject": {"reference": "Patient/002-1"}, "medication": {"concept": {"text": "ASPIRIN"}}, "authoredOn": at(26)},
            {"resourceType": "MedicationRequest", "id": "mr-3", "status": "cancelled", "intent": "order",
             "subject": {"reference": "Patient/002-1"}, "medication": {"reference": {"reference": "Medication/m9"}},
             "authoredOn": at(27)},
        ],
        "MedicationAdministration": [
            {"resourceType": "MedicationAdministration", "id": "ma-1", "status": "completed",
             "subject": {"reference": "Patient/002-1"}, "medication": {"concept": {"text": "Norepinephrine"}},
             "occurenceDateTime": at(18), "dosage": {"rateQuantity": {"value": 8, "unit": "mcg/min"}}},
            {"resourceType": "MedicationAdministration", "id": "ma-2", "status": "completed",
             "subject": {"reference": "Patient/002-1"}, "medication": {"concept": {"text": "Norepinephrine"}},
             "occurenceDateTime": at(8), "dosage": {"rateQuantity": {"value": 12, "unit": "mcg/min"}}},
            {"resourceType": "MedicationAdministration", "id": "ma-3", "status": "completed",
             "subject": {"reference": "Patient/002-1"}, "medication": {"concept": {"text": "IVF"}}, "occurenceDateTime": at(8)},
        ],
        "MedicationStatement": [
            {"resourceType": "MedicationStatement", "id": "ms-1", "status": "recorded",
             "subject": {"reference": "Patient/002-1"}, "medication": {"concept": {"text": "LISINOPRIL"}},
             "dateAsserted": at(28), "dosage": [{"text": "10 mg daily"}]},
        ],
        "RiskAssessment": [
            {"resourceType": "RiskAssessment", "id": "ra-1", "status": "final", "subject": {"reference": "Patient/002-1"},
             "method": {"text": "APACHE IV"}, "occurrenceDateTime": at(10),
             "prediction": [{"outcome": {"text": "ICU mortality"}, "probabilityDecimal": 0.10}]},
            {"resourceType": "RiskAssessment", "id": "ra-2", "status": "final", "subject": {"reference": "Patient/002-1"},
             "method": {"text": "APACHE IVa"}, "occurrenceDateTime": at(10),
             "prediction": [{"outcome": {"text": "ICU mortality"}, "probabilityDecimal": 0.31},
                            {"outcome": {"text": "Hospital mortality"}, "probabilityDecimal": 0.42},
                            {"outcome": {"text": "ignored"}, "probabilityDecimal": True}]},
        ],
        "Organization": [{"resourceType": "Organization", "id": "hospital-10", "name": "eICU Hospital 10", "description": "Teaching hospital"}],
        "Location": [{"resourceType": "Location", "id": "ward-90", "name": "Med-Surg ICU (ward 90)"}],
    }


class FakeResponse:
    def __init__(self, status_code: int, payload: Any):
        self.status_code = status_code
        self.content = b"" if payload is None else (payload if isinstance(payload, bytes) else json.dumps(payload).encode())


class FakeFhir:
    def __init__(self) -> None:
        self.resources = build_resources()
        self.calls: List[Tuple[str, str, Dict[str, List[str]]]] = []
        self.failures: Dict[str, Exception] = {}
        self.subscriptions: Dict[str, List[Dict[str, Any]]] = {}
        self.deleted: List[str] = []
        self.replay_available = True
        self.page_size: Optional[int] = None
        self.event_batch = 200

    def __call__(self, method: str, url: str, **options: Any) -> FakeResponse:
        assert options["api_key"] == TOKEN
        assert options["allowed_private_hosts"] == ["eicu-fhir"]
        assert options["trust_environment"] is False
        path = urlsplit(url).path[len("/fhir/"):]
        query: Dict[str, List[str]] = {}
        for name, value in options.get("params") or []:
            query.setdefault(name, []).append(value)
        self.calls.append((method, path, query))
        for marker, failure in self.failures.items():
            if marker in path:
                raise failure
        if method == "POST" and path == "Subscription":
            identifier = f"sub-{len(self.subscriptions) + 1}"
            self.subscriptions[identifier] = []
            return FakeResponse(201, {**options["json_body"], "id": identifier, "status": "active"})
        if method == "DELETE":
            self.deleted.append(path.rsplit("/", 1)[-1])
            return FakeResponse(204, None)
        return FakeResponse(200, self.get(path, query))

    def bundle(self, path: str, resources: List[Dict[str, Any]], query: Dict[str, List[str]], total: Optional[int] = None) -> Dict[str, Any]:
        offset = int((query.get("_offset") or ["0"])[0])
        count = self.page_size or int((query.get("_count") or ["500"])[0])
        page = resources[offset:offset + count]
        links = [{"relation": "self", "url": f"{BASE}/{path}"}]
        if offset + count < len(resources):
            pairs = [f"{name}={value}" for name, values in query.items() if name != "_offset" for value in values]
            links.append({"relation": "next", "url": f"{BASE}/{path}?" + "&".join(pairs + [f"_offset={offset + count}"])})
        return {
            "resourceType": "Bundle", "type": "searchset", "total": len(resources) if total is None else total, "link": links,
            "entry": [{"resource": resource, "search": {"mode": "match"}} for resource in page],
        }

    def matches(self, resource: Dict[str, Any], query: Dict[str, List[str]]) -> bool:
        subject = (resource.get("subject") or resource.get("patient") or {}).get("reference", "")
        for value in query.get("patient", []):
            if subject != f"Patient/{value}":
                return False
        for value in query.get("status", []):
            if resource.get("status") not in value.split(","):
                return False
        for value in query.get("class", []):
            if value not in [item["code"] for concept in resource.get("class", []) for item in concept["coding"]]:
                return False
        for value in query.get("category", []):
            if value not in [item["code"] for concept in resource.get("category", []) for item in concept["coding"]]:
                return False
        for value in query.get("code", []):
            wanted = {token.split("|")[-1] for token in value.split(",")}
            if not wanted & {item["code"] for item in resource.get("code", {}).get("coding", [])}:
                return False
        for name in ("date", "date-start", "recorded-date", "authoredon"):
            for value in query.get(name, []):
                moment = (
                    resource.get("effectiveDateTime") or resource.get("recordedDate") or resource.get("authoredOn")
                    or (resource.get("actualPeriod") or resource.get("occurrencePeriod") or {}).get("start")
                )
                if value.startswith("ge") and moment and datetime.fromisoformat(moment) < datetime.fromisoformat(value[2:]):
                    return False
        return not (query.get("deceased") == ["true"] and "deceasedDateTime" not in resource)

    def get(self, path: str, query: Dict[str, List[str]]) -> Any:
        if path == "metadata":
            return {
                "resourceType": "CapabilityStatement", "title": "eICU demo live-replay FHIR server", "fhirVersion": "5.0.0",
                "description": "Read-only replay", "software": {"name": "eicu-fhir", "version": "0.1.0"},
                "rest": [{"resource": [{
                    "type": "Observation", "interaction": [{"code": "read"}, {"code": "search-type"}],
                    "searchParam": [{"name": "code"}, {"name": "patient"}],
                }, {
                    "type": "Encounter", "interaction": [{"code": "read"}],
                    "searchParam": [{"name": name} for name in (
                        "class", "date", "date-start", "end-date", "identifier", "location", "part-of", "patient")],
                }]}],
            }
        if path == "$replay-status":
            if not self.replay_available:
                raise external_http.BadRequestError("Upstream returned 404: nope")
            return {"resourceType": "Parameters", "parameter": [
                {"name": "now", "valueInstant": NOW.isoformat(timespec="seconds")}, {"name": "patients", "valueInteger": 1841},
                {"name": "icuStays", "valueInteger": 2520}, {"name": "activeIcuEncounters", "valueInteger": 189},
                {"name": "observations", "valueInteger": 6960940}, {"name": "cycleDays", "valueInteger": 28},
                {"name": "dataset", "valueString": "eICU demo 2.0.1"}, {"name": "timezone", "valueString": "America/New_York"},
            ]}
        if path == "SubscriptionTopic":
            topics = [{"resourceType": "SubscriptionTopic", "id": name, "url": f"{TOPIC_BASE}/{name}"} for name in ("observation", "encounter")]
            return self.bundle(path, topics, {})
        if path.startswith("Subscription/") and path.endswith("/$events"):
            identifier = path.split("/")[1]
            after = int(query["eventsSinceNumber"][0])
            events = self.subscriptions[identifier][after - 1:after - 1 + self.event_batch]
            status = {
                "resourceType": "SubscriptionStatus", "type": "query-event" if events else "query-status",
                "notificationEvent": [{"eventNumber": str(after + index)} for index in range(len(events))],
            }
            if not events:
                status.pop("notificationEvent")
            entries = [{"resource": status}] + [
                {"resource": resource, "request": {"method": "POST" if created else "PUT"}} for resource, created in events
            ]
            return {"resourceType": "Bundle", "type": "subscription-notification", "entry": entries}
        if path == "Observation/$lastn":
            newest: Dict[str, Dict[str, Any]] = {}
            for resource in self.resources["Observation"]:
                if not self.matches(resource, query):
                    continue
                key = json.dumps(resource["code"], sort_keys=True)
                if key not in newest or resource["effectiveDateTime"] > newest[key]["effectiveDateTime"]:
                    newest[key] = resource
            return self.bundle(path, list(newest.values()), {})
        resource_type, _, identifier = path.partition("/")
        if identifier:
            for resource in self.resources.get(resource_type, []):
                if resource["id"] == identifier:
                    return resource
            raise external_http.BadRequestError("Upstream returned 404: not found")
        found = [resource for resource in self.resources.get(resource_type, []) if self.matches(resource, query)]
        return self.bundle(path, found, query)


@pytest.fixture
def settings() -> FhirSettings:
    return FhirSettings(BASE, TOKEN, 20)


@pytest.fixture
def server() -> FakeFhir:
    return FakeFhir()


@pytest.fixture
def connected(monkeypatch, server, settings) -> FakeFhir:
    monkeypatch.setattr(mcp_tools, "connect", lambda: FhirClient(settings, transport=server))
    return server
