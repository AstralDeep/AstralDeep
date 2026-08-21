"""Coverage for the fix/code-review-audit-hardening security fixes.

These tests pin the newly-added defensive branches that the original PR left
uncovered (diff-cover gate). They bind the real Orchestrator methods onto
minimal fakes (see ``test_inprocess_dispatch.py`` for the pattern) and drive the
small standalone helpers directly — no Postgres, no network.

Covered:
- ``orchestrator.auth.verify_admin`` empty-principal 403 (fail closed).
- ``Orchestrator.register_agent`` skill-scope validation (unknown + empty scope).
- ``Orchestrator._execute_in_process`` deep-copies args + scrubs ``_credentials``.
- The stream subscribe hard-blocks and poll loop's shared authorization refusal.
- ``Orchestrator.validate_token`` issuer (``iss``) mismatch rejection.
- ``ToolPermissionManager._safe_flip_allowed`` (public/private/cache/fail-closed).
- ``web_auth._secret`` key-separation branches + ``auth_login`` pending pruning.
- ``orchestrator.projection_surfaces.agents.handle_safe_set`` owner path + notices.
"""
from __future__ import annotations

import asyncio
import sys
import threading
import types
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def test_audit_recorder_close_joins_inflight_retry(tmp_path):
    """Shutdown waits for a retry insert before Plane can close its pool."""
    from audit.recorder import Recorder
    from audit.schemas import AuditEventCreate

    event = AuditEventCreate(
        actor_user_id="audit-close-user",
        auth_principal="audit-close-user",
        event_class="auth",
        action_type="auth.close_test",
        description="close fencing",
        correlation_id="11111111-1111-4111-8111-111111111111",
        outcome="success",
        inputs_meta={},
        started_at=datetime.now(timezone.utc),
    )
    retry_path = tmp_path / "pending.jsonl"
    retry_path.write_text(event.model_dump_json() + "\n", encoding="utf-8")
    entered = threading.Event()
    release = threading.Event()

    class _BlockingRepository:
        def insert(self, _event):
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError("test did not release retry insert")
            return SimpleNamespace()

    recorder = Recorder(_BlockingRepository(), retry_queue=retry_path)

    async def _exercise():
        recorder._ensure_drain_task()  # noqa: SLF001 - lifecycle regression seam
        assert await asyncio.to_thread(entered.wait, 5)
        close_task = asyncio.create_task(recorder.close())
        await asyncio.sleep(0)
        assert close_task.done() is False
        release.set()
        await asyncio.wait_for(close_task, timeout=5)
        with pytest.raises(RuntimeError, match="recorder is closing"):
            await recorder.record(event)

    asyncio.run(_exercise())
    assert retry_path.read_text(encoding="utf-8") == ""


# ---------------------------------------------------------------------------
# auth.verify_admin — empty principal must 403 (fail closed)
# ---------------------------------------------------------------------------
async def test_verify_admin_empty_principal_denied():
    from fastapi import HTTPException

    from orchestrator.auth import verify_admin

    with pytest.raises(HTTPException) as exc:
        await verify_admin({})
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# register_agent — declared-scope validation branches
# ---------------------------------------------------------------------------
class _NoopPerms:
    def register_tool_scopes(self, *a, **k):
        self.last = (a, k)

    def cleanup_stale_tool_overrides(self, *a, **k):
        pass


class _RegFakeOrch:
    """Minimal surface for binding ``Orchestrator.register_agent``."""

    def __init__(self):
        self.agents = {}
        self.agent_cards = {}
        self.agent_capabilities = {}
        self._streamable_tools = {}
        self.security_flags = {}
        self.tool_permissions = _NoopPerms()
        self.security_analyzer = SimpleNamespace(analyze_agent=lambda card: {})
        self.user_agent_registry = SimpleNamespace(
            get_agent_ownership=lambda _agent_id: None,
        )

    def _is_draft_agent(self, agent_id):
        # Return True so register_agent returns before the UI broadcast loop.
        return True


