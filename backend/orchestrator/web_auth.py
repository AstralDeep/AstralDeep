"""Server-side OIDC login, session cookie issuance, silent refresh, and revocation for
the web shell, plus device-flow kiosk sign-in. Backed by session_store.py, consumed
by auth.py and orchestrator.py across the authenticated surface.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import time
import threading
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import shared  # noqa: F401

logger = logging.getLogger("orchestrator.web_auth")

web_auth_router = APIRouter()

_SESSIONS: Dict[str, Dict[str, Any]] = {}
_SESSION_CACHE_LOCK = threading.RLock()
_PENDING: Dict[str, Dict[str, Any]] = {}
_DEATH_REASONS: Dict[str, str] = {}

HARD_MAX_SECONDS = int(os.getenv("OFFLINE_GRANT_MAX_DAYS", "365")) * 24 * 60 * 60
COOKIE_NAME = "astral_session"
STATE_COOKIE_NAME = "astral_oidc_state"
STATE_COOKIE_PATH = "/auth"
_STATE_COOKIE_MAX_AGE = 600
_INVALID_CALLBACK = "Sign-in could not be completed (invalid callback). Please try again."
_SCOPE = "openid profile email offline_access"
_REFRESH_WINDOW_SECONDS = 60
_CLOCK_SKEW_SECONDS = 300

_PROCESS_SECRET = secrets.token_hex(32)

_STORE = None
_STORE_FAILED = False
_CREDENTIAL_MANAGER = None


def bind_session_store(store) -> None:
    global _STORE, _STORE_FAILED
    if store is None:
        raise ValueError("session store binding is required")
    _STORE = store
    _STORE_FAILED = False


def unbind_session_store(store) -> None:
    global _STORE, _STORE_FAILED
    if _STORE is None:
        return
    if _STORE is not store:
        raise RuntimeError("session store unbind does not own the binding")
    _STORE = None
    _STORE_FAILED = False


def bind_credential_manager(manager) -> None:
    global _CREDENTIAL_MANAGER
    if manager is None:
        raise ValueError("credential manager binding is required")
    _CREDENTIAL_MANAGER = manager


def unbind_credential_manager(manager) -> None:
    global _CREDENTIAL_MANAGER
    if _CREDENTIAL_MANAGER is None:
        return
    if _CREDENTIAL_MANAGER is not manager:
        raise RuntimeError("credential manager unbind does not own the binding")
    _CREDENTIAL_MANAGER = None


def _is_mock() -> bool:
    return os.getenv("USE_MOCK_AUTH", "").strip().lower() in ("1", "true", "yes")


def _secret() -> bytes:
    explicit = os.getenv("WEB_SESSION_SECRET")
    if explicit:
        return explicit.encode()
    enc = os.getenv("WEB_SESSION_ENC_KEY") or os.getenv("OFFLINE_GRANT_ENC_KEY")
    if enc:
        try:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.kdf.hkdf import HKDF
            return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                        info=b"astral-web-cookie-hmac").derive(enc.encode())
        except Exception:
            return enc.encode()
    return _PROCESS_SECRET.encode()


def _sign(sid: str) -> str:
    mac = hmac.new(_secret(), sid.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{sid}.{mac}"


def _unsign(value: str) -> Optional[str]:
    if not value or "." not in value:
        return None
    sid, mac = value.rsplit(".", 1)
    expected = hmac.new(_secret(), sid.encode(), hashlib.sha256).hexdigest()[:32]
    return sid if hmac.compare_digest(mac, expected) else None


def _cookie_secure(request: Request) -> bool:
    from orchestrator.session_store import is_dev_mode
    return (not is_dev_mode()) or str(request.base_url).startswith("https")


def _clear_state_cookie(resp: Response) -> Response:
    resp.delete_cookie(STATE_COOKIE_NAME, path=STATE_COOKIE_PATH)
    return resp


def _state_is_bound(request: Request, state: str) -> bool:
    raw = request.cookies.get(STATE_COOKIE_NAME, "") or ""
    bound = (_unsign(raw) if raw.isascii() else None) or ""
    return bool(state) and bool(bound) and hmac.compare_digest(bound.encode(), state.encode())


def _get_store():
    global _STORE, _STORE_FAILED
    if _STORE is not None:
        return _STORE
    if not _STORE_FAILED:
        _STORE_FAILED = True
        logger.warning(
            "web_auth: application session store is not bound — durable sessions unavailable"
        )
    return None


def reset_store_for_tests() -> None:
    global _STORE, _STORE_FAILED, _IDP_OK_UNTIL
    _STORE = None
    _STORE_FAILED = False
    _IDP_OK_UNTIL = 0.0
    _DEATH_REASONS.clear()


def _keycloak_config():
    try:
        from orchestrator.auth import _get_keycloak_config
        return _get_keycloak_config()
    except Exception:
        return (
            os.getenv("KEYCLOAK_AUTHORITY", ""),
            os.getenv("KEYCLOAK_CLIENT_ID", "astral-frontend"),
            os.getenv("KEYCLOAK_CLIENT_SECRET", ""),
        )


def _validate_next(nxt: Optional[str]) -> str:
    nxt = (nxt or "").strip()
    if not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt or ":" in nxt.split("?", 1)[0]:
        return "/"
    return nxt


def _jwt_payload(token: str) -> Dict[str, Any]:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


def _token_expires_at(token: str) -> Optional[int]:
    exp = _jwt_payload(token).get("exp")
    try:
        return int(exp) if exp is not None else None
    except (TypeError, ValueError):
        return None


def _record_death(sid: str, reason: str) -> None:
    if len(_DEATH_REASONS) > 256:
        _DEATH_REASONS.clear()
    _DEATH_REASONS[sid] = reason


def _same_incarnation(first: dict | None, second: dict | None) -> bool:
    return bool(first and second and first.get("incarnation_id")
                and first.get("incarnation_id") == second.get("incarnation_id"))


def _evict_session_observation(sid: str, observed: dict | None) -> bool:
    with _SESSION_CACHE_LOCK:
        current = _SESSIONS.get(sid)
        if current is observed or _same_incarnation(current, observed):
            _SESSIONS.pop(sid, None)
            return True
        return False


def _session_from_row(row: dict, previous: dict | None = None) -> dict:
    return {
        "sid": row["sid"], "incarnation_id": row["incarnation_id"],
        "access_token": row["access_token"], "refresh_token": row["refresh_token"],
        "sub": row["user_id"], "created_at": row["interactive_anchor"],
        "issuing_issuer": row.get("issuing_issuer"),
        "issuing_client_id": row.get("issuing_client_id"),
        "resumed": previous.get("resumed", False) if _same_incarnation(previous, row) else True,
    }


def _session_by_sid(sid: str) -> Optional[Dict[str, Any]]:
    sess = _SESSIONS.get(sid)
    store = _get_store()
    if sess is not None and store is None:
        if (time.time() - sess.get("created_at", 0)) > HARD_MAX_SECONDS:
            _evict_session_observation(sid, sess)
            logger.info("web_auth: session %s exceeded 365-day cap — cleared", sid[:8])
            _record_death(sid, "hard_cap")
            return None
        return sess
    if store is None:
        return None
    row = store.get(sid)
    if row is None:
        _evict_session_observation(sid, sess)
        reason = None
        try:
            reason = store.pop_death_reason(sid)
        except AttributeError:
            pass
        if reason:
            _record_death(sid, reason)
        return None
    updated = _session_from_row(row, sess)
    with _SESSION_CACHE_LOCK:
        current = _SESSIONS.get(sid)
        if current is sess or _same_incarnation(current, updated):
            _SESSIONS[sid] = updated
    return updated


def get_session(request: Request) -> Optional[Dict[str, Any]]:
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        return None
    sid = _unsign(raw)
    if not sid:
        return None
    sess = _session_by_sid(sid)
    if sess is not None and "sid" not in sess:
        sess["sid"] = sid
    return sess


async def aget_session(request: Request) -> Optional[Dict[str, Any]]:
    return await asyncio.to_thread(get_session, request)


async def _asession_by_sid(sid: str) -> Optional[Dict[str, Any]]:
    return await asyncio.to_thread(_session_by_sid, sid)


def _session_client_id(sess: Dict[str, Any]) -> str:
    return str(_jwt_payload(sess.get("access_token", "")).get("azp", "") or "").strip()


async def _exchange_session_refresh(refresh_token: str, prior_access: str) -> dict:
    authority, web_client_id, client_secret = _keycloak_config()
    from orchestrator.session_store import SessionRefreshUnavailable
    if not authority:
        raise SessionRefreshUnavailable("session refresh authority unavailable")
    effective = _session_client_id({"access_token": prior_access}) or web_client_id
    return await _post_session_refresh(refresh_token, authority, effective,
                                       client_secret if effective == web_client_id else "")


async def _post_session_refresh(refresh_token, authority, client_id, client_secret):
    from orchestrator.session_store import SessionRefreshUnavailable
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token,
            "client_id": client_id}
    if client_secret:
        data["client_secret"] = client_secret
    async with httpx.AsyncClient(timeout=10) as client:
        async with client.stream(
            "POST", f"{authority}/protocol/openid-connect/token", data=data,
            follow_redirects=False,
        ) as resp:
            resp.raise_for_status()
            body = bytearray()
            async for chunk in resp.aiter_bytes(chunk_size=8192):
                body.extend(chunk)
                if len(body) > 65536:
                    raise SessionRefreshUnavailable("refresh response exceeds limit")
            return json.loads(body)


def _bound_session_destination(issuer, client_id):
    from orchestrator.session_store import SessionRefreshUnavailable, _binding_string
    from shared.auth_clients import allowed_azps
    authority, web_client, secret = _keycloak_config()
    if (_is_mock() or not _binding_string(issuer, 2048)
            or not _binding_string(client_id, 256)
            or issuer != authority or client_id not in allowed_azps()):
        raise SessionRefreshUnavailable("bound session authority unavailable")
    return authority, client_id, secret if client_id == web_client else ""


async def _exchange_bound_session_refresh(refresh_token, identity):
    from orchestrator import auth
    from orchestrator.session_store import (
        SessionIssuingIdentity, SessionRefreshUnavailable, _valid_token,
    )
    if type(identity) is not SessionIssuingIdentity:
        raise SessionRefreshUnavailable("bound session authority unavailable")
    destination = _bound_session_destination(identity.issuer, identity.client_id)
    async with asyncio.timeout(10):
        try:
            payload = await _post_session_refresh(refresh_token, *destination)
        except httpx.HTTPStatusError as error:
            if error.response.status_code in {400, 401}:
                raise
            raise SessionRefreshUnavailable("bound session exchange unavailable") from None
        try:
            if (not isinstance(payload, dict) or not _valid_token(payload.get("access_token"))
                    or ("refresh_token" in payload and not _valid_token(payload["refresh_token"]))):
                raise ValueError
            claims = await auth.verify_production_token(payload["access_token"])
            await auth.verify_user(claims)
            expiry = claims.get("exp")
            if (claims.get("iss") != identity.issuer or claims.get("azp") != identity.client_id
                    or claims.get("sub") != identity.owner_id or type(expiry) not in (int, float)
                    or not math.isfinite(expiry) or expiry <= time.time()
                    or _bound_session_destination(identity.issuer, identity.client_id) != destination):
                raise ValueError
        except Exception:
            raise SessionRefreshUnavailable("bound session response unavailable") from None
    return payload


async def _refresh_session(sid: str, sess: Dict[str, Any], *, on_retired=None) -> Optional[Dict[str, Any]]:
    authority, _, _ = _keycloak_config()
    store = _get_store()
    if not authority or store is None:
        return None
    observed = dict(sess)
    from orchestrator.session_store import _valid_incarnation
    if not _valid_incarnation(observed.get("incarnation_id")):
        return None
    try:
        row = await store.refresh_credential(
            sid, owner_id=observed.get("sub", ""), exchange=_exchange_session_refresh,
            bound_exchange=_exchange_bound_session_refresh,
            expected_incarnation_id=observed["incarnation_id"])
    except httpx.HTTPStatusError:
        logger.info("web_auth: refresh refused for session %s — clearing", sid[:8])
        retired = await _kill_session(sid, observed, audit_action="token_refresh_failed",
                                      description="Silent token refresh refused by the identity provider")
        if retired and on_retired is not None:
            await on_retired()
        return None
    except Exception:
        observed["refresh_token"] = ""
        try:
            current = await asyncio.to_thread(
                store.is_current_incarnation, observed.get("sub", ""),
                session_id=sid, incarnation_id=observed["incarnation_id"])
        except Exception:
            current = False
        logger.warning("web_auth: session refresh unavailable")
        return observed if current else None
    if not _same_incarnation(observed, row):
        return None
    observed["access_token"] = row["access_token"]
    observed["refresh_token"] = row["refresh_token"]
    observed["issuing_issuer"] = row.get("issuing_issuer")
    observed["issuing_client_id"] = row.get("issuing_client_id")
    return observed


async def _kill_session(sid: str, sess: Dict[str, Any], *, audit_action: Optional[str] = None,
                        description: str = "", outcome: str = "failure",
                        request_execution: bool = False) -> bool:
    incarnation = sess.get("incarnation_id")
    _evict_session_observation(sid, sess)
    store = _get_store()
    retired = store is None and incarnation is None
    if store is None and not retired:
        sess["refresh_token"] = ""
    if store is not None:
        sess["refresh_token"] = ""
        try:
            from orchestrator.session_store import _valid_incarnation
            deleted = (await store.adelete(sid, expected_incarnation_id=incarnation,
                        **({"request_execution": True} if request_execution else {}))
                       if _valid_incarnation(incarnation) else None)
            sess["refresh_token"] = "" if deleted is None else deleted["refresh_token"]
            if deleted is not None:
                sess["access_token"] = deleted["access_token"]
                sess["issuing_issuer"] = deleted.get("issuing_issuer")
                sess["issuing_client_id"] = deleted.get("issuing_client_id")
                retired = True
        except Exception:
            logger.debug("web_auth: store delete failed", exc_info=True)
    if audit_action:
        await _audit(audit_action, sess.get("sub", "anonymous"), description, outcome=outcome)
    return retired


async def _end_voice_session(request: Request, user_id: str, reason: str) -> None:
    app_state = getattr(getattr(request, "app", None), "state", None)
    voice_services = getattr(
        getattr(app_state, "orchestrator", None), "voice_services", None
    )
    if voice_services is None or not user_id:
        return
    try:
        await voice_services.end_user_voice_session(
            user_id=user_id,
            reason=reason,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(
            "web_auth: voice cleanup failed reason=%s",
            reason,
            exc_info=True,
        )


async def ensure_session(request: Request) -> Optional[Dict[str, Any]]:
    if _is_mock():
        return {"access_token": "dev-token", "refresh_token": "", "sub": "test_user",
                "created_at": time.time(), "resumed": True, "sid": "mock"}
    sess = await aget_session(request)
    if sess is None:
        return None
    sid = sess.get("sid") or _unsign(request.cookies.get(COOKIE_NAME, "")) or ""
    exp = _token_expires_at(sess.get("access_token", ""))
    if exp is None or (exp - time.time()) < _REFRESH_WINDOW_SECONDS:
        async def on_retired():
            await _end_voice_session(request, sess.get("sub", ""), "auth_expired")

        refreshed = await _refresh_session(sid, sess, on_retired=on_retired)
        if refreshed is None:
            return None
        sess = refreshed
        exp2 = _token_expires_at(sess.get("access_token", ""))
        if exp2 is not None and (time.time() - exp2) > _CLOCK_SKEW_SECONDS:
            return None
    return sess


def shell_gate(request: Request) -> Optional[str]:
    if _is_mock():
        return None
    if get_session(request) is not None:
        return None
    path = request.url.path or "/"
    query = ("?" + str(request.url.query)) if request.url.query else ""
    nxt = _validate_next(path + query)
    from urllib.parse import quote
    return f"/auth/login?next={quote(nxt, safe='')}"


def session_token(request: Request) -> str:
    if _is_mock():
        return "dev-token"
    sess = get_session(request)
    return (sess or {}).get("access_token", "") or ""


def session_resumed_flag(request: Request) -> bool:
    sess = get_session(request)
    if sess is None:
        return True
    resumed = bool(sess.get("resumed", True))
    if not resumed:
        sess["resumed"] = True
        store = _get_store()
        if store is not None and sess.get("sid"):
            try:
                from orchestrator.session_store import _valid_incarnation
                if _valid_incarnation(sess.get("incarnation_id")):
                    store.mark_resumed(sess["sid"], expected_incarnation_id=sess["incarnation_id"])
            except Exception:
                logger.debug("web_auth: mark_resumed failed", exc_info=True)
    return resumed


def session_roles(request: Request) -> list:
    if _is_mock():
        return ["admin", "user"]
    sess = get_session(request)
    return _roles_from_token((sess or {}).get("access_token", "") or "")


def session_subject(request: Request) -> str:
    if _is_mock():
        return "test_user"
    return str((get_session(request) or {}).get("sub", "") or "")


ANONYMOUS_IDENTITY = {"name": "Signed in", "role": "Guest", "initials": "A"}

MOCK_IDENTITY = {"name": "Local operator", "role": "Development session", "initials": "LO"}


def identity_from_claims(payload: dict, roles=None) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    name = str(
        payload.get("name")
        or " ".join(
            part for part in (payload.get("given_name"), payload.get("family_name")) if part
        ).strip()
        or payload.get("preferred_username")
        or ""
    ).strip()
    if not name or "@" in name:
        name = ANONYMOUS_IDENTITY["name"]
    if roles is None:
        roles = list((payload.get("realm_access") or {}).get("roles") or [])
        for client in (payload.get("resource_access") or {}).values():
            roles.extend((client or {}).get("roles") or [])
    role = "Administrator" if "admin" in roles else ("Member" if roles else "Guest")
    initials = "".join(part[0] for part in name.split()[:2] if part).upper() or "A"
    return {"name": name, "role": role, "initials": initials}


def session_identity(request: Request) -> dict:
    if _is_mock():
        return dict(MOCK_IDENTITY)
    token = (get_session(request) or {}).get("access_token", "") or ""
    return identity_from_claims(_jwt_payload(token), _roles_from_token(token))


def _roles_from_token(token: str) -> list:
    payload = _jwt_payload(token)
    if not payload:
        return []
    roles = list(payload.get("realm_access", {}).get("roles", []) or [])
    for client in (payload.get("resource_access", {}) or {}).values():
        roles.extend(client.get("roles", []) or [])
    return roles


def _pkce_pair():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(40)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _redirect_uri(request: Request) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}/auth/callback"


_IDP_OK_UNTIL = 0.0


async def _idp_reachable(authority: str) -> bool:
    global _IDP_OK_UNTIL
    if time.time() < _IDP_OK_UNTIL:
        return True
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{authority}/.well-known/openid-configuration")
        if resp.status_code < 500:
            _IDP_OK_UNTIL = time.time() + 60
            return True
    except Exception:
        logger.warning("web_auth: identity provider unreachable at %s", authority)
    return False


@web_auth_router.get("/auth/login")
async def auth_login(request: Request):
    nxt = _validate_next(request.query_params.get("next", "/"))
    if _is_mock():
        return _establish_session(request, {"access_token": "dev-token", "refresh_token": "", "sub": "test_user"}, nxt)
    authority, client_id, _secret_unused = _keycloak_config()
    if not authority:
        return _error_page(nxt, "OIDC is not configured on this server.", status=500)
    if not await _idp_reachable(authority):
        return _error_page(nxt, "The identity provider is unreachable right now. "
                                "Please try again in a moment.", status=503)
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)
    _now = time.time()
    for _stale in [k for k, v in _PENDING.items() if _now - v.get("created_at", 0) > 600]:
        _PENDING.pop(_stale, None)
    if len(_PENDING) > 4096:
        for _old in sorted(_PENDING, key=lambda k: _PENDING[k].get("created_at", 0))[: len(_PENDING) - 4096]:
            _PENDING.pop(_old, None)
    _PENDING[state] = {"code_verifier": verifier, "created_at": time.time(), "next": nxt}
    from urllib.parse import urlencode
    params = urlencode({
        "client_id": client_id, "response_type": "code", "scope": _SCOPE,
        "redirect_uri": _redirect_uri(request), "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    resp = RedirectResponse(f"{authority}/protocol/openid-connect/auth?{params}")
    # Must stay lax - Keycloak's callback is a cross-site GET
    resp.set_cookie(STATE_COOKIE_NAME, _sign(state), httponly=True, samesite="lax",
                    secure=_cookie_secure(request), max_age=_STATE_COOKIE_MAX_AGE,
                    path=STATE_COOKIE_PATH)
    return resp


@web_auth_router.get("/auth/callback")
async def auth_callback(request: Request):
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    pending = _PENDING.get(state) if state else None
    nxt = _validate_next((pending or {}).get("next", "/"))
    idp_error = request.query_params.get("error")
    if idp_error:
        desc = request.query_params.get("error_description") or idp_error
        logger.info("web_auth: IdP returned error at callback: %s", idp_error)
        return _clear_state_cookie(
            _error_page(nxt, f"Sign-in was not completed ({desc[:160]}). Please try again."))
    if not code or not pending:
        return _clear_state_cookie(_error_page(nxt, _INVALID_CALLBACK))
    # Must run before token exchange and user-switch revocation
    if not _state_is_bound(request, state or ""):
        logger.warning("web_auth: callback state is not bound to this browser — refused")
        return _clear_state_cookie(_error_page(nxt, _INVALID_CALLBACK))
    _PENDING.pop(state, None)
    prior = await aget_session(request)
    authority, client_id, client_secret = _keycloak_config()
    data = {
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": _redirect_uri(request), "client_id": client_id,
        "code_verifier": pending["code_verifier"],
    }
    if client_secret:
        data["client_secret"] = client_secret
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(f"{authority}/protocol/openid-connect/token", data=data)
        resp.raise_for_status()
        tok = resp.json()
    except Exception:
        logger.exception("web_auth: token exchange failed")
        return _clear_state_cookie(
            _error_page(nxt, "The identity provider rejected the sign-in. Please try again."))
    sub = _sub_from_jwt(tok.get("access_token", ""))

    if prior and prior.get("sub") and prior["sub"] != sub:
        prior_sid = prior.get("sid", "")
        logger.info("web_auth: user switch %s -> %s — revoking prior session", prior["sub"], sub)
        retired = await _kill_session(prior_sid, prior, audit_action="logout",
                            description="Prior session revoked by user switch on shared browser",
                            outcome="success")
        if retired:
            await _revoke_session_or_queue(prior, legacy_default_client=True)
            await _end_voice_session(request, prior.get("sub", ""), "logout")

    roles = _roles_from_token(tok.get("access_token", ""))
    if "user" not in roles and "admin" not in roles:
        await _revoke_or_queue(sub, tok.get("refresh_token", ""))
        await _audit("login_interactive", sub,
                     "Sign-in refused: account has neither the 'user' nor 'admin' role",
                     outcome="failure")
        return _clear_state_cookie(_no_access_page())

    await _audit("login_interactive", sub, "Interactive login completed; new session established")
    resp = await asyncio.to_thread(
        _establish_session,
        request,
        {"access_token": tok.get("access_token", ""), "refresh_token": tok.get("refresh_token", ""), "sub": sub},
        nxt,
    )
    return _clear_state_cookie(resp)


def _advertise_native_custody(resp: JSONResponse) -> JSONResponse:
    from orchestrator.native_session_custody import HEADER, MODE
    resp.headers[HEADER] = MODE
    return resp


@web_auth_router.get("/auth/session")
async def auth_session(request: Request):
    if _is_mock():
        return JSONResponse({"authenticated": True, "access_token": "dev-token", "resumed": True})
    sess = await ensure_session(request)
    if not sess:
        raw = request.cookies.get(COOKIE_NAME, "")
        sid = _unsign(raw) or ""
        reason = _DEATH_REASONS.pop(sid, None) or ("refresh_failed" if raw else "no_session")
        return _advertise_native_custody(JSONResponse(
            {"authenticated": False, "access_token": "", "resumed": False, "reason": reason}))
    resumed = bool(sess.get("resumed", True))
    if not resumed:
        sess["resumed"] = True
        store = _get_store()
        if store is not None and sess.get("sid"):
            try:
                from orchestrator.session_store import _valid_incarnation
                if _valid_incarnation(sess.get("incarnation_id")):
                    await store.amark_resumed(sess["sid"], expected_incarnation_id=sess["incarnation_id"])
            except Exception:
                logger.debug("web_auth: mark_resumed failed", exc_info=True)
    return _advertise_native_custody(JSONResponse({
        "authenticated": True,
        "access_token": sess.get("access_token", ""),
        "resumed": resumed,
        "user_id": sess.get("sub", ""),
    }))


@web_auth_router.post("/auth/logout")
@web_auth_router.get("/auth/logout")
async def auth_logout(request: Request):
    raw = request.cookies.get(COOKIE_NAME)
    sess = None
    if raw:
        sid = _unsign(raw)
        if sid:
            sess = await _asession_by_sid(sid)
            if sess is not None and not await _kill_session(sid, sess):
                sess = None
    if sess:
        user_id = sess.get("sub", "")
        await _end_voice_session(request, user_id, "logout")
    if sess and not _is_mock():
        revocation = await _revoke_session_or_queue(sess)
        try:
            from orchestrator.offline_grant import get_offline_grant_store
            revoked = await asyncio.to_thread(
                get_offline_grant_store().revoke_for_user,
                user_id,
            )
            if revoked:
                logger.info("web_auth: revoked %d offline grant(s) for %s at sign-out", revoked, user_id)
        except Exception:
            logger.warning("web_auth: offline-grant revocation failed at sign-out", exc_info=True)
        await _destroy_machine_credentials(user_id, "sign-out")
        remote = {"revoked": "confirmed", "queued": "queued"}.get(revocation, "unconfirmed")
        await _audit("logout", user_id, "User signed out; local session retired; "
                     f"remote refresh credential revocation {remote}")
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    if not _is_mock():
        authority, client_id, _ = _keycloak_config()
        if authority:
            from urllib.parse import urlencode
            params = urlencode({"client_id": client_id, "post_logout_redirect_uri": str(request.base_url).rstrip("/")})
            resp = RedirectResponse(f"{authority}/protocol/openid-connect/logout?{params}", status_code=303)
            resp.delete_cookie(COOKIE_NAME)
    return resp


@web_auth_router.get("/auth/error")
async def auth_error(request: Request):
    nxt = _validate_next(request.query_params.get("next", "/"))
    reason = (request.query_params.get("reason") or "Sign-in failed.")[:300]
    return _error_page(nxt, reason)


def _no_access_page() -> HTMLResponse:
    body = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>AstralDeep — no access</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0;display:grid;place-items:center;min-height:100vh;background:#0F1221;color:#F3F4F6;font-family:system-ui,sans-serif">
<div style="max-width:440px;padding:2rem;border:1px solid rgba(255,255,255,.1);border-radius:12px;background:#1A1E2E;text-align:center">
<h1 style="font-size:1.1rem;margin:0 0 .75rem">No access</h1>
<p style="font-size:.9rem;color:#9CA3AF;margin:0 0 1.25rem">Your account signed in successfully but does not have access to this
application. Ask an administrator to grant your account the <b>user</b> role, then sign in again.</p>
<a href="/auth/login?next=%2F" style="display:inline-block;padding:.6rem 1.2rem;border-radius:8px;background:#6366F1;color:#fff;text-decoration:none;font-size:.9rem">Sign in again</a>
</div></body></html>"""
    return HTMLResponse(body, status_code=403)


