"""Server-side OIDC Authorization-Code flow (feature 026 FR-009, upgraded by 028).

The orchestrator drives login server-side: ``/auth/login`` → Keycloak
authorize (PKCE), ``/auth/callback`` → token exchange (confidential
client_secret stays server-side), ``/auth/session`` → hands the access token
to the WS ``register_ui`` handshake, ``/auth/logout`` → revocation +
Keycloak end-session. Tokens stay server-side in a signed-cookie session.

Feature 028 (workspace-auth-revival, Part A) adds the full session
lifecycle on top of the 026 flow:

* **Durable sessions** — ``web_session`` Postgres rows (Fernet-encrypted at
  rest) survive restarts/multi-instance deploys; the module-level
  ``_SESSIONS`` dict remains as the in-process cache and the dev/mock
  fallback (FR-008, research D3).
* **Silent refresh** — :func:`ensure_session` renews the access token at
  Keycloak when it nears expiry. Refresh NEVER moves the 365-day
  interactive-login anchor (016 FR-001); at the hard cap the session dies
  and interactive login is required (FR-006/FR-007, research D2).
* **Shell gate** — :func:`shell_gate` gives ``GET /`` its redirect-to-login
  decision with a validated ``next`` destination (FR-001..FR-003, D1).
* **Sign-out revocation** — logout revokes the refresh token at Keycloak,
  revokes feature-025 offline grants, and completes locally even offline
  (queued retries — FR-012/FR-013, D5); a different user signing in on the
  same browser revokes the prior session first (FR-014, D6).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import shared  # noqa: F401 — normalizes USE_MOCK_AUTH/KEYCLOAK_* env aliases (post-VITE rename)

logger = logging.getLogger("orchestrator.web_auth")

web_auth_router = APIRouter()

# In-process session cache + dev/mock fallback: sid -> {access_token,
# refresh_token, sub, created_at, resumed}. The durable source of truth is
# the web_session table (session_store.WebSessionStore); rows are mirrored
# here on read so the hot path stays dict-cheap.
_SESSIONS: Dict[str, Dict[str, Any]] = {}
# Pending logins: state -> {code_verifier, created_at, next}
_PENDING: Dict[str, Dict[str, Any]] = {}
# sid -> why the session died ('hard_cap'), so /auth/session can report the
# contracted reason (auth-session.md) instead of a generic refresh_failed.
_DEATH_REASONS: Dict[str, str] = {}

HARD_MAX_SECONDS = int(os.getenv("OFFLINE_GRANT_MAX_DAYS", "365")) * 24 * 60 * 60
COOKIE_NAME = "astral_session"
# One-shot cookie binding an in-flight login to THIS browser: /auth/login mints
# it alongside the CSRF/PKCE state and /auth/callback refuses any state that
# does not match it. Scoped to /auth (the only two routes that touch it), so it
# is independent of the path="/" session cookie.
STATE_COOKIE_NAME = "astral_oidc_state"
STATE_COOKIE_PATH = "/auth"
# The same _PENDING lifetime the pruning sweep in auth_login enforces.
_STATE_COOKIE_MAX_AGE = 600
# Deliberately identical for every callback refusal: the page must not tell a
# forger which check rejected them.
_INVALID_CALLBACK = "Sign-in could not be completed (invalid callback). Please try again."
_SCOPE = "openid profile email offline_access"
# Refresh when the access token has less than this many seconds left (D2).
_REFRESH_WINDOW_SECONDS = 60
# ±5 min JWT clock-skew tolerance (016 clarification).
_CLOCK_SKEW_SECONDS = 300

_PROCESS_SECRET = secrets.token_hex(32)

_STORE = None
_STORE_FAILED = False


def _is_mock() -> bool:
    return os.getenv("USE_MOCK_AUTH", "").strip().lower() in ("1", "true", "yes")


def _secret() -> bytes:
    """Cookie-signing key. Must be identical across workers/restarts (FR-008):
    falls back through every documented session key before the per-process
    random secret (which only suits single-process dev).

    A dedicated ``WEB_SESSION_SECRET`` is used verbatim. When only an
    *encryption* key is available (``WEB_SESSION_ENC_KEY`` /
    ``OFFLINE_GRANT_ENC_KEY``), it is NOT used raw for signing — it is run
    through HKDF so the cookie-signing key is cryptographically separated from
    the at-rest encryption key (key separation)."""
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
    """Production posture: always mark auth cookies Secure, even if the request
    scheme reads as http behind a TLS-terminating proxy. Development keeps the
    scheme-derived value so http://localhost still works."""
    from orchestrator.session_store import is_dev_mode
    return (not is_dev_mode()) or str(request.base_url).startswith("https")


