"""Checks the draft client metadata document and app-link association file against the MCP 2026-07-28 rules.
Run it directly or through test_drafts.py, and the site publish task consumes the same files.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

SITE_ORIGIN = "https://astraldeep.github.io"
METADATA_PATH = "oauth/client-metadata.json"
ASSOCIATION_PATH = ".well-known/apple-app-site-association"
BUNDLE_ID = "com.personalailabs.astraldeep"
REQUIRED = ("client_id", "client_name", "redirect_uris")
SECRET_FIELDS = ("client_secret", "client_secret_expires_at", "registration_access_token", "registration_client_uri")
PUBLIC_AUTH_METHODS = {"none", "private_key_jwt"}
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
TEAM_ID = re.compile(r"^[A-Z0-9]{10}$")


def check_metadata(document: object, url: str) -> list[str]:
    if not isinstance(document, dict):
        return ["metadata document is not a JSON object"]
    problems = [f"missing required field {name}" for name in REQUIRED if name not in document]
    client_id = document.get("client_id")
    if client_id != url:
        problems.append("client_id does not equal the document URL")
    if isinstance(client_id, str):
        parts = urlsplit(client_id)
        if parts.scheme != "https" or not parts.netloc:
            problems.append("client_id is not an https URL")
        if parts.path in ("", "/"):
            problems.append("client_id has no path component")
        if parts.fragment or parts.username or parts.password:
            problems.append("client_id has a fragment or credentials")
        if {"..", "."} & set(parts.path.split("/")):
            problems.append("client_id has dot segments")
    problems.extend(f"secret field {name} present" for name in SECRET_FIELDS if name in document)
    if document.get("token_endpoint_auth_method", "none") not in PUBLIC_AUTH_METHODS:
        problems.append("token_endpoint_auth_method is a shared-secret method")
    uris = document.get("redirect_uris")
    if not isinstance(uris, list) or not uris:
        problems.append("redirect_uris is not a non-empty list")
        return problems
    problems.extend(_redirect_problems(uri) for uri in uris)
    return [p for p in problems if p]


def _redirect_problems(uri: object) -> str:
    if not isinstance(uri, str):
        return "redirect URI is not a string"
    parts = urlsplit(uri)
    if parts.fragment or "*" in uri:
        return f"redirect URI {uri} has a fragment or wildcard"
    if parts.scheme == "https" and parts.netloc:
        return ""
    if parts.scheme == "http" and parts.hostname in LOOPBACK:
        return ""
    return f"redirect URI {uri} is neither https nor loopback http"


def check_association(document: object, redirect_uris: list[str]) -> list[str]:
    if not isinstance(document, dict):
        return ["association file is not a JSON object"]
    app_id_suffix = "." + BUNDLE_ID
    problems: list[str] = []
    details = (document.get("applinks") or {}).get("details") or []
    app_ids = [app for entry in details for app in entry.get("appIDs", [])]
    web_apps = (document.get("webcredentials") or {}).get("apps") or []
    for label, ids in (("applinks", app_ids), ("webcredentials", web_apps)):
        if not ids:
            problems.append(f"{label} names no app")
        for app in ids:
            team, _, rest = app.partition(".")
            if not TEAM_ID.match(team) or "." + rest != app_id_suffix:
                problems.append(f"{label} app id {app} is not <team id>{app_id_suffix}")
    paths = {c.get("/") for entry in details for c in entry.get("components", [])}
    for uri in redirect_uris:
        parts = urlsplit(uri)
        if parts.scheme == "https" and f"{parts.scheme}://{parts.netloc}" == SITE_ORIGIN and parts.path not in paths:
            problems.append(f"callback path {parts.path} is not claimed by applinks")
    return problems


def validate(root: Path) -> list[str]:
    metadata = json.loads((root / METADATA_PATH).read_text())
    association = json.loads((root / ASSOCIATION_PATH).read_text())
    problems = check_metadata(metadata, f"{SITE_ORIGIN}/{METADATA_PATH}")
    return problems + check_association(association, metadata.get("redirect_uris", []))


if __name__ == "__main__":
    found = validate(Path(__file__).resolve().parent)
    print("\n".join(found) if found else "drafts valid")
    sys.exit(1 if found else 0)
