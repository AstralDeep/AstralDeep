"""Reference for the pure decisions of the MCP over HTTP adapter: the challenge parser, metadata URLs and checks, server selection, registration, the egress hosts a writer lists, scopes, the RFC 9207 table, step-up and the bearer target.
It takes already fetched results as data, reuses the URL rules of connection.py, and vectors/connection-oauth-v1.json pins it for every other implementation.
"""

from __future__ import annotations

import re
from typing import Any

import connection
from astral_mesh_ref.errors import MeshError

RESOURCE_WELL_KNOWN = "/.well-known/oauth-protected-resource"
OAUTH_WELL_KNOWN = "/.well-known/oauth-authorization-server"
OIDC_WELL_KNOWN = "/.well-known/openid-configuration"
APPLICATION_TYPES = ("native", "web")
CLIENT_NAME = "Astral"
REGISTERED = (200, 201)
REGISTERED_METHOD = "none"
AS_ENDPOINTS = (
    "authorization_endpoint",
    "token_endpoint",
    "registration_endpoint",
    "device_authorization_endpoint",
)
STEP_UPS_BETWEEN_SUCCESSES = 1
CODES = (
    "connection.resource_metadata_offsite",
    "connection.no_resource_metadata",
    "connection.resource_mismatch",
    "connection.no_authorization_server",
    "connection.issuer_changed",
    "connection.issuer_mismatch",
    "connection.no_s256",
    "connection.as_unreachable",
    "connection.no_client_id",
    "connection.registration_refused",
    "connection.too_many_scopes",
    "connection.iss_missing",
    "connection.iss_mismatch",
    "connection.refused",
    "connection.step_up_limit",
    "connection.token_passthrough",
)

TCHAR = set(
    "!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
)
TOKEN68 = re.compile(r"[A-Za-z0-9\-._~+/]+=*[ \t]*(?:,|$)")
SPACE = " \t"


class _Malformed(Exception):
    pass


def _fail(code: str, detail: str = "") -> MeshError:
    return MeshError(code, detail)


def _token(s: str, i: int) -> tuple[str, int]:
    j = i
    while j < len(s) and s[j] in TCHAR:
        j += 1
    return s[i:j], j


def _ows(s: str, i: int) -> int:
    while i < len(s) and s[i] in SPACE:
        i += 1
    return i


def _quoted(s: str, i: int) -> tuple[str, int]:
    out = []
    i += 1
    while i < len(s):
        char = s[i]
        if char == '"':
            return "".join(out), i + 1
        if char == "\\":
            if i + 1 >= len(s):
                raise _Malformed
            out.append(s[i + 1])
            i += 2
            continue
        out.append(char)
        i += 1
    raise _Malformed


def _params(s: str, i: int, params: dict[str, str]) -> int:
    while True:
        name, j = _token(s, i)
        j = _ows(s, j)
        if not name or j >= len(s) or s[j] != "=":
            raise _Malformed
        j = _ows(s, j + 1)
        if j < len(s) and s[j] == '"':
            value, j = _quoted(s, j)
        else:
            value, j = _token(s, j)
            if not value:
                raise _Malformed
        if name.lower() in params:
            raise _Malformed
        params[name.lower()] = value
        j = _ows(s, j)
        if j >= len(s):
            return j
        if s[j] != ",":
            raise _Malformed
        k = j + 1
        while k < len(s) and s[k] in SPACE + ",":
            k += 1
        if k >= len(s):
            return k
        following, after = _token(s, k)
        after = _ows(s, after)
        if not (following and after < len(s) and s[after] == "="):
            return k
        i = k


def challenges(header: str) -> list[tuple[str, dict[str, str]]]:
    if any(not (c == "\t" or " " <= c <= "~") for c in header):
        raise _Malformed
    out: list[tuple[str, dict[str, str]]] = []
    i = 0
    while True:
        while i < len(header) and header[i] in SPACE + ",":
            i += 1
        if i >= len(header):
            return out
        scheme, i = _token(header, i)
        if not scheme:
            raise _Malformed
        params: dict[str, str] = {}
        out.append((scheme.lower(), params))
        if i >= len(header) or header[i] == ",":
            continue
        if header[i] not in SPACE:
            raise _Malformed
        i = _ows(header, i)
        token68 = TOKEN68.match(header, i)
        if token68:
            i = token68.end()
            continue
        i = _params(header, i, params)


def joined(header: Any) -> Any:
    if isinstance(header, list) and all(isinstance(line, str) for line in header):
        return ", ".join(header)
    return header


def bearer_params(header: Any) -> dict[str, str] | None:
    header = joined(header)
    if not isinstance(header, str) or not header:
        return None
    try:
        parsed = challenges(header)
    except _Malformed:
        return None
    return next((params for scheme, params in parsed if scheme == "bearer"), None)