def _clear_state_cookie(resp: Response) -> Response:
    """Drop the one-shot login-state cookie. ``delete_cookie`` only matches when
    given the SAME path it was set with."""
    resp.delete_cookie(STATE_COOKIE_NAME, path=STATE_COOKIE_PATH)
    return resp


def _state_is_bound(request: Request, state: str) -> bool:
    """True when the callback's ``state`` matches the signed cookie that
    ``/auth/login`` minted for this browser.

    ``_sign`` emits ASCII only, so a non-ASCII cookie value cannot be valid and
    is refused before ``_unsign`` — whose ``hmac.compare_digest`` raises on
    non-ASCII ``str``. The state comparison runs over bytes for the same reason:
    ``state`` is attacker-supplied query text.
    """
    raw = request.cookies.get(STATE_COOKIE_NAME, "") or ""
    bound = (_unsign(raw) if raw.isascii() else None) or ""
    return bool(state) and bool(bound) and hmac.compare_digest(bound.encode(), state.encode())


def _get_store():
    """Lazily construct the durable session store (None when unavailable).

    The fail-closed production boot check lives in the orchestrator startup
    (FR-015); here a missing store degrades to the in-memory cache so unit
    tests and keyless dev environments keep working.
    """
    global _STORE, _STORE_FAILED
    if _STORE is not None or _STORE_FAILED:
        return _STORE
    try:
        from orchestrator.session_store import WebSessionStore
        _STORE = WebSessionStore()
    except Exception as exc:
        _STORE_FAILED = True
        logger.warning("web_auth: durable session store unavailable (%s) — using in-memory sessions", exc)
    return _STORE


def reset_store_for_tests() -> None:
    """Test helper: drop the cached store so monkeypatched envs re-init."""
    global _STORE, _STORE_FAILED, _IDP_OK_UNTIL
    _STORE = None
    _STORE_FAILED = False
    _IDP_OK_UNTIL = 0.0
    _DEATH_REASONS.clear()


def _keycloak_config():
    """Reuse the existing helper (authority, client_id, client_secret)."""
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
    """Open-redirect guard (D1): same-origin relative paths only."""
    nxt = (nxt or "").strip()
    if not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt or ":" in nxt.split("?", 1)[0]:
        return "/"
    return nxt


def _jwt_payload(token: str) -> Dict[str, Any]:
    """Best-effort, non-validating JWT payload decode ('' claims on failure)."""
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


# ---------------------------------------------------------------------------
# Session lookup (cache → durable store) + silent refresh
# ---------------------------------------------------------------------------

def _record_death(sid: str, reason: str) -> None:
    if len(_DEATH_REASONS) > 256:
        _DEATH_REASONS.clear()
    _DEATH_REASONS[sid] = reason


def _session_by_sid(sid: str) -> Optional[Dict[str, Any]]:
    sess = _SESSIONS.get(sid)
    if sess is not None:
        if (time.time() - sess.get("created_at", 0)) > HARD_MAX_SECONDS:
            _SESSIONS.pop(sid, None)
            store = _get_store()
            if store is not None:
                store.delete(sid)
            logger.info("web_auth: session %s exceeded 365-day cap — cleared", sid[:8])
            _record_death(sid, "hard_cap")
            return None
        return sess
    store = _get_store()
    if store is None:
        return None
    row = store.get(sid)  # enforces the hard cap itself
    if row is None:
        reason = None
        try:
            reason = store.pop_death_reason(sid)
        except AttributeError:
            pass
        if reason:
            _record_death(sid, reason)
        return None
    sess = {
        "sid": sid,
        "access_token": row["access_token"],
        "refresh_token": row["refresh_token"],
        "sub": row["user_id"],
        "created_at": row["interactive_anchor"],
        "resumed": True,  # any store re-read is by definition a resume
    }
    _SESSIONS[sid] = sess
    return sess