def _error_page(nxt: str, reason: str, status: int = 200) -> HTMLResponse:
    from html import escape
    from urllib.parse import quote
    retry = f"/auth/login?next={quote(_validate_next(nxt), safe='')}"
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>AstralDeep — sign-in</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0;display:grid;place-items:center;min-height:100vh;background:#0F1221;color:#F3F4F6;font-family:system-ui,sans-serif">
<div style="max-width:420px;padding:2rem;border:1px solid rgba(255,255,255,.1);border-radius:12px;background:#1A1E2E;text-align:center">
<h1 style="font-size:1.1rem;margin:0 0 .75rem">Sign-in problem</h1>
<p style="font-size:.9rem;color:#9CA3AF;margin:0 0 1.25rem">{escape(reason)}</p>
<a href="{escape(retry)}" style="display:inline-block;padding:.6rem 1.2rem;border-radius:8px;background:#6366F1;color:#fff;text-decoration:none;font-size:.9rem">Try again</a>
</div></body></html>"""
    return HTMLResponse(body, status_code=status)


async def _revoke_refresh_token(refresh_token: str, client_id: str | None = None,
                                *, issuing_issuer: str | None = None) -> bool:
    if issuing_issuer is not None:
        from orchestrator.session_store import _valid_token
        if not _valid_token(refresh_token):
            return False
    if not refresh_token:
        return True
    authority, web_client_id, client_secret = _keycloak_config()
    if not authority:
        return False
    effective = (client_id or "").strip() or web_client_id
    if issuing_issuer is not None:
        try:
            authority, effective, client_secret = _bound_session_destination(issuing_issuer, client_id)
        except Exception:
            return False
    data = {"token": refresh_token, "token_type_hint": "refresh_token", "client_id": effective}
    if client_secret and effective == web_client_id:
        data["client_secret"] = client_secret
    try:
        async with asyncio.timeout(10 if issuing_issuer is not None else None):
            async with httpx.AsyncClient(timeout=10) as client:
                if issuing_issuer is not None:
                    async with client.stream("POST", f"{authority}/protocol/openid-connect/revoke",
                                             data=data, follow_redirects=False) as resp:
                        return 200 <= resp.status_code < 300
                resp = await client.post(f"{authority}/protocol/openid-connect/revoke", data=data)
        return resp.status_code < 400
    except Exception:
        return False


async def _revoke_or_queue(user_id: str, refresh_token: str,
                           client_id: str | None = None, *, issuing_issuer: str | None = None) -> str:
    if issuing_issuer is not None:
        from orchestrator.session_store import _valid_token
        if not _valid_token(refresh_token):
            return "failed"
    if not refresh_token:
        return "noop"
    binding = {} if issuing_issuer is None else {"issuing_issuer": issuing_issuer}
    if await _revoke_refresh_token(refresh_token, client_id=client_id, **binding):
        return "revoked"
    store = _get_store()
    if store is not None:
        try:
            await store.aenqueue_revocation(user_id, refresh_token, client_id=client_id, **binding)
            logger.info("web_auth: IdP unreachable — refresh-token revocation queued for %s", user_id)
            return "queued"
        except Exception:
            logger.warning("web_auth: revocation enqueue failed", exc_info=True)
    logger.warning("web_auth: could not revoke or queue refresh token for %s", user_id)
    return "failed"


async def _revoke_session_or_queue(sess, *, legacy_default_client=False):
    issuer, client = sess.get("issuing_issuer"), sess.get("issuing_client_id")
    if issuer is None and client is None:
        return await _revoke_or_queue(sess.get("sub", ""), sess.get("refresh_token", ""),
            client_id=None if legacy_default_client else _session_client_id(sess) or None)
    from orchestrator.session_store import SessionIssuingIdentity, SessionStoreError
    try:
        identity = SessionIssuingIdentity(sess.get("sub", ""), issuer, client)
    except SessionStoreError:
        return "failed"
    return await _revoke_or_queue(identity.owner_id, sess.get("refresh_token", ""),
                                  client_id=identity.client_id, issuing_issuer=identity.issuer)


async def _destroy_machine_credentials(user_id: str, context: str) -> None:
    if not user_id:
        return
    try:
        manager = _CREDENTIAL_MANAGER
        if manager is None:
            raise RuntimeError("credential persistence is not bound to the application Plane")
        removed = await asyncio.to_thread(
            manager.remove_machine_credentials_for_user,
            user_id,
        )
        if removed:
            logger.info("web_auth: destroyed %d machine credential(s) for %s at %s",
                        removed, user_id, context)
    except Exception:
        logger.warning("web_auth: machine-credential revocation failed at %s", context,
                       exc_info=True)


_MAX_REVOCATION_ATTEMPTS = 30


async def process_revocation_queue_once() -> int:
    from orchestrator.session_store import SessionRevocationPageUnavailable
    store = _get_store()
    if store is None:
        return 0
    try:
        async with store.revocation_pass() as pending:
            return await _process_revocation_page(store, pending)
    except SessionRevocationPageUnavailable:
        logger.debug("web_auth: revocation queue read failed", exc_info=True)
        return 0


async def _process_revocation_page(store, pending):
    resolved = 0
    for item in pending:
        issuer = item.get("issuing_issuer")
        binding = {} if issuer is None else {"issuing_issuer": issuer}
        if await _revoke_refresh_token(item["refresh_token"],
                                       client_id=item.get("client_id"), **binding):
            await store.aresolve_revocation(item["id"])
            resolved += 1
        elif item["attempts"] >= _MAX_REVOCATION_ATTEMPTS:
            if issuer is not None:
                continue
            logger.warning("web_auth: dropping revocation for %s after %d attempts "
                           "(token will die at its natural expiry)", item["user_id"], item["attempts"])
            await store.aresolve_revocation(item["id"])
        else:
            await store.abump_revocation_attempt(item["id"])
    return resolved


def _attach_session(request: Request, payload: Dict[str, Any], resp: Response) -> str:
    sid = secrets.token_urlsafe(24)
    previous = _SESSIONS.get(sid)
    cached = {**payload, "created_at": time.time(), "sid": sid, "resumed": False}
    if not _is_mock():
        store = _get_store()
        if store is not None:
            try:
                row = store.create(sid, user_id=payload.get("sub", "anonymous"),
                             access_token=payload.get("access_token", ""),
                             refresh_token=payload.get("refresh_token", ""),
                             hard_max_seconds=HARD_MAX_SECONDS)
                cached = {**_session_from_row(row), "resumed": False}
            except Exception:
                logger.warning("web_auth: durable session persist failed — session is process-local",
                               exc_info=True)
    with _SESSION_CACHE_LOCK:
        if _SESSIONS.get(sid) is previous or _same_incarnation(_SESSIONS.get(sid), cached):
            _SESSIONS[sid] = cached
    resp.set_cookie(COOKIE_NAME, _sign(sid), httponly=True, samesite="lax",
                    secure=_cookie_secure(request), max_age=HARD_MAX_SECONDS, path="/")
    return sid


def _attach_stored_session_cookie(request: Request, row: dict, resp: Response) -> None:
    resp.set_cookie(COOKIE_NAME, _sign(row["sid"]), httponly=True, samesite="lax",
                    secure=_cookie_secure(request),
                    max_age=max(0, int(row["hard_expires_at"] - time.time())), path="/")


def _establish_session(request: Request, payload: Dict[str, Any], nxt: str) -> RedirectResponse:
    resp = RedirectResponse(_validate_next(nxt), status_code=303)
    _attach_session(request, payload, resp)
    return resp


def _sub_from_jwt(token: str) -> str:
    return _jwt_payload(token).get("sub", "anonymous") or "anonymous"


async def _audit(action: str, sub: str, description: str, *, outcome: str = "success") -> None:
    try:
        from audit.hooks import record_auth_event
        await record_auth_event(
            claims={"sub": sub or "anonymous"},
            action=action,
            description=description,
            outcome=outcome,
        )
    except Exception:
        logger.debug("web_auth: audit hook unavailable for %s", action, exc_info=True)


kiosk_router = APIRouter()

KIOSK_COOKIE = "astral_kiosk"
_KIOSK_FLOW_TTL_SECONDS = 900
_KIOSK_FLOW_MAX = 512

_KIOSK_FLOWS: Dict[str, Dict[str, Any]] = {}


def _kiosk_client_id() -> str:
    return (os.getenv("KIOSK_DEVICE_CLIENT", "") or "").strip() or "astral-watch"


def _kiosk_prune() -> None:
    now = time.time()
    for stale in [k for k, v in _KIOSK_FLOWS.items()
                  if now - v.get("created_at", 0) > _KIOSK_FLOW_TTL_SECONDS]:
        _KIOSK_FLOWS.pop(stale, None)
    if len(_KIOSK_FLOWS) > _KIOSK_FLOW_MAX:
        for old in sorted(_KIOSK_FLOWS,
                          key=lambda k: _KIOSK_FLOWS[k].get("created_at", 0))[
                              : len(_KIOSK_FLOWS) - _KIOSK_FLOW_MAX]:
            _KIOSK_FLOWS.pop(old, None)


def _kiosk_flow_handle(request: Request) -> Optional[str]:
    raw = request.cookies.get(KIOSK_COOKIE, "") or ""
    flow_id = (_unsign(raw) if raw.isascii() else None) or ""
    entry = _KIOSK_FLOWS.get(flow_id) if flow_id else None
    if not entry:
        return None
    if time.time() - entry.get("created_at", 0) > _KIOSK_FLOW_TTL_SECONDS:
        _KIOSK_FLOWS.pop(flow_id, None)
        return None
    return str(entry.get("handle", "")) or None


def _kiosk_template_resource():
    from astralprojection import template_path

    return template_path("kiosk.html")


@kiosk_router.get("/kiosk", response_class=HTMLResponse)
async def kiosk_page(request: Request):
    if await aget_session(request):
        return RedirectResponse("/", status_code=303)
    try:
        page = _kiosk_template_resource().read_text(encoding="utf-8")
    except Exception:
        logger.exception("astralprojection: kiosk template missing")
        return _error_page("/", "The sign-in page is unavailable.", status=500)
    resp = HTMLResponse(page)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@kiosk_router.post("/auth/kiosk/start")
async def kiosk_start(request: Request):
    from orchestrator import device_login
    ip = request.client.host if request.client else "unknown"
    try:
        started = await device_login.start(_kiosk_client_id(), ip)
    except device_login.DeviceLoginError as exc:
        return JSONResponse(
            {"error": getattr(exc, "code", "device_login_error"), "detail": str(exc)},
            status_code=getattr(exc, "status", 500))
    _kiosk_prune()
    flow_id = secrets.token_urlsafe(24)
    _KIOSK_FLOWS[flow_id] = {"handle": started.get("handle", ""), "created_at": time.time()}
    resp = JSONResponse({
        "user_code": started.get("user_code", ""),
        "verification_uri": started.get("verification_uri", ""),
        "qr_png_base64": started.get("qr_png_base64", ""),
        "expires_in": started.get("expires_in", 600),
        "interval": started.get("interval", 5),
    })
    resp.set_cookie(KIOSK_COOKIE, _sign(flow_id), httponly=True, samesite="strict",
                    secure=_cookie_secure(request), max_age=_KIOSK_FLOW_TTL_SECONDS,
                    path="/")
    return resp


@kiosk_router.post("/auth/kiosk/poll")
async def kiosk_poll(request: Request):
    from orchestrator import device_login
    handle = _kiosk_flow_handle(request)
    if not handle:
        return JSONResponse({"status": "restart"})
    prior = await aget_session(request)
    ip = request.client.host if request.client else "unknown"
    try:
        result = await device_login.poll(handle, ip)
    except device_login.DeviceLoginError as exc:
        code = getattr(exc, "code", "device_login_error")
        if code in ("invalid_handle", "rate_limited"):
            return JSONResponse({"status": "restart"})
        return JSONResponse({"error": code, "detail": str(exc)},
                            status_code=getattr(exc, "status", 500))

    status = str(result.get("status", ""))
    if status != "approved":
        out: Dict[str, Any] = {"status": status}
        if "interval" in result:
            out["interval"] = result["interval"]
        if status == "denied":
            out["reason"] = str(result.get("reason", ""))
        return JSONResponse(out)

    tokens = result.get("tokens", {}) or {}
    access_token = str(tokens.get("access_token", ""))
    sub = _sub_from_jwt(access_token)

    if prior and prior.get("sub") and prior["sub"] != sub:
        retired = await _kill_session(prior.get("sid", ""), prior, audit_action="logout",
                            description="Prior session revoked by user switch at the kiosk",
                            outcome="success")
        if retired:
            await _revoke_session_or_queue(prior)
            await _end_voice_session(request, prior.get("sub", ""), "logout")

    await _audit("login_interactive", sub,
                 "Interactive login completed at a kiosk; new session established")
    resp = JSONResponse({"status": "approved", "next": "/"})
    await asyncio.to_thread(
        _attach_session,
        request,
        {"access_token": access_token,
         "refresh_token": str(tokens.get("refresh_token", "")),
         "sub": sub},
        resp,
    )
    resp.delete_cookie(KIOSK_COOKIE, path="/")
    return resp