def resource_metadata_urls(endpoint: str, header: Any) -> list[str]:
    parsed = connection.split_url(endpoint, "endpoint")
    if parsed is None:
        raise _fail("connection.malformed", "endpoint")
    base = parsed["origin"] + RESOURCE_WELL_KNOWN
    params = bearer_params(header) or {}
    advertised = params.get("resource_metadata")
    if advertised is not None:
        tail = advertised[len(base) :] if advertised.startswith(base) else None
        if tail is None or not (
            tail == "" or (tail.startswith("/") and connection.path_ok(tail, False))
        ):
            raise _fail("connection.resource_metadata_offsite", advertised)
        return [advertised]
    return [base + parsed["path"], base] if parsed["path"] else [base]


def _document(responses: dict[str, Any], url: str) -> dict[str, Any] | None:
    answer = responses.get(url)
    if answer is None or answer.get("status") != 200:
        return None
    body = answer.get("json")
    return body if isinstance(body, dict) else None


def _scope_list(text: Any) -> list[str] | None:
    if not isinstance(text, str):
        return None
    tokens = [t for t in text.split(" ") if t]
    if not tokens or any(not connection.SCOPE.fullmatch(t) for t in tokens):
        return None
    return sorted(set(tokens))


def _supported(scopes: Any) -> list[str]:
    if not isinstance(scopes, list) or any(
        not isinstance(s, str) or not connection.SCOPE.fullmatch(s) for s in scopes
    ):
        return []
    return sorted(set(scopes))


def resource_metadata(case_input: dict[str, Any]) -> dict[str, Any]:
    endpoint, local = case_input["endpoint"], case_input["local"]
    for url in resource_metadata_urls(endpoint, case_input["header"]):
        document = _document(case_input["responses"], url)
        if document is not None:
            break
    else:
        raise _fail("connection.no_resource_metadata")
    if document.get("resource") != endpoint:
        raise _fail("connection.resource_mismatch", str(document.get("resource")))
    servers: list[str] = []
    listed = document.get("authorization_servers")
    for issuer in listed if isinstance(listed, list) else []:
        if connection.secure_url(issuer, "issuer", local) and issuer not in servers:
            servers.append(issuer)
    if not servers:
        raise _fail("connection.no_authorization_server")
    return {
        "url": url,
        "authorization_servers": servers,
        "scopes_supported": _supported(document.get("scopes_supported")),
    }


def as_metadata_urls(issuer: str) -> list[str]:
    parsed = connection.split_url(issuer, "issuer")
    if parsed is None:
        raise _fail("connection.malformed", "issuer")
    origin, path = parsed["origin"], parsed["path"].rstrip("/")
    if not path:
        return [origin + OAUTH_WELL_KNOWN, origin + OIDC_WELL_KNOWN]
    return [
        origin + OAUTH_WELL_KNOWN + path,
        origin + OIDC_WELL_KNOWN + path,
        origin + path + OIDC_WELL_KNOWN,
    ]


def select_server(case_input: dict[str, Any]) -> dict[str, Any]:
    issuers, responses = case_input["issuers"], case_input["responses"]
    expected = case_input.get("record_issuer")
    if expected is not None:
        if expected not in issuers:
            raise _fail("connection.issuer_changed", expected)
        issuers = [expected]
    found = False
    for issuer in issuers:
        document = None
        for url in as_metadata_urls(issuer):
            document = _document(responses, url)
            if document is not None:
                break
        if document is None:
            continue
        found = True
        if document.get("issuer") != issuer:
            raise _fail("connection.issuer_mismatch", issuer)
        methods = document.get("code_challenge_methods_supported")
        if isinstance(methods, list) and "S256" in methods:
            return {"issuer": issuer, "url": url}
    if not found:
        raise _fail("connection.as_unreachable")
    raise _fail("connection.no_s256")


def registration_branch(client_id: Any, metadata: dict[str, Any]) -> str:
    if client_id is not None:
        return "record"
    if metadata.get("client_id_metadata_document_supported") is True:
        return "metadata_document"
    endpoint = metadata.get("registration_endpoint")
    if isinstance(endpoint, str) and endpoint:
        return "dynamic"
    raise _fail("connection.no_client_id")


def registration_request(application_type: str, redirect_uri: str) -> dict[str, Any]:
    return {
        "application_type": application_type,
        "client_name": CLIENT_NAME,
        "grant_types": ["authorization_code", "refresh_token"],
        "redirect_uris": [redirect_uri],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }


def dynamic_registration(case_input: dict[str, Any]) -> dict[str, Any]:
    answers = case_input["responses"]
    requests = []
    for index, application_type in enumerate(APPLICATION_TYPES):
        requests.append(
            registration_request(application_type, case_input["redirect_uri"])
        )
        answer = answers[index] if index < len(answers) else None
        body = answer.get("json") if answer else None
        if (
            answer is not None
            and answer.get("status") in REGISTERED
            and isinstance(body, dict)
            and isinstance(body.get("client_id"), str)
            and body["client_id"]
            and body.get("token_endpoint_auth_method", REGISTERED_METHOD)
            == REGISTERED_METHOD
        ):
            return {
                "ok": {
                    "application_type": application_type,
                    "client_id": body["client_id"],
                    "requests": requests,
                }
            }
        if not (index == 0 and answer is not None and answer.get("status") == 400):
            break
    return {"error": "connection.registration_refused", "requests": requests}