def get_session(request: Request) -> Optional[Dict[str, Any]]:
    """Return the live session dict for a request, enforcing the 365-day cap.

    Does NOT refresh — callers needing a guaranteed-fresh access token use
    :func:`ensure_session` (async)."""
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
    """Async twin of :func:`get_session` — the durable-store read (cache miss)
    is a blocking DB call, so async handlers run it off the event loop."""
    return await asyncio.to_thread(get_session, request)


async def _asession_by_sid(sid: str) -> Optional[Dict[str, Any]]:
    """Async twin of :func:`_session_by_sid`, run off the event loop."""
    return await asyncio.to_thread(_session_by_sid, sid)


def _session_client_id(sess: Dict[str, Any]) -> str:
    """The OIDC client that minted this session's tokens, from the ``azp`` claim.

    Ordinary web sessions come from the confidential web client, but a session
    can be minted by a different (public) first-party client — the device grant
    used by the watch broker is one such path. Keycloak only refreshes or
    revokes a token for its ISSUING client, so presenting such a refresh token
    as the web client yields ``invalid_grant``: silent refresh would kill the
    session, and sign-out would silently fail to revoke. Empty string means
    "unknown", and callers fall back to the configured web client.
    """
    return str(_jwt_payload(sess.get("access_token", "")).get("azp", "") or "").strip()


