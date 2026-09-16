"""Opt-in native custody of a fresh Keycloak grant, never a token-import API.

Attempt bookkeeping is bounded and process-local. A lost device attempt cannot
be reconstructed from its handle; uncertain exchanges are terminal. The IdP's
one-use authorization code remains the cross-process replay fence. This module
does not promise distributed OAuth-result recovery.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import secrets
import time
from urllib.parse import parse_qsl, urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials

from orchestrator import auth, device_login, session_store, web_auth
from shared.auth_clients import _primary_client_id, allowed_azps

MODE = "server_v1"
HEADER = "x-astral-session-custody"
BODY_LIMIT = 16_384
REQUEST_SECONDS = 15
ATTEMPT_SECONDS = 600
ATTEMPT_LIMIT = 512
ISSUANCE_LIMIT = 8
_CODE_ATTEMPTS: dict[str, float] = {}
_ISSUANCE_TASKS: set[asyncio.Task] = set()
_STARTS_IN_FLIGHT = 0
_REDIRECTS = {
    "astral-mobile": "com.personalailabs.astraldeep:/oauth2redirect",
    "astral-desktop": "com.personalailabs.astraldeep:/oauth2redirect",
}


def _deny(status=400):
    raise HTTPException(status, "native_session_unavailable")


def requested(request: Request) -> bool:
    return HEADER in request.headers


def _text(value, maximum):
    return (type(value) is str and 0 < len(value) <= maximum
            and value == value.strip() and all(32 < ord(c) < 127 for c in value))


def _expiry(claims):
    exp = claims.get("exp")
    if type(exp) not in (int, float) or not math.isfinite(exp) or exp <= time.time():
        _deny(401)
    return exp


def _issuer():
    authority = auth._get_keycloak_config()[0]
    try:
        parsed = urlsplit(authority)
    except ValueError:
        _deny(503)
    if (not _text(authority, 2048) or parsed.scheme != "https" or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or authority.endswith("/") or web_auth._is_mock()):
        _deny(503)
    return authority


def _client(client, *, device=False):
    if (not _text(client, 256) or client == _primary_client_id()
            or client not in allowed_azps()):
        _deny()
    if device:
        device_login._validate_client(client)
    elif client not in _REDIRECTS:
        _deny()
    return client


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _deny()
        result[key] = value
    return result


async def _body(request, fields):
    if (request.headers.getlist(HEADER) != [MODE]
            or request.headers.get("origin") is not None or request.url.query
            or request.url.scheme != "https"):
        _deny()
    size = request.headers.getlist("content-length")
    if len(size) > 1 or (size and (not size[0].isdigit() or int(size[0]) > BODY_LIMIT)):
        _deny(413)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > BODY_LIMIT:
            _deny(413)
    try:
        content_type = request.headers.get("content-type", "").split(";")[0]
        if content_type == "application/json":
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
        elif content_type == "application/x-www-form-urlencoded":
            value = _pairs(parse_qsl(raw.decode("utf-8"), keep_blank_values=True,
                                    strict_parsing=True, max_num_fields=16,
                                    encoding="utf-8", errors="strict"))
        else:
            _deny()
    except (ValueError, UnicodeError, RecursionError):
        _deny()
    if type(value) is not dict or set(value) != set(fields) or value.get("session_custody") != MODE:
        _deny()
    return value


@dataclass(repr=False)
class _Binding:
    store: session_store.WebSessionStore
    issuer: str
    client: str | None
    cookie_key: bytes = field(repr=False)
    encryption_key: bytes = field(repr=False)
    cipher: object = field(repr=False)
    expires: float
    request: Request = field(repr=False)
    headers: tuple = field(repr=False)
    hard_max: int
    owner: str | None = None
    row: dict | None = field(default=None, repr=False)

    def local(self):
        if (web_auth._get_store() is not self.store or _issuer() != self.issuer
                or (self.client is not None and
                    _client(self.client, device=self.client not in _REDIRECTS) != self.client)
                or web_auth._secret() != self.cookie_key
                or session_store._enc_key() != self.encryption_key
                or self.store._fernet is not self.cipher or time.time() >= self.expires
                or web_auth.HARD_MAX_SECONDS != self.hard_max
                or tuple(self.request.scope["headers"]) != self.headers):
            _deny(503)

    async def current(self):
        self.local()
        if self.row is not None:
            current = await self.store.aget(self.row["sid"], request_execution=True)
            self.local()
            if (current is None or current.get("incarnation_id") != self.row["incarnation_id"]
                    or current.get("user_id") != self.row["user_id"]):
                _deny(401)


async def _capture(request, client=None, *, device=False, authenticate=True):
    if (request.headers.getlist(HEADER) != [MODE] or request.headers.get("origin") is not None
            or request.url.query or request.url.scheme != "https"):
        _deny()
    store = web_auth._get_store()
    if type(store) is not session_store.WebSessionStore or store._fernet is None:
        _deny(503)
    binding = _Binding(store, _issuer(), None if client is None else _client(client, device=device),
                       web_auth._secret(), session_store._enc_key(), store._fernet,
                       time.time() + ATTEMPT_SECONDS, request, tuple(request.scope["headers"]),
                       web_auth.HARD_MAX_SECONDS)
    # Freeze real transport values before IAM; no owner, SID or cookie comes
    # from a caller body or a registration's conversation session_id.
    headers = tuple(request.scope["headers"])
    raw_cookie = request.cookies.get(web_auth.COOKIE_NAME)
    if raw_cookie is not None:
        cookie_headers = request.headers.getlist("cookie")
        if sum(part.strip().startswith(web_auth.COOKIE_NAME + "=")
               for header in cookie_headers for part in header.split(";")) != 1:
            _deny(401)
        sid = web_auth._unsign(raw_cookie)
        if not sid:
            _deny(401)
        binding.row = await store.aget(sid, request_execution=True)
        if binding.row is None:
            _deny(401)
        binding.expires = min(binding.expires, binding.row["hard_expires_at"])
    authorization = request.headers.getlist("authorization")
    if len(authorization) > 1:
        _deny(401)
    if authenticate and (authorization or raw_cookie is not None):
        credentials = None
        if authorization:
            scheme, separator, token = authorization[0].partition(" ")
            if not separator or scheme.lower() != "bearer" or not _text(token, 16_384):
                _deny(401)
            credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        claims = await auth.get_web_or_bearer_user_payload(request, credentials)
        await auth.verify_user(claims)
        binding.owner = claims.get("sub")
        if not isinstance(binding.owner, str) or not binding.owner:
            _deny(401)
        binding.expires = min(binding.expires, _expiry(claims))
        if binding.row is not None and binding.row["user_id"] != binding.owner:
            _deny(401)
    await binding.current()
    if tuple(request.scope["headers"]) != headers:
        _deny(401)
    return binding


async def _issue(request, binding, payload, *, device=False):
    await binding.current()
    if (type(payload) is not dict or not session_store._valid_token(payload.get("access_token"))
            or not session_store._valid_token(payload.get("refresh_token"))):
        _deny(503)
    claims = await auth.verify_production_token(payload["access_token"])
    await auth.verify_user(claims)
    owner = claims.get("sub")
    if (not isinstance(owner, str) or not owner or len(owner) > 256
            or claims.get("iss") != binding.issuer or claims.get("azp") != binding.client
            or (binding.owner is not None and owner != binding.owner)):
        _deny(401)
    exp = _expiry(claims)
    await binding.current()
    sid = secrets.token_urlsafe(24)
    if len(_ISSUANCE_TASKS) >= ISSUANCE_LIMIT:
        _deny(429)
    worker = asyncio.create_task(binding.store.acreate(
        sid, user_id=owner, access_token=payload["access_token"],
        refresh_token=payload["refresh_token"], hard_max_seconds=binding.hard_max,
        issuing_issuer=binding.issuer, issuing_client_id=binding.client,
        request_execution=True), name="native-session-issuance")
    _ISSUANCE_TASKS.add(worker)

    def finished(task):
        _ISSUANCE_TASKS.discard(task)
        if not task.cancelled():
            task.exception()  # Observe an uncertain caller's bounded worker failure.
    worker.add_done_callback(finished)
    try:
        row = await asyncio.shield(worker)
    except asyncio.CancelledError:
        raise
    except Exception:
        # Outcome may be unknown. Never issue again, synthesize a row/cookie,
        # or revoke a credential based on an invented transaction outcome.
        _deny(503)
    await web_auth._audit("login_interactive", owner, "Native grant entered server custody")
    await binding.current()
    if (not isinstance(row, dict) or not session_store._valid_incarnation(row.get("incarnation_id"))
            or row.get("sid") != sid or row.get("user_id") != owner
            or row.get("issuing_issuer") != binding.issuer
            or row.get("issuing_client_id") != binding.client or exp <= time.time()):
        _deny(503)
    deadline = getattr(request.state, "_native_custody_deadline", 0)
    try:
        metadata = await asyncio.to_thread(binding.store.verify_native_custody,
            issued=dict(row), original=None if binding.row is None else dict(binding.row),
            valid_until=datetime.fromtimestamp(min(deadline, binding.expires, exp), timezone.utc))
    except asyncio.CancelledError:
        raise
    except session_store.SessionStoreError:
        _deny(503)
    binding.local()
    if min(deadline, exp) <= time.time():
        _deny(503)
    result = {"authenticated": True, "access_token": payload["access_token"],
              "token_type": "Bearer", "expires_in": max(0, int(min(exp, row["hard_expires_at"]) - time.time())),
              "user_id": owner, "resumed": False}
    if device:
        result["status"] = "approved"
    response = JSONResponse(result, headers={"Cache-Control": "no-store"})
    web_auth._attach_stored_session_cookie(request, metadata, response)
    return response


async def token(request):
    try:
        async with asyncio.timeout(REQUEST_SECONDS):
            if not auth._check_token_rate(request.client.host if request.client else "unknown"):
                _deny(429)
            binding = await _capture(request)
            body = await _body(request, {"session_custody", "grant_type", "client_id",
                                         "code", "code_verifier", "redirect_uri"})
            client = _client(body["client_id"])
            binding.client = client
            await binding.current()
            if (body["grant_type"] != "authorization_code"
                    or body["redirect_uri"] != _REDIRECTS[client]
                    or not _text(body["code"], 4096)
                    or not isinstance(body["code_verifier"], str)
                    or re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", body["code_verifier"]) is None):
                _deny()
            key = hashlib.sha256((client + "\0" + body["code"]).encode()).hexdigest()
            now = time.time()
            for old in tuple(_CODE_ATTEMPTS):
                if _CODE_ATTEMPTS[old] <= now:
                    del _CODE_ATTEMPTS[old]
            if key in _CODE_ATTEMPTS:
                _deny(409)
            if len(_CODE_ATTEMPTS) >= ATTEMPT_LIMIT:
                _deny(429)
            _CODE_ATTEMPTS[key] = now + ATTEMPT_SECONDS
            if len(_ISSUANCE_TASKS) >= ISSUANCE_LIMIT:
                _deny(429)
            fields = {k: v for k, v in body.items() if k != "session_custody"}
            try:
                status, payload = await device_login._default_post_form(
                    binding.issuer + "/protocol/openid-connect/token", fields)
            except asyncio.CancelledError:
                raise
            except Exception:
                _deny(503)
            if status != 200:
                _deny(401)
            return await _issue(request, binding, payload)
    except TimeoutError:
        _deny(503)


def assert_discovery(binding, discovery):
    binding.local()
    if discovery != {
        "token_endpoint": binding.issuer + "/protocol/openid-connect/token",
        "device_authorization_endpoint": binding.issuer + "/protocol/openid-connect/auth/device",
    }:
        _deny(503)


async def device_start(request):
    global _STARTS_IN_FLIGHT
    try:
        async with asyncio.timeout(REQUEST_SECONDS):
            if not auth._check_token_rate(request.client.host if request.client else "unknown"):
                _deny(429)
            binding = await _capture(request)
            body = await _body(request, {"session_custody", "client"})
            binding.client = _client(body["client"], device=True)
            await binding.current()
            now = time.time()
            for key, state in tuple(device_login._POLL_STATE.items()):
                if state.get("custody") is not None and state["custody"].expires <= now:
                    del device_login._POLL_STATE[key]
            if len(device_login._POLL_STATE) + _STARTS_IN_FLIGHT >= ATTEMPT_LIMIT:
                _deny(429)
            _STARTS_IN_FLIGHT += 1
            try:
                result = await device_login.start(binding.client,
                    request.client.host if request.client else "unknown", custody=binding)
            finally:
                _STARTS_IN_FLIGHT -= 1
            await binding.current()
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except TimeoutError:
        _deny(503)


async def device_poll(request):
    try:
        async with asyncio.timeout(REQUEST_SECONDS):
            caller = await _capture(request)
            body = await _body(request, {"session_custody", "handle"})
            if not _text(body["handle"], BODY_LIMIT):
                _deny()
            state = device_login._POLL_STATE.get(device_login._handle_digest(body["handle"]))
            if (state is None or type(state.get("custody")) is not _Binding
                    or state.get("used") or state.get("in_flight")):
                _deny()
            binding = state["custody"]
            if caller.owner != binding.owner:
                _deny(401)
            if caller.issuer != binding.issuer:
                _deny(503)
            # Claim before the first awaited authority/discovery/provider work.
            state["in_flight"] = True
            terminal = True
            try:
                def alive():
                    if device_login._POLL_STATE.get(device_login._handle_digest(body["handle"])) is not state:
                        _deny()
                await caller.current()
                await binding.current()
                alive()

                async def finish(payload):
                    alive()
                    await caller.current()
                    response = await _issue(request, binding, payload, device=True)
                    alive()
                    caller.local()
                    return response

                result = await device_login.poll(body["handle"],
                    request.client.host if request.client else "unknown",
                    custody=(binding, finish))
                alive()
                terminal = not (type(result) is dict and result.get("status") in {"pending", "slow_down"})
                return result
            finally:
                state["in_flight"] = False
                if terminal:
                    state["used"] = True
    except TimeoutError:
        _deny(503)


async def capture_logout(request):
    # FastAPI runs this before the unchanged normal Bearer dependency. Capture
    # the selected incarnation first; do not authenticate a synthetic request.
    binding = await _capture(request, authenticate=False)
    if binding.row is None:
        _deny(401)
    binding.client = _client(binding.row.get("issuing_client_id"),
                             device=binding.row.get("issuing_client_id") not in _REDIRECTS)
    if binding.row.get("issuing_issuer") != binding.issuer:
        _deny(401)
    return binding


async def logout(request, binding, claims):
    await auth.verify_user(claims)
    binding.expires = min(binding.expires, _expiry(claims))
    binding.owner = claims.get("sub")
    if (binding.owner != binding.row["user_id"] or claims.get("iss") != binding.issuer
            or claims.get("azp") != binding.client):
        _deny(401)
    await _body(request, {"session_custody"})
    await binding.current()
    selected = web_auth._session_from_row(binding.row)
    if not await web_auth._kill_session(binding.row["sid"], selected, request_execution=True):
        _deny(409)
    outcome = await web_auth._revoke_session_or_queue(selected)
    await auth._end_native_voice(request, binding.owner)
    result = await auth._finish_native_logout(claims, binding.owner, binding.client, outcome)
    binding.local()
    response = JSONResponse(result, headers={"Cache-Control": "no-store"})
    response.delete_cookie(web_auth.COOKIE_NAME, path="/", secure=True, httponly=True,
                           samesite="lax")
    return response