def egress_entries(case_input: dict[str, Any]) -> dict[str, Any]:
    local = case_input["local"]
    endpoint = connection.split_url(case_input["endpoint"], "endpoint")
    if endpoint is None:
        raise _fail("connection.malformed", "endpoint")
    found = {connection.entry_for(endpoint)}
    issuer = case_input["issuer"]
    urls = [issuer] if issuer is not None else []
    urls += [
        case_input["metadata"][field]
        for field in AS_ENDPOINTS
        if isinstance(case_input["metadata"].get(field), str)
    ]
    for url in urls:
        parsed = connection.secure_url(url, "fetch", local)
        if parsed is None:
            raise _fail("connection.egress_refused", url)
        found.add(connection.entry_for(parsed))
    return {"entries": sorted(found, key=lambda e: connection.parse_entry(e, True))}


def choose_scopes(challenge: Any, scopes_supported: Any) -> list[str]:
    scopes = _scope_list(challenge)
    if scopes is None:
        scopes = _supported(scopes_supported)
    if len(scopes) > connection.MAX_SCOPES:
        raise _fail("connection.too_many_scopes", str(len(scopes)))
    return scopes


def check_iss(issuer: str, supported: Any, values: list[str]) -> dict[str, Any]:
    if len(values) > 1:
        raise _fail("connection.iss_mismatch", "repeated")
    if not values:
        if supported is True:
            raise _fail("connection.iss_missing")
        return {}
    if values[0] != issuer:
        raise _fail("connection.iss_mismatch", values[0])
    return {}


def step_up_sequence(case_input: dict[str, Any]) -> dict[str, Any]:
    scopes = list(case_input["scopes"])
    since_success = 0
    results: list[dict[str, Any]] = []
    for step in case_input["steps"]:
        if step.get("success"):
            since_success = 0
            results.append({"ok": {"action": "success"}})
            continue
        params = bearer_params(step["forbidden"])
        if params is None or params.get("error") != "insufficient_scope":
            results.append({"error": "connection.refused"})
            continue
        if since_success >= STEP_UPS_BETWEEN_SUCCESSES:
            results.append({"error": "connection.step_up_limit"})
            continue
        union = sorted(set(scopes) | set(_scope_list(params.get("scope")) or []))
        if len(union) > connection.MAX_SCOPES:
            results.append({"error": "connection.too_many_scopes"})
            continue
        action = "sign_in" if union == scopes else "put"
        scopes = union
        since_success += 1
        results.append({"ok": {"action": action, "scopes": union}})
    return {"ok": {"results": results}}


def bearer_target(endpoint: str, url: str) -> dict[str, Any]:
    parsed = connection.split_url(endpoint, "endpoint")
    if parsed is None:
        raise _fail("connection.malformed", "endpoint")
    origin = parsed["origin"]
    if url != origin and not url.startswith((origin + "/", origin + "?")):
        raise _fail("connection.token_passthrough", url)
    return {}


def _guard(call: Any) -> dict[str, Any]:
    try:
        return {"ok": call()}
    except MeshError as error:
        return {"error": error.code}


def run(kind: str, case_input: dict[str, Any]) -> dict[str, Any]:
    if kind == "oauth_dynamic_registration":
        return dynamic_registration(case_input)
    if kind == "oauth_step_up_sequence":
        return step_up_sequence(case_input)
    calls = {
        "oauth_challenge": lambda: {"bearer": bearer_params(case_input["header"])},
        "oauth_resource_metadata_urls": lambda: {
            "urls": resource_metadata_urls(case_input["endpoint"], case_input["header"])
        },
        "oauth_resource_metadata": lambda: resource_metadata(case_input),
        "oauth_as_metadata_urls": lambda: {
            "urls": as_metadata_urls(case_input["issuer"])
        },
        "oauth_select_server": lambda: select_server(case_input),
        "oauth_registration": lambda: {
            "branch": registration_branch(
                case_input["client_id"], case_input["metadata"]
            )
        },
        "oauth_egress_entries": lambda: egress_entries(case_input),
        "oauth_scopes": lambda: {
            "scopes": choose_scopes(
                case_input["challenge"], case_input["scopes_supported"]
            )
        },
        "oauth_iss": lambda: check_iss(
            case_input["issuer"], case_input["iss_supported"], case_input["iss"]
        ),
        "oauth_bearer_target": lambda: bearer_target(
            case_input["endpoint"], case_input["url"]
        ),
    }
    return _guard(calls[kind])