async def _refresh_session(sid: str, sess: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Silent refresh at Keycloak (D2). Returns the refreshed session or None.

    Never moves the interactive anchor; failure kills the session (the user
    must sign in interactively) and audits ``auth.token_refresh_failed``.
    """
    authority, web_client_id, client_secret = _keycloak_config()
    refresh_token = sess.get("refresh_token", "")
    if not authority or not refresh_token:
        return None
    # Refresh as the issuing client: the secret belongs to the web client alone
    # and is never sent for a public client (mirrors _revoke_refresh_token).
    effective = _session_client_id(sess) or web_client_id
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": effective}
    if client_secret and effective == web_client_id:
        data["client_secret"] = client_secret
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(f"{authority}/protocol/openid-connect/token", data=data)
        resp.raise_for_status()
        tok = resp.json()
    except httpx.HTTPStatusError:
        # Keycloak refused the refresh token (revoked/expired) — dead session.
        logger.info("web_auth: refresh refused for session %s — clearing", sid[:8])
        await _kill_session(sid, sess, audit_action="token_refresh_failed",
                            description="Silent token refresh refused by the identity provider")
        return None
    except Exception:
        # IdP unreachable: keep the session (offline tolerance) — the caller
        # may still succeed with the current token if it hasn't expired.
        logger.warning("web_auth: refresh attempt failed (IdP unreachable?)", exc_info=True)
        return sess
    sess["access_token"] = tok.get("access_token", "") or sess["access_token"]
    new_refresh = tok.get("refresh_token", "")
    if new_refresh:
        sess["refresh_token"] = new_refresh
    store = _get_store()
    if store is not None:
        try:
            await store.aupdate_tokens(sid, access_token=sess["access_token"], refresh_token=sess["refresh_token"])
        except Exception:
            logger.warning("web_auth: failed persisting refreshed tokens", exc_info=True)
    return sess


async def _kill_session(sid: str, sess: Dict[str, Any], *, audit_action: Optional[str] = None,
                        description: str = "", outcome: str = "failure") -> None:
    _SESSIONS.pop(sid, None)
    store = _get_store()
    if store is not None:
        try:
            await store.adelete(sid)
        except Exception:
            logger.debug("web_auth: store delete failed", exc_info=True)
    if audit_action:
        await _audit(audit_action, sess.get("sub", "anonymous"), description, outcome=outcome)


async def _end_voice_session(request: Request, user_id: str, reason: str) -> None:
    """Best-effort identity media fence; durable auth teardown stays primary."""

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
    """Session for this request with a guaranteed-usable access token.

    Refreshes silently when the access token is inside the refresh window.
    Returns None when there is no session, the hard cap is reached, or the
    refresh was refused (interactive login required)."""
    if _is_mock():
        return {"access_token": "dev-token", "refresh_token": "", "sub": "test_user",
                "created_at": time.time(), "resumed": True, "sid": "mock"}
    sess = await aget_session(request)
    if sess is None:
        return None
    sid = sess.get("sid") or _unsign(request.cookies.get(COOKIE_NAME, "")) or ""
    exp = _token_expires_at(sess.get("access_token", ""))
    if exp is None or (exp - time.time()) < _REFRESH_WINDOW_SECONDS:
        refreshed = await _refresh_session(sid, sess)
        if refreshed is None:
            await _end_voice_session(request, sess.get("sub", ""), "auth_expired")
            return None
        sess = refreshed
        # If the IdP was unreachable and the token is hard-expired (beyond
        # skew), the session can't serve this request.
        exp2 = _token_expires_at(sess.get("access_token", ""))
        if exp2 is not None and (time.time() - exp2) > _CLOCK_SKEW_SECONDS:
            await _end_voice_session(request, sess.get("sub", ""), "auth_expired")
            return None
    return sess


def shell_gate(request: Request) -> Optional[str]:
    """FR-001: redirect target for unauthenticated shell requests, else None.

    Cheap synchronous check (no refresh): a session that merely needs a
    refresh is allowed through — the client's ``/auth/session`` fetch
    refreshes before the WS handshake. Only a missing/dead session gates."""
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
    """Access token for the WS register_ui handshake ('' if unauthenticated)."""
    if _is_mock():
        return "dev-token"
    sess = get_session(request)
    return (sess or {}).get("access_token", "") or ""


def session_resumed_flag(request: Request) -> bool:
    """Is this page load a silent resume of an existing session?

    False only for the load immediately following interactive sign-in
    (one-shot — consuming it flips the session to resumed). The shell injects
    this for the client to echo into register_ui's ``resumed`` field, so
    ``auth.session_resumed`` keeps its 016 meaning instead of the client
    guessing from per-page-load connection state (FR-011)."""
    sess = get_session(request)
    if sess is None:
        return True
    resumed = bool(sess.get("resumed", True))
    if not resumed:
        sess["resumed"] = True
        store = _get_store()
        if store is not None and sess.get("sid"):
            try:
                store.mark_resumed(sess["sid"])
            except Exception:
                logger.debug("web_auth: mark_resumed failed", exc_info=True)
    return resumed


def session_roles(request: Request) -> list:
    """Feature 027 — roles for shell-render UX gating (settings menu groups).

    Mock auth mirrors the WS/REST mock principal (admin + user). For real
    sessions the roles are read from the access token's claims WITHOUT
    signature verification — this gates only what the shell renders; every
    admin action is still enforced server-side by the validated-JWT role
    checks (Constitution VII / spec FR-014).
    """
    if _is_mock():
        return ["admin", "user"]
    sess = get_session(request)
    return _roles_from_token((sess or {}).get("access_token", "") or "")


def _roles_from_token(token: str) -> list:
    """Realm + client roles from a JWT's claims (non-validating decode)."""
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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

# Positive IdP reachability result cached briefly so the happy path stays a
# single redirect (FR-004: an unreachable IdP must yield the bounded error
# page with a retry link, never a raw browser connection error).
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
    """Begin the OIDC Authorization-Code flow (PKCE)."""
    nxt = _validate_next(request.query_params.get("next", "/"))
    if _is_mock():
        # Dev/mock: mint a local session immediately, no Keycloak round-trip.
        return _establish_session(request, {"access_token": "dev-token", "refresh_token": "", "sub": "test_user"}, nxt)
    authority, client_id, _secret_unused = _keycloak_config()
    if not authority:
        return _error_page(nxt, "OIDC is not configured on this server.", status=500)
    if not await _idp_reachable(authority):
        return _error_page(nxt, "The identity provider is unreachable right now. "
                                "Please try again in a moment.", status=503)
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)
    # Bound the pending-auth table: expire stale CSRF/PKCE states (a login that
    # was started but never returned to the callback) and cap total size so a
    # flood of /auth/login hits can't grow it without limit.
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
    # Bind the state to THIS browser so only the agent that started the login
    # can finish it. SameSite MUST stay 'lax': the callback is a cross-site
    # top-level GET from Keycloak and 'strict' would drop the cookie there.
    resp.set_cookie(STATE_COOKIE_NAME, _sign(state), httponly=True, samesite="lax",
                    secure=_cookie_secure(request), max_age=_STATE_COOKIE_MAX_AGE,
                    path=STATE_COOKIE_PATH)
    return resp


