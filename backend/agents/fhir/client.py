"""FHIR R5 REST client for the FHIR agent: reads the operator-configured endpoint from the
environment and calls it through shared.external_http with that one host allow-listed.
mcp_tools.py uses it to read, search and poll; failures surface as FhirError codes.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlsplit

from shared import external_http

DEFAULT_TIMEOUT_SECONDS = 20
MAX_TIMEOUT_SECONDS = 25
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_PAGES = 12
FHIR_JSON = "application/fhir+json"

RESOURCE_TYPES = (
    "AllergyIntolerance", "Condition", "Encounter", "Location", "MedicationAdministration",
    "MedicationRequest", "MedicationStatement", "Observation", "Organization", "Patient",
    "Procedure", "RiskAssessment",
)


class FhirError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def as_tool_error(self) -> Dict[str, Any]:
        return {"_error": {"code": self.code, "message": self.message, "retryable": self.retryable}}


@dataclass(frozen=True)
class FhirSettings:
    base_url: str
    token: str
    timeout: int

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).hostname or ""

    @property
    def origin(self) -> Tuple[str, str]:
        parts = urlsplit(self.base_url)
        return parts.scheme, parts.netloc


def load_settings(environment: Optional[Dict[str, str]] = None) -> FhirSettings:
    source = os.environ if environment is None else environment
    base_url = (source.get("FHIR_BASE_URL") or "").strip().rstrip("/")
    token = (source.get("FHIR_ACCESS_TOKEN") or "").strip()
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.hostname or not token:
        raise FhirError("FHIR_NOT_CONFIGURED", "FHIR_BASE_URL and FHIR_ACCESS_TOKEN must be set")
    try:
        timeout = int((source.get("FHIR_TIMEOUT_SECONDS") or "").strip() or DEFAULT_TIMEOUT_SECONDS)
    except ValueError:
        timeout = DEFAULT_TIMEOUT_SECONDS
    return FhirSettings(base_url, token, max(1, min(timeout, MAX_TIMEOUT_SECONDS)))


def bundle_resources(bundle: Dict[str, Any], mode: str = "match") -> List[Dict[str, Any]]:
    found = []
    for entry in bundle.get("entry") or []:
        resource = entry.get("resource")
        entry_mode = (entry.get("search") or {}).get("mode", "match")
        if isinstance(resource, dict) and entry_mode == mode:
            found.append(resource)
    return found


def next_link(bundle: Dict[str, Any]) -> Optional[str]:
    for link in bundle.get("link") or []:
        if link.get("relation") == "next" and isinstance(link.get("url"), str):
            return link["url"]
    return None


class FhirClient:
    def __init__(self, settings: FhirSettings, transport: Callable[..., Any] = external_http.request):
        self.settings = settings
        self._transport = transport

    def _call(self, method: str, url: str, params: Optional[Iterable[Tuple[str, str]]], body: Any) -> Any:
        try:
            response = self._transport(
                method,
                url,
                api_key=self.settings.token,
                json_body=body,
                params=list(params) if params else None,
                timeout=self.settings.timeout,
                max_response_bytes=MAX_RESPONSE_BYTES,
                extra_headers={"Accept": FHIR_JSON},
                allowed_private_hosts=[self.settings.host],
                trust_environment=False,
            )
        except external_http.EgressBlockedError as error:
            raise FhirError("FHIR_BLOCKED", "The FHIR endpoint is blocked by egress policy") from error
        except external_http.AuthFailedError as error:
            raise FhirError("FHIR_AUTH_FAILED", "The FHIR endpoint rejected the access token") from error
        except external_http.BadRequestError as error:
            if "returned 404" in str(error) or "returned 410" in str(error):
                raise FhirError("FHIR_NOT_FOUND", "The FHIR endpoint has no such record") from error
            raise FhirError("FHIR_BAD_REQUEST", "The FHIR endpoint refused the query") from error
        except external_http.ResponseTooLargeError as error:
            raise FhirError("FHIR_BAD_REQUEST", "The FHIR response was too large; narrow the query") from error
        except external_http.ExternalHttpError as error:
            raise FhirError("FHIR_UNAVAILABLE", "The FHIR endpoint could not be reached", retryable=True) from error
        if response.status_code == 204 or not response.content:
            return None
        try:
            return json.loads(response.content)
        except ValueError as error:
            raise FhirError("FHIR_INVALID_RESPONSE", "The FHIR endpoint returned a response that is not JSON") from error

    def _resource(self, payload: Any, expected: Optional[str] = None) -> Dict[str, Any]:
        if not isinstance(payload, dict) or not isinstance(payload.get("resourceType"), str):
            raise FhirError("FHIR_INVALID_RESPONSE", "The FHIR endpoint returned a response that is not a resource")
        if expected and payload["resourceType"] != expected:
            raise FhirError("FHIR_INVALID_RESPONSE", f"Expected a {expected} resource")
        return payload

    def get(self, path: str, params: Optional[Iterable[Tuple[str, str]]] = None, expected: Optional[str] = None) -> Dict[str, Any]:
        return self._resource(self._call("GET", f"{self.settings.base_url}/{path.lstrip('/')}", params, None), expected)

    def read(self, resource_type: str, identifier: str) -> Dict[str, Any]:
        return self.get(f"{resource_type}/{identifier}", expected=resource_type)

    def bundle(self, path: str, params: Optional[Iterable[Tuple[str, str]]] = None) -> Dict[str, Any]:
        return self.get(path, params, expected="Bundle")

    def follow(self, url: str) -> Dict[str, Any]:
        parts = urlsplit(url)
        if (parts.scheme, parts.netloc) != self.settings.origin or not parts.path.startswith(urlsplit(self.settings.base_url).path):
            raise FhirError("FHIR_INVALID_RESPONSE", "The FHIR endpoint returned a paging link to another address")
        return self._resource(
            self._call("GET", f"{parts.scheme}://{parts.netloc}{parts.path}", parse_qsl(parts.query), None), "Bundle"
        )

    def search(self, resource_type: str, params: Iterable[Tuple[str, str]], limit: int) -> Tuple[List[Dict[str, Any]], Optional[int]]:
        limit = max(1, limit)
        query = [(name, value) for name, value in params if name != "_count"]
        query.append(("_count", str(min(limit, 500))))
        bundle = self.bundle(resource_type, query)
        total = bundle.get("total") if isinstance(bundle.get("total"), int) else None
        resources = bundle_resources(bundle)
        pages = 1
        while len(resources) < limit and pages < MAX_PAGES:
            following = next_link(bundle)
            if not following:
                break
            bundle = self.follow(following)
            resources += bundle_resources(bundle)
            pages += 1
        return resources[:limit], total

    def latest(self, params: Iterable[Tuple[str, str]], maximum: int = 1) -> List[Dict[str, Any]]:
        query = list(params) + [("max", str(maximum))]
        return bundle_resources(self.bundle("Observation/$lastn", query))

    def post(self, path: str, body: Dict[str, Any], expected: Optional[str] = None) -> Dict[str, Any]:
        return self._resource(self._call("POST", f"{self.settings.base_url}/{path.lstrip('/')}", None, body), expected)

    def delete(self, path: str) -> None:
        self._call("DELETE", f"{self.settings.base_url}/{path.lstrip('/')}", None, None)