async def test_register_agent_validates_skill_scopes(monkeypatch):
    monkeypatch.setenv("DEFAULT_AGENT_OWNER", "")  # skip auto-ownership branch
    # Empty (not delete) so the orchestrator import's load_dotenv(override=False)
    # cannot repopulate it — an empty key takes the keyless dev path.
    monkeypatch.setenv("AGENT_API_KEY", "")
    monkeypatch.setenv("ASTRAL_ENV", "development")
    from orchestrator.orchestrator import Orchestrator

    fake = _RegFakeOrch()
    fake.register_agent = types.MethodType(Orchestrator.register_agent, fake)

    skills = [
        # Unknown declared scope -> warning branch (orchestrator.py:706-711).
        SimpleNamespace(id="bad_tool", description="d", input_schema={},
                        scope="tools:bogus"),
        # Empty declared scope -> debug/default branch (orchestrator.py:712-716).
        SimpleNamespace(id="empty_tool", description="d", input_schema={},
                        scope=""),
        # Valid scope -> neither branch.
        SimpleNamespace(id="ok_tool", description="d", input_schema={},
                        scope="tools:read"),
    ]
    card = SimpleNamespace(agent_id="cov-agent-1", name="Cov", skills=skills)
    msg = SimpleNamespace(agent_card=card, api_key="")  # dev mode -> keyless

    await fake.register_agent(None, msg)

    # Unknown scope is still mapped verbatim; empty defaults to tools:read.
    scope_map = fake.tool_permissions.last[0][1]
    assert scope_map["bad_tool"] == "tools:bogus"
    assert scope_map["empty_tool"] == "tools:read"
    assert scope_map["ok_tool"] == "tools:read"


# ---------------------------------------------------------------------------
# _execute_in_process — deep-copies args and scrubs _credentials
# ---------------------------------------------------------------------------
class _CredScrubFakeOrch:
    def __init__(self):
        self.local_agents = {}
        self.pending_requests = {}
        self.pending_ui_sockets = {}
        self.stream_manager = None
        # 056: dispatch-context bookkeeping surface used by _execute_in_process
        self._dispatch_context = {}
        from orchestrator.orchestrator import Orchestrator as _O
        self._register_dispatch_context = types.MethodType(
            _O._register_dispatch_context, self)

    async def handle_agent_message(self, websocket, message):
        # Route the agent's loopback frame to the pending future (mirrors prod).
        from shared.protocol import MCPResponse, Message

        msg = Message.from_json(message)
        if isinstance(msg, MCPResponse):
            fut = self.pending_requests.get(msg.request_id)
            if fut is not None and not fut.done():
                fut.set_result(msg)


async def test_execute_in_process_scrubs_credentials():
    from orchestrator.orchestrator import Orchestrator
    from shared.protocol import MCPResponse

    fake = _CredScrubFakeOrch()

    class _Agent:
        async def handle_mcp_request(self, websocket, request):
            # The agent decrypts credentials into its own args copy and writes a
            # response frame back over the loopback (which the orchestrator
            # routes to the pending future). The orchestrator must not retain
            # that plaintext in the caller's dict.
            await websocket.send_text(
                MCPResponse(request_id=request.request_id, result={"ok": True}).to_json())

    fake.local_agents["a1"] = _Agent()
    fake._execute_in_process = types.MethodType(Orchestrator._execute_in_process, fake)

    original = {"foo": "bar", "_credentials": "TOP-SECRET"}
    resp = await fake._execute_in_process("a1", "tool", original, timeout=5.0)

    # The caller's dict is untouched (deep copy isolates it).
    assert original["_credentials"] == "TOP-SECRET"
    assert resp is not None


async def test_execute_in_process_scrub_swallows_error():
    """The credential-scrub finally is defensive: a pop() that raises is swallowed."""
    from orchestrator.orchestrator import Orchestrator
    from shared.protocol import MCPResponse

    fake = _CredScrubFakeOrch()

    class _Agent:
        async def handle_mcp_request(self, websocket, request):
            await websocket.send_text(
                MCPResponse(request_id=request.request_id, result={"ok": True}).to_json())

    fake.local_agents["a1"] = _Agent()
    fake._execute_in_process = types.MethodType(Orchestrator._execute_in_process, fake)

    class _BadDict(dict):
        def pop(self, *a, **k):  # deepcopy preserves the subclass type
            raise RuntimeError("scrub boom")

    # The scrub's pop() raises but the except/pass keeps the call from failing.
    resp = await fake._execute_in_process(
        "a1", "tool", _BadDict({"foo": "bar"}), timeout=5.0)
    assert resp is not None and resp.error is None