@web_auth_router.get("/auth/callback")
async def auth_callback(request: Request):
    """Exchange the authorization code for tokens, establish a session, audit
    ``auth.login_interactive``. A valid cookie belonging to a DIFFERENT user
    triggers user-switch revocation of the prior session first (016 FR-008)."""
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    # PEEK (do not consume yet): an unbound hit on this URL — a prefetcher, a
    # corporate URL scanner, or a replay that lacks the astral_oidc_state cookie
    # — must not pop the pending PKCE entry, or the real browser arriving later
    # with the correct cookie finds nothing and its legitimate login is DoSed.
    # The entry is consumed only after the browser-binding check passes below.
    pending = _PENDING.get(state) if state else None
    # Recover the destination BEFORE any error exit — FR-003: a deep link is
    # never silently dropped, even on a denied/failed callback.
    nxt = _validate_next((pending or {}).get("next", "/"))
    idp_error = request.query_params.get("error")
    if idp_error:
        # OIDC error response (e.g. access_denied when the user cancels at the
        # IdP) — bounded recoverable page, retry preserves the destination.
        desc = request.query_params.get("error_description") or idp_error
        logger.info("web_auth: IdP returned error at callback: %s", idp_error)
        return _clear_state_cookie(
            _error_page(nxt, f"Sign-in was not completed ({desc[:160]}). Please try again."))
    if not code or not pending:
        return _clear_state_cookie(_error_page(nxt, _INVALID_CALLBACK))
    # The state must be bound to this browser by the cookie /auth/login set.
    # This runs BEFORE the token exchange and BEFORE the user-switch revocation
    # below, so a forged callback can neither spend an authorization code nor
    # destroy the session of whoever is signed in here.
    if not _state_is_bound(request, state or ""):
        logger.warning("web_auth: callback state is not bound to this browser — refused")
        return _clear_state_cookie(_error_page(nxt, _INVALID_CALLBACK))
    # Bound and valid — NOW consume the one-shot entry (a replay of this exact
    # bound callback finds nothing and is refused above).
    _PENDING.pop(state, None)
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

    # D6 — user-switch revocation: a live session for someone else on this
    # browser is revoked (session + refresh token) before the new one starts.
    prior = await aget_session(request)
    if prior and prior.get("sub") and prior["sub"] != sub:
        prior_sid = prior.get("sid", "")
        logger.info("web_auth: user switch %s -> %s — revoking prior session", prior["sub"], sub)
        await _revoke_or_queue(prior.get("sub", ""), prior.get("refresh_token", ""))
        await _kill_session(prior_sid, prior, audit_action="logout",
                            description="Prior session revoked by user switch on shared browser",
                            outcome="success")
        await _end_voice_session(request, prior.get("sub", ""), "logout")

    # FR-005: entry requires a Keycloak-issued 'user' or 'admin' role. An
    # authenticated account with neither gets an explicit no-access outcome —
    # no session is established and the refresh credential is revoked.
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
    # After _establish_session so the session cookie stays the FIRST Set-Cookie.
    return _clear_state_cookie(resp)


@web_auth_router.get("/auth/session")
async def auth_session(request: Request):
    """Report the current session/token for the WS handshake — refresh-aware
    (D2/D4): an access token inside the refresh window is renewed before
    being handed out, so reconnects after the token TTL recover silently."""
    if _is_mock():
        return JSONResponse({"authenticated": True, "access_token": "dev-token", "resumed": True})
    sess = await ensure_session(request)
    if not sess:
        raw = request.cookies.get(COOKIE_NAME, "")
        sid = _unsign(raw) or ""
        reason = _DEATH_REASONS.pop(sid, None) or ("refresh_failed" if raw else "no_session")
        return JSONResponse({"authenticated": False, "access_token": "", "resumed": False, "reason": reason})
    resumed = bool(sess.get("resumed", True))
    if not resumed:
        # One-shot: only the fetch immediately following interactive login
        # reports resumed=false; every later page load is a silent resume
        # (016 audit semantics — the client echoes this in register_ui).
        sess["resumed"] = True
        store = _get_store()
        if store is not None and sess.get("sid"):
            try:
                await store.amark_resumed(sess["sid"])
            except Exception:
                logger.debug("web_auth: mark_resumed failed", exc_info=True)
    return JSONResponse({
        "authenticated": True,
        "access_token": sess.get("access_token", ""),
        "resumed": resumed,
        "user_id": sess.get("sub", ""),
    })


