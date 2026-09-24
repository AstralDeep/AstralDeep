#!/usr/bin/env python3
"""Shared HTTP/credential foundation for the ML Services agent's tool slices
(classify_tools.py, forecaster_tools.py, llm_factory_tools.py): a per-call
ExternalServiceClient over shared.external_http plus uniform error/verdict mapping.
"""
import json
import os
import sys
from dataclasses import dataclass
from typing import Any, Dict

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from shared import external_http
from shared.external_http import (
    AuthFailedError,
    BadRequestError,
    EgressBlockedError,
    RateLimitedError,
    ServiceUnreachableError,
    normalize_url,
)

RETRYABLE_EXCEPTIONS = (ConnectionError, TimeoutError, json.JSONDecodeError, OSError)
try:
    import requests
    RETRYABLE_EXCEPTIONS = RETRYABLE_EXCEPTIONS + (requests.exceptions.RequestException,)
except ImportError:
    pass

NON_RETRYABLE_EXCEPTIONS = (TypeError, KeyError, ValueError, AttributeError)


def is_retryable_error(exc: Exception) -> bool:
    if isinstance(exc, RETRYABLE_EXCEPTIONS):
        return True
    if isinstance(exc, NON_RETRYABLE_EXCEPTIONS):
        return False
    return True


@dataclass(frozen=True)
class CredentialBundle:
    service: str
    display_name: str
    url_key: str
    api_key_key: str
    strip_v1_suffix: bool = False


CLASSIFY_BUNDLE = CredentialBundle(
    service="CLASSify",
    display_name="CLASSify",
    url_key="CLASSIFY_URL",
    api_key_key="CLASSIFY_API_KEY",
)

FORECASTER_BUNDLE = CredentialBundle(
    service="Forecaster",
    display_name="Timeseries Forecaster",
    url_key="FORECASTER_URL",
    api_key_key="FORECASTER_API_KEY",
)

LLM_FACTORY_BUNDLE = CredentialBundle(
    service="LLM-Factory",
    display_name="LLM-Factory",
    url_key="LLM_FACTORY_URL",
    api_key_key="LLM_FACTORY_API_KEY",
    strip_v1_suffix=True,
)


def bundle_configured(credentials: Dict[str, str], bundle: CredentialBundle) -> bool:
    if not isinstance(credentials, dict):
        return False
    return bool(credentials.get(bundle.url_key)) and bool(credentials.get(bundle.api_key_key))


class ExternalServiceClient:
    def __init__(self, credentials: Dict[str, str], bundle: CredentialBundle):
        self.bundle = bundle
        self.api_key = credentials.get(bundle.api_key_key, "")
        raw_url = credentials.get(bundle.url_key, "")
        base = normalize_url(raw_url) if raw_url else ""
        if bundle.strip_v1_suffix and base.endswith("/v1"):
            base = base[:-3]
        self.base_url = base

    def validate(self):
        if not self.base_url:
            raise ValueError(
                f"{self.bundle.service} Service URL is not configured. "
                "Open the agent's settings to add it."
            )
        if not self.api_key:
            raise ValueError(
                f"{self.bundle.service} API Key is not configured. "
                "Open the agent's settings to add it."
            )

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}{path}"

    def get(self, path: str, params: Dict[str, Any] = None):
        return external_http.request("GET", self._url(path), api_key=self.api_key, params=params)

    def post(self, path: str, json_body: Any = None, files: Dict[str, Any] = None,
             data: Dict[str, Any] = None):
        return external_http.request(
            "POST", self._url(path),
            api_key=self.api_key, json_body=json_body, files=files, data=data,
        )


def build_client(kwargs: Dict[str, Any], bundle: CredentialBundle) -> ExternalServiceClient:
    credentials = kwargs.get("_credentials", {})
    if not credentials:
        if kwargs.get("_credentials_stale"):
            raise ValueError(
                f"Saved {bundle.display_name} credentials could not be decrypted "
                "(the agent's encryption key has changed since they were saved). "
                "Open the agent's settings and save your Service URL and API key again."
            )
        raise ValueError(
            f"{bundle.display_name} is not configured. "
            "Save your Service URL and API key in the agent's settings."
        )
    client = ExternalServiceClient(credentials, bundle)
    client.validate()
    return client


def verdict_for_exception(exc: Exception) -> Dict[str, str]:
    if isinstance(exc, AuthFailedError):
        return {"credential_test": "auth_failed", "detail": str(exc)}
    if isinstance(exc, (ServiceUnreachableError, EgressBlockedError, RateLimitedError)):
        return {"credential_test": "unreachable", "detail": str(exc)}
    return {"credential_test": "unexpected", "detail": str(exc)}


def user_facing_error(exc: Exception, service: str) -> str:
    if isinstance(exc, AuthFailedError):
        return f"The saved {service} API key was rejected. Update it in the agent's settings."
    if isinstance(exc, ServiceUnreachableError):
        return f"{service} is unreachable. Try again later."
    if isinstance(exc, RateLimitedError):
        return f"{service} call failed: {exc}"
    if isinstance(exc, EgressBlockedError):
        return f"{service} URL is not allowed: {exc}"
    if isinstance(exc, BadRequestError):
        return f"{service} rejected the request: {exc}"
    return f"{service} call failed: {exc}"


def ui(components, data=None, retryable: bool = True):
    serialized = []
    for c in components:
        if hasattr(c, "to_json"):
            serialized.append(c.to_dict())
        else:
            serialized.append(c)
    return {"_ui_components": serialized, "_data": data, "_retryable": retryable}


def safe_json(resp) -> Dict[str, Any]:
    try:
        payload = resp.json() if resp.content else {}
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def render_metric_value(value: Any) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, float):
        return f"{value:.4f}" if abs(value) < 1000 else f"{value:.4g}"
    if isinstance(value, (list, tuple)):
        return ", ".join(render_metric_value(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value)
    return str(value)