# ---------------------------------------------------------------------------
# Stream paths — hard security-flag block branches
# ---------------------------------------------------------------------------
class _StreamFakeOrch:
    def __init__(self, agent_id, tool_name):
        self.security_flags = {agent_id: {tool_name: {"blocked": True}}}
        self._streamable_tools = {
            tool_name: {"agent_id": agent_id, "kind": "push",
                        "default_interval": 2, "min_interval": 1, "max_interval": 30}
        }
        self._stream_tasks = {}
        self._stream_subs = {}
        self._ws_active_chat = {}
        self.sent = []

    async def _safe_send(self, websocket, data):
        import json as _json
        self.sent.append(_json.loads(data))

    def _get_user_id(self, websocket):
        return "u1"


def _bind_stream(fake):
    from orchestrator.orchestrator import Orchestrator

    fake._tool_security_blocked = types.MethodType(Orchestrator._tool_security_blocked, fake)
    fake._run_gate_stack = types.MethodType(Orchestrator._run_gate_stack, fake)
    fake._authorize_and_prepare = types.MethodType(
        Orchestrator._authorize_and_prepare, fake)
    fake._handle_push_stream_subscribe = types.MethodType(
        Orchestrator._handle_push_stream_subscribe, fake)
    fake._handle_stream_subscribe = types.MethodType(
        Orchestrator._handle_stream_subscribe, fake)
    fake._stream_loop = types.MethodType(Orchestrator._stream_loop, fake)


async def test_push_stream_subscribe_blocked():
    fake = _StreamFakeOrch("a1", "tstream")
    _bind_stream(fake)
    await fake._handle_push_stream_subscribe(
        object(), "chat-1", {"tool_name": "tstream", "params": {}}, "u1")
    assert fake.sent and fake.sent[-1]["type"] == "stream_error"
    assert fake.sent[-1]["payload"]["code"] == "blocked"


async def test_stream_subscribe_blocked():
    fake = _StreamFakeOrch("a1", "tstream")
    # Poll-form cfg for the legacy subscribe path.
    fake._streamable_tools["tstream"]["kind"] = "poll"
    _bind_stream(fake)
    await fake._handle_stream_subscribe(object(), {"tool_name": "tstream"})
    assert fake.sent and fake.sent[-1]["type"] == "stream_error"
    assert "system-blocked" in fake.sent[-1]["error"]


async def test_stream_loop_blocked_breaks():
    fake = _StreamFakeOrch("a1", "tstream")
    _bind_stream(fake)
    # The shared authorization boundary sees the flag on the first iteration,
    # so the poll loop sends one refusal and exits. Keep this bounded so a
    # stale fake or swallowed authorization error cannot hang the entire suite.
    await asyncio.wait_for(
        fake._stream_loop(object(), "tstream", "a1", 1, {}),
        timeout=1.0,
    )
    assert fake.sent and fake.sent[-1]["type"] == "stream_error"
    assert "system-blocked" in fake.sent[-1]["error"]


def test_tool_security_blocked_helper():
    fake = _StreamFakeOrch("a1", "tstream")
    _bind_stream(fake)
    assert fake._tool_security_blocked("a1", "tstream") is True
    assert fake._tool_security_blocked("a1", "other") is False
    assert fake._tool_security_blocked(None, "tstream") is False


# ---------------------------------------------------------------------------
# validate_token — issuer (iss) mismatch rejection
# ---------------------------------------------------------------------------
async def test_validate_token_rejects_iss_mismatch(monkeypatch):
    import shared.jwks_cache as jwks_cache
    from orchestrator import orchestrator as orch_mod
    from orchestrator.orchestrator import Orchestrator

    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.setenv("KEYCLOAK_AUTHORITY", "https://kc.example/realms/astral")
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", "astral-frontend")

    async def _fake_get_jwks(url, token=None):
        return {}

    monkeypatch.setattr(jwks_cache, "get_jwks", _fake_get_jwks)
    monkeypatch.setattr(
        orch_mod.jose_jwt, "decode",
        lambda *a, **k: {"iss": "https://evil.example/realms/other", "azp": "astral-frontend"},
    )

    fake = SimpleNamespace()
    fake.validate_token = types.MethodType(Orchestrator.validate_token, fake)
    result = await fake.validate_token("a.b.c")
    assert result is None