@web_auth_router.post("/auth/logout")
@web_auth_router.get("/auth/logout")
async def auth_logout(request: Request):
    """Sign-out with server-side invalidation (FR-012/FR-013, research D5).

    Order: end the server session unconditionally → best-effort refresh-token
    revocation at Keycloak (queued for retry when offline) → revoke the
    user's feature-025 offline grants → destroy the user's feature-063
    machine credentials → audit → Keycloak end-session redirect
    (best-effort). Local sign-out never blocks on the IdP."""
    raw = request.cookies.get(COOKIE_NAME)
    sess = None
    if raw:
        sid = _unsign(raw)
        if sid:
            sess = await _asession_by_sid(sid)
            if sess is None:
                _SESSIONS.pop(sid, None)
            else:
                await _kill_session(sid, sess)  # unconditional local sign-out
    if sess:
        user_id = sess.get("sub", "")
        await _end_voice_session(request, user_id, "logout")
    if sess and not _is_mock():
        # Revoke as the issuing client — a session minted by a public
        # first-party client cannot be revoked as the confidential web client.
        await _revoke_or_queue(user_id, sess.get("refresh_token", ""),
                               client_id=_session_client_id(sess) or None)
        try:
            from orchestrator.offline_grant import OfflineGrantStore
            revoked = await asyncio.to_thread(
                lambda: OfflineGrantStore().revoke_for_user(user_id))
            if revoked:
                logger.info("web_auth: revoked %d offline grant(s) for %s at sign-out", revoked, user_id)
        except Exception:
            logger.warning("web_auth: offline-grant revocation failed at sign-out", exc_info=True)
        await _destroy_machine_credentials(user_id, "sign-out")
        await _audit("logout", user_id, "User signed out; session and refresh credential revoked")
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
    """Bounded, ungated sign-in error page (FR-004) — never auto-redirects."""
    nxt = _validate_next(request.query_params.get("next", "/"))
    reason = (request.query_params.get("reason") or "Sign-in failed.")[:300]
    return _error_page(nxt, reason)


def _no_access_page() -> HTMLResponse:
    """FR-005: explicit, bounded no-access outcome for a signed-in account
    holding neither the 'user' nor the 'admin' role. Ungated, no loop."""
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


# ---------------------------------------------------------------------------
# Revocation (D5) — best-effort with offline-tolerant queue
# ---------------------------------------------------------------------------

async def _revoke_refresh_token(refresh_token: str, client_id: str | None = None) -> bool:
    """POST the refresh token to Keycloak's RFC 7009 revocation endpoint.

    ``client_id`` overrides the configured web client for tokens minted to a
    different first-party client (feature 044 native logout): Keycloak only
    revokes a token for its issuing client, and the native clients
    (astral-desktop / astral-mobile) are PUBLIC clients — no secret is sent
    for them."""
    if not refresh_token:
        return True
    authority, web_client_id, client_secret = _keycloak_config()
    if not authority:
        return False
    effective = (client_id or "").strip() or web_client_id
    data = {"token": refresh_token, "token_type_hint": "refresh_token", "client_id": effective}
    if client_secret and effective == web_client_id:
        data["client_secret"] = client_secret
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{authority}/protocol/openid-connect/revoke", data=data)
        return resp.status_code < 400
    except Exception:
        return False


async def _revoke_or_queue(user_id: str, refresh_token: str,
                           client_id: str | None = None) -> str:
    """Revoke now or queue for the background retrier.

    Returns the outcome — ``"revoked" | "queued" | "failed" | "noop"`` — for
    the 044 native-logout endpoint to report; the web logout path ignores it
    (behavior unchanged)."""
    if not refresh_token:
        return "noop"
    if await _revoke_refresh_token(refresh_token, client_id=client_id):
        return "revoked"
    store = _get_store()
    if store is not None:
        try:
            await store.aenqueue_revocation(user_id, refresh_token, client_id=client_id)
            logger.info("web_auth: IdP unreachable — refresh-token revocation queued for %s", user_id)
            return "queued"
        except Exception:
            logger.warning("web_auth: revocation enqueue failed", exc_info=True)
    logger.warning("web_auth: could not revoke or queue refresh token for %s", user_id)
    return "failed"