# ---------------------------------------------------------------------------
# tool_permissions._safe_flip_allowed — public/private/cache/fail-closed
# ---------------------------------------------------------------------------
class _SafeFlipAgentRepository:
    def __init__(self, get_ownership):
        self._get_ownership = get_ownership

    def get_ownership(self, _transaction, *, agent_id):
        ownership = self._get_ownership(agent_id)
        if ownership is None:
            return None
        return SimpleNamespace(is_public=bool(ownership["is_public"]))


class _SafeFlipPlaneRuntime:
    def __init__(self, agents):
        self.repositories = SimpleNamespace(
            tool_policy_state=object(),
            agents=agents,
        )

    @contextmanager
    def transaction(self):
        yield object()


def _pm_with_db(db):
    from orchestrator.tool_permissions import ToolPermissionManager

    runtime = _SafeFlipPlaneRuntime(
        _SafeFlipAgentRepository(db.get_agent_ownership),
    )
    return ToolPermissionManager(plane_runtime=runtime)


def test_safe_flip_allowed_public_agent():
    pm = _pm_with_db(SimpleNamespace(
        get_agent_ownership=lambda aid: {"is_public": True}))
    assert pm._safe_flip_allowed("pub") is True


def test_safe_flip_allowed_private_agent():
    pm = _pm_with_db(SimpleNamespace(
        get_agent_ownership=lambda aid: {"is_public": False}))
    assert pm._safe_flip_allowed("priv") is False


def test_safe_flip_allowed_no_ownership_record():
    pm = _pm_with_db(SimpleNamespace(get_agent_ownership=lambda aid: None))
    assert pm._safe_flip_allowed("builtin") is True


def test_safe_flip_allowed_cache_hit():
    calls = {"n": 0}

    def _own(aid):
        calls["n"] += 1
        return {"is_public": True}

    pm = _pm_with_db(SimpleNamespace(get_agent_ownership=_own))
    assert pm._safe_flip_allowed("pub") is True
    assert pm._safe_flip_allowed("pub") is True  # served from the 30s cache
    assert calls["n"] == 1  # second call did not re-hit the DB


def test_safe_flip_allowed_fails_closed_on_error():
    def _raise(aid):
        raise RuntimeError("db down")

    pm = _pm_with_db(SimpleNamespace(get_agent_ownership=_raise))
    assert pm._safe_flip_allowed("boom") is False


# ---------------------------------------------------------------------------
# web_auth._secret — key separation branches
# ---------------------------------------------------------------------------
def test_secret_explicit_web_session_secret(monkeypatch):
    from orchestrator import web_auth

    monkeypatch.setenv("WEB_SESSION_SECRET", "explicit-signing-key")
    monkeypatch.delenv("WEB_SESSION_ENC_KEY", raising=False)
    monkeypatch.delenv("OFFLINE_GRANT_ENC_KEY", raising=False)
    assert web_auth._secret() == b"explicit-signing-key"


def test_secret_hkdf_from_enc_key(monkeypatch):
    from orchestrator import web_auth

    monkeypatch.delenv("WEB_SESSION_SECRET", raising=False)
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", "encryption-key-only")
    monkeypatch.delenv("OFFLINE_GRANT_ENC_KEY", raising=False)
    derived = web_auth._secret()
    # HKDF separates the signing key from the raw encryption key.
    assert derived != b"encryption-key-only"
    assert len(derived) == 32


def test_secret_hkdf_failure_falls_back(monkeypatch):
    from cryptography.hazmat.primitives.kdf import hkdf as hkdf_mod
    from orchestrator import web_auth

    monkeypatch.delenv("WEB_SESSION_SECRET", raising=False)
    monkeypatch.setenv("WEB_SESSION_ENC_KEY", "encryption-key-only")
    monkeypatch.delenv("OFFLINE_GRANT_ENC_KEY", raising=False)

    def _boom(*a, **k):
        raise RuntimeError("no hkdf")

    monkeypatch.setattr(hkdf_mod, "HKDF", _boom)
    assert web_auth._secret() == b"encryption-key-only"


# ---------------------------------------------------------------------------
# web_auth.auth_login — pending-auth pruning sweep
# ---------------------------------------------------------------------------
async def test_auth_login_prunes_pending(monkeypatch):
    import time

    from orchestrator import web_auth

    monkeypatch.setattr(web_auth, "_keycloak_config",
                        lambda: ("https://kc.example/realms/astral", "astral-frontend", ""))

    async def _reachable(authority):
        return True

    monkeypatch.setattr(web_auth, "_idp_reachable", _reachable)
    monkeypatch.setenv("USE_MOCK_AUTH", "false")

    saved = dict(web_auth._PENDING)
    try:
        web_auth._PENDING.clear()
        now = time.time()
        # One stale entry (>600s) to exercise the expiry sweep (line 459)...
        web_auth._PENDING["stale"] = {"code_verifier": "x", "created_at": now - 1000}
        # ...plus enough fresh entries to exceed the 4096 cap (lines 461-462).
        for i in range(4098):
            web_auth._PENDING[f"fresh-{i}"] = {"code_verifier": "x", "created_at": now}

        req = SimpleNamespace(
            query_params={"next": "/"},
            base_url="http://test/",
        )
        # query_params needs .get; dict provides it.
        resp = await web_auth.auth_login(req)
        assert resp.status_code in (302, 303, 307)
        assert "stale" not in web_auth._PENDING  # expired entry pruned
        assert len(web_auth._PENDING) <= 4097  # capped (4096 + the new state)
    finally:
        web_auth._PENDING.clear()
        web_auth._PENDING.update(saved)


# ---------------------------------------------------------------------------
# agents.handle_safe_set — verified-owner path + notice messages
# ---------------------------------------------------------------------------
class _SafeSetFakeDB:
    def __init__(self):
        self.safe = False

    def get_agent_ownership(self, agent_id):
        return {"owner_email": "owner@example.com"}

    def get_user(self, user_id):
        return {"email": "owner@example.com"}

    def upsert_agent_safe(self, agent_id, safe, marked_by="unknown"):
        prior = self.safe
        self.safe = bool(safe)
        return prior


class _SafeSetManagementRepository:
    def __init__(self, store):
        self.store = store

    def get_detail_context(self, _transaction, *, owner_id, agent_id, **_limits):
        ownership = self.store.get_agent_ownership(agent_id)
        return SimpleNamespace(
            email=owner_id,
            disabled=False,
            ownership=(
                None
                if ownership is None
                else SimpleNamespace(
                    owner_email=ownership["owner_email"],
                    is_public=bool(ownership.get("is_public")),
                )
            ),
            is_safe=self.store.safe,
            safe_known=True,
            credential_keys=(),
            scope_states=(),
            tool_override_states=(),
            external_identity_links=(),
        )


class _SafeSetPlaneRuntime:
    def __init__(self, repository):
        self.repositories = SimpleNamespace(agent_management=repository)

    @contextmanager
    def transaction(self):
        yield object()


def _safe_set_orchestrator(store):
    from orchestrator.plane_repository_context import ApplicationPlaneSource

    runtime = _SafeSetPlaneRuntime(_SafeSetManagementRepository(store))
    return SimpleNamespace(
        plane_repository_source=ApplicationPlaneSource(
            plane_runtime=runtime,
            plane_repositories=runtime.repositories,
        ),
        user_agent_registry=store,
    )


async def test_handle_safe_set_owner_marks_safe():
    from orchestrator.projection_surfaces import agents

    db = _SafeSetFakeDB()
    orch = _safe_set_orchestrator(db)
    # roles WITHOUT admin/owner — privilege must come from verified ownership.
    region, params, html = await agents.handle_safe_set(
        orch, object(), "owner@example.com", ["user"],
        {"agent_id": "a1", "is_safe": True})
    assert region == "agents"
    assert "marked safe" in html
    assert "auto-enables ALL" in html
    assert db.safe is True


async def test_handle_safe_set_owner_unmarks_safe():
    from orchestrator.projection_surfaces import agents

    db = _SafeSetFakeDB()
    db.safe = True
    orch = _safe_set_orchestrator(db)
    region, params, html = await agents.handle_safe_set(
        orch, object(), "owner@example.com", ["user"],
        {"agent_id": "a1", "is_safe": False})
    assert "not safe" in html
    assert db.safe is False