async def _destroy_machine_credentials(user_id: str, context: str) -> None:
    """Feature 063 FR-015: a sign-out destroys the user's stored remote-machine
    credentials, riding the same revocation flow as the refresh token and the
    feature-025 offline grants. Fail-open — credential cleanup must never
    block a local sign-out. Shared by the web and 044 native logout legs."""
    if not user_id:
        return
    try:
        from orchestrator.credential_manager import CredentialManager
        from shared.database import Database
        removed = await asyncio.to_thread(
            lambda: CredentialManager(db=Database()).remove_machine_credentials_for_user(user_id))
        if removed:
            logger.info("web_auth: destroyed %d machine credential(s) for %s at %s",
                        removed, user_id, context)
    except Exception:
        logger.warning("web_auth: machine-credential revocation failed at %s", context,
                       exc_info=True)


_MAX_REVOCATION_ATTEMPTS = 30


async def process_revocation_queue_once() -> int:
    """Drain pending offline revocations (called by the orchestrator's
    background worker). Returns how many were resolved this pass."""
    store = _get_store()
    if store is None:
        return 0
    resolved = 0
    try:
        pending = await store.apending_revocations()
    except Exception:
        logger.debug("web_auth: revocation queue read failed", exc_info=True)
        return 0
    for item in pending:
        if await _revoke_refresh_token(item["refresh_token"],
                                       client_id=item.get("client_id")):
            await store.aresolve_revocation(item["id"])
            resolved += 1
        elif item["attempts"] >= _MAX_REVOCATION_ATTEMPTS:
            logger.warning("web_auth: dropping revocation for %s after %d attempts "
                           "(token will die at its natural expiry)", item["user_id"], item["attempts"])
            await store.aresolve_revocation(item["id"])
        else:
            await store.abump_revocation_attempt(item["id"])
    return resolved


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _attach_session(request: Request, payload: Dict[str, Any], resp: Response) -> str:
    """Mint the durable session for ``payload`` and set the signed cookie on ``resp``.

    Split out of :func:`_establish_session` so a redirect flow and a JSON flow
    can share ONE copy of the cookie policy rather than restating it. Returns
    the new session id. Blocking (durable persist) — call it off the event loop.
    """
    sid = secrets.token_urlsafe(24)
    _SESSIONS[sid] = {**payload, "created_at": time.time(), "sid": sid, "resumed": False}
    if not _is_mock():
        store = _get_store()
        if store is not None:
            try:
                store.create(sid, user_id=payload.get("sub", "anonymous"),
                             access_token=payload.get("access_token", ""),
                             refresh_token=payload.get("refresh_token", ""),
                             hard_max_seconds=HARD_MAX_SECONDS)
            except Exception:
                logger.warning("web_auth: durable session persist failed — session is process-local",
                               exc_info=True)
    resp.set_cookie(COOKIE_NAME, _sign(sid), httponly=True, samesite="lax",
                    secure=_cookie_secure(request), max_age=HARD_MAX_SECONDS, path="/")
    return sid


def _establish_session(request: Request, payload: Dict[str, Any], nxt: str) -> RedirectResponse:
    resp = RedirectResponse(_validate_next(nxt), status_code=303)
    _attach_session(request, payload, resp)
    return resp


def _sub_from_jwt(token: str) -> str:
    """Best-effort, non-validating sub extraction (validation happens via JWKS
    in validate_token when register_ui arrives)."""
    return _jwt_payload(token).get("sub", "anonymous") or "anonymous"


async def _audit(action: str, sub: str, description: str, *, outcome: str = "success") -> None:
    """Record an auth lifecycle event (fixes the 026 signature mismatch that
    silently dropped ``auth.login_interactive`` from this module)."""
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


# ---------------------------------------------------------------------------
# Feature 068 — kiosk sign-in (browser terminals with no keyboard)
#
# A second, flag-gated entry point beside /auth/login. It shows an RFC 8628
# device code as a QR — the same broker the watch uses, unchanged — next to the
# ordinary Keycloak redirect, so a terminal that cannot type is signed in from
# a phone. GET / is untouched: feature 028's redirect-straight-to-Keycloak
# posture is byte-identical, and with the flag off this router is never
# included at all.
#
# Two invariants make this safe to expose on an unattended public terminal:
#
#   1. The device handle NEVER reaches the browser. /api/auth/device/poll
#      relays raw access/refresh tokens to its caller by design (the watch is
#      native and keeps them in the keychain). Here the handle is held
#      server-side and referenced by a signed, HttpOnly, SameSite=Strict
#      cookie, so page script cannot redeem it for tokens.
#   2. The browser only ever receives a status. On approval the session is
#      minted server-side through the same helper /auth/callback uses.
# ---------------------------------------------------------------------------

kiosk_router = APIRouter()

KIOSK_COOKIE = "astral_kiosk"
_KIOSK_FLOW_TTL_SECONDS = 900
_KIOSK_FLOW_MAX = 512

# flow_id -> {"handle": str, "created_at": float}. Process-local, like the
# broker's own poll state and the _PENDING login table above.
_KIOSK_FLOWS: Dict[str, Dict[str, Any]] = {}


def _kiosk_client_id() -> str:
    """The PUBLIC device-grant client the kiosk signs in with.

    Defaults to ``astral-watch`` — the watch's client is already a public
    device-grant client in both allow-lists, so the kiosk needs no new realm
    configuration. Set ``KIOSK_DEVICE_CLIENT`` to a dedicated client to make
    kiosk and watch sessions separately revocable and distinguishable in audit.

    Whatever it is, it must appear in BOTH ``KEYCLOAK_DEVICE_CLIENTS`` and
    ``KEYCLOAK_ALLOWED_AZP`` (the broker requires the intersection, and the
    WebSocket handshake would refuse the resulting token), and it can never be
    the confidential web client — the device grant refuses that by construction.
    """
    # Strip BEFORE falling back: a whitespace-only value must not resolve to an
    # empty client id, which the broker would refuse as unknown_client.
    return (os.getenv("KIOSK_DEVICE_CLIENT", "") or "").strip() or "astral-watch"


def _kiosk_prune() -> None:
    """Bound the in-flight flow table (mirrors the _PENDING sweep)."""
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
    """The live device handle bound to this browser, or None."""
    raw = request.cookies.get(KIOSK_COOKIE, "") or ""
    flow_id = (_unsign(raw) if raw.isascii() else None) or ""
    entry = _KIOSK_FLOWS.get(flow_id) if flow_id else None
    if not entry:
        return None
    if time.time() - entry.get("created_at", 0) > _KIOSK_FLOW_TTL_SECONDS:
        _KIOSK_FLOWS.pop(flow_id, None)
        return None
    return str(entry.get("handle", "")) or None


def _kiosk_template_path() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "webrender", "templates", "kiosk.html")


@kiosk_router.get("/kiosk", response_class=HTMLResponse)
async def kiosk_page(request: Request):
    """The two-column kiosk sign-in page. Unauthenticated by definition."""
    if await aget_session(request):
        return RedirectResponse("/", status_code=303)
    try:
        with open(_kiosk_template_path(), "r", encoding="utf-8") as fh:
            page = fh.read()
    except Exception:
        logger.exception("web_auth: kiosk template missing")
        return _error_page("/", "The sign-in page is unavailable.", status=500)
    resp = HTMLResponse(page)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@kiosk_router.post("/auth/kiosk/start")
async def kiosk_start(request: Request):
    """Begin a device flow and bind it to this browser. Returns display fields only."""
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
    """Poll the bound device flow; on approval mint the cookie session here.

    The browser receives a status and nothing else — never token material.
    """
    from orchestrator import device_login
    handle = _kiosk_flow_handle(request)
    if not handle:
        return JSONResponse({"status": "restart"})
    ip = request.client.host if request.client else "unknown"
    try:
        result = await device_login.poll(handle, ip)
    except device_login.DeviceLoginError as exc:
        # A consumed or unknown handle just means "start over" for a kiosk.
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

    # A kiosk is a shared browser by definition — retire a live session that
    # belongs to somebody else before minting this one (mirrors /auth/callback).
    prior = await aget_session(request)
    if prior and prior.get("sub") and prior["sub"] != sub:
        await _revoke_or_queue(prior.get("sub", ""), prior.get("refresh_token", ""),
                               client_id=_session_client_id(prior) or None)
        await _kill_session(prior.get("sid", ""), prior, audit_action="logout",
                            description="Prior session revoked by user switch at the kiosk",
                            outcome="success")
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
