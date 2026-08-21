"""Feature 027 — T012: Agents & permissions surface (structural/behavioral).

Runs without Postgres: a minimal fake orchestrator exposes the typed Plane
agent-management projection, registry, tool-permission facade, credential
manager, cards, and draft predicate used by the surface. Assertions are
structural (key markup + handler side-effects), mirroring test_topbar.py /
test_render_golden.py style.
"""
import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

from astralplane.repositories.agent_management import (
    AgentManagementDetailContext,
    AgentManagementListContext,
)
from astralplane.repositories.agents import AgentOwnershipRecord
from astralplane.repositories.identity import ExternalIdentityLinkRecord
from astralplane.repositories.tool_policy import ScopeState, ToolOverrideState
from orchestrator.plane_repository_context import ApplicationPlaneSource
from orchestrator.projection_surfaces import agents as surface


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeSkill:
    def __init__(self, sid, description, scope, name=None, metadata=None):
        self.id = sid
        self.name = name or sid
        self.description = description
        self.scope = scope
        self.metadata = metadata or {}


class FakeCard:
    def __init__(self, agent_id, name, description, skills=None, metadata=None):
        self.agent_id = agent_id
        self.name = name
        self.description = description
        self.skills = skills or []
        self.metadata = metadata or {}


class FakeDB:
    def __init__(self, ownership=None, users=None):
        self.ownership = ownership or {}
        self.users = users or {}
        self.disabled = set()
        self.calls = []
        self.preferences = {}
        self.safe = {}

    def get_all_agent_ownership(self):
        return [{"agent_id": k, **v} for k, v in self.ownership.items()]

    def get_agent_ownership(self, agent_id):
        o = self.ownership.get(agent_id)
        return {"agent_id": agent_id, **o} if o else None

    def set_agent_visibility(self, agent_id, is_public):
        self.calls.append(("set_agent_visibility", agent_id, is_public))
        self.ownership[agent_id]["is_public"] = is_public
        return True

    def get_user_disabled_agents(self, user_id):
        return sorted(self.disabled)

    def is_user_agent_disabled(self, user_id, agent_id):
        return agent_id in self.disabled

    def set_user_agent_disabled(self, user_id, agent_id, disabled):
        self.calls.append(("set_user_agent_disabled", user_id, agent_id, disabled))
        if disabled:
            self.disabled.add(agent_id)
        else:
            self.disabled.discard(agent_id)
        return True

    def get_user(self, user_id):
        return self.users.get(user_id)

    def get_user_preferences(self, user_id):
        return dict(self.preferences.get(user_id) or {})

    def get_agent_is_safe(self, agent_id):
        return bool(self.safe.get(agent_id, False))

    def upsert_agent_safe(self, agent_id, is_safe, *, marked_by):
        prior = self.get_agent_is_safe(agent_id)
        self.safe[agent_id] = bool(is_safe)
        return prior

    def reset_agent_safe(self, agent_id, *, marked_by):
        return self.upsert_agent_safe(agent_id, False, marked_by=marked_by)


class FakePerms:
    def __init__(self, scope_map=None, per_tool=None):
        self.scope_map = scope_map or {}
        self.per_tool = per_tool or {}
        self.scopes = dict.fromkeys(surface.PERMISSION_KINDS, False)
        self.set_calls = []
        self.scope_calls = []
        self.backfilled = []
        self.disabled = set()
        self.disabled_calls = []

    def backfill_per_tool_rows(self, user_id, agent_id):
        self.backfilled.append((user_id, agent_id))
        return 0

    def get_tool_scope_map(self, agent_id):
        return dict(self.scope_map)

    def get_effective_tool_permissions(self, user_id, agent_id, safe_default=None):
        return {t: dict(k) for t, k in self.per_tool.items()}

    def set_tool_permission(self, user_id, agent_id, tool, kind, enabled):
        self.set_calls.append((tool, kind, enabled))
        self.per_tool.setdefault(tool, {})[kind] = enabled

    def get_agent_scopes(self, user_id, agent_id):
        return dict(self.scopes)

    def set_agent_scopes(self, user_id, agent_id, scopes):
        self.scope_calls.append(dict(scopes))
        self.scopes.update(scopes)

    def set_agent_disabled(self, user_id, agent_id, disabled):
        self.disabled_calls.append((user_id, agent_id, disabled))
        if disabled:
            self.disabled.add(agent_id)
        else:
            self.disabled.discard(agent_id)
        return True


class FakeCreds:
    def __init__(self, keys=None):
        self.keys = list(keys or [])
        self.calls = []

    def list_credential_keys(self, user_id, agent_id):
        return list(self.keys)

    def set_bulk_credentials(self, user_id, agent_id, credentials):
        self.calls.append(("set_bulk", dict(credentials)))
        for k in credentials:
            if k not in self.keys:
                self.keys.append(k)

    def delete_credential(self, user_id, agent_id, key):
        self.calls.append(("delete", key))
        if key in self.keys:
            self.keys.remove(key)

    def get_agent_credentials_encrypted(self, user_id, agent_id):
        return {k: "enc" for k in self.keys}


class FakeHistory:
    def __init__(self, db):
        self.db = db


class FakeAgentManagementRepository:
    def __init__(self, db, perms, creds):
        self.db = db
        self.perms = perms
        self.creds = creds

    @staticmethod
    def _ownership(agent_id, value):
        if value is None:
            return None
        return AgentOwnershipRecord(
            agent_id=agent_id,
            owner_email=value["owner_email"],
            is_public=bool(value.get("is_public")),
            created_at=None,
            updated_at=None,
        )

    def get_list_context(self, transaction, *, owner_id, ownership_limit=5000):
        ownership = tuple(
            record
            for agent_id, value in sorted(self.db.ownership.items())
            if (record := self._ownership(agent_id, value)) is not None
        )
        return AgentManagementListContext(
            owner_id=owner_id,
            email=(self.db.users.get(owner_id) or {}).get("email"),
            disabled_agent_ids=tuple(sorted(self.db.disabled)),
            ownership=ownership,
        )

    def get_detail_context(self, transaction, *, owner_id, agent_id, **limits):
        preferences = self.db.preferences.get(owner_id) or {}
        links = []
        for provider, value in sorted(
            (preferences.get("verified_external_identities") or {}).items()
        ):
            links.append(
                ExternalIdentityLinkRecord(
                    owner_id=owner_id,
                    agent_id=value["verified_by_agent"],
                    provider=provider,
                    subject=value["subject"],
                    issuer=value["issuer"],
                    verified_at=int(value.get("verified_at") or 0),
                )
            )
        return AgentManagementDetailContext(
            owner_id=owner_id,
            agent_id=agent_id,
            email=(self.db.users.get(owner_id) or {}).get("email"),
            disabled=agent_id in self.db.disabled,
            ownership=self._ownership(agent_id, self.db.ownership.get(agent_id)),
            is_safe=self.db.get_agent_is_safe(agent_id),
            safe_known=True,
            credential_keys=tuple(sorted(self.creds.keys)),
            scope_states=tuple(
                ScopeState(
                    owner_id=owner_id,
                    agent_id=agent_id,
                    scope=scope,
                    enabled=bool(enabled),
                    updated_at=None,
                )
                for scope, enabled in sorted(self.perms.scopes.items())
            ),
            tool_override_states=tuple(
                ToolOverrideState(
                    owner_id=owner_id,
                    agent_id=agent_id,
                    tool_name=tool_name,
                    permission_kind=permission_kind,
                    enabled=bool(enabled),
                    updated_at=None,
                )
                for tool_name, kind_map in sorted(self.perms.per_tool.items())
                for permission_kind, enabled in sorted(kind_map.items())
            ),
            external_identity_links=tuple(links),
        )


class FakePlaneRuntime:
    def __init__(self, repository):
        self.repositories = SimpleNamespace(agent_management=repository)

    @contextmanager
    def transaction(self):
        yield object()


class FakeOrch:
    def __init__(self, cards, db, perms, creds, draft_ids=()):
        self.agent_cards = cards
        self.history = FakeHistory(db)
        self.tool_permissions = perms
        self.credential_manager = creds
        repository = FakeAgentManagementRepository(db, perms, creds)
        runtime = FakePlaneRuntime(repository)
        self.plane_repository_source = ApplicationPlaneSource(
            plane_runtime=runtime,
            plane_repositories=runtime.repositories,
        )
        self.user_agent_registry = db
        self.security_flags = {}
        self._draft_ids = set(draft_ids)
        self.dispatched = []
        self.probe_response = None

    def _is_draft_agent(self, agent_id):
        return agent_id in self._draft_ids

    async def _dispatch_tool_call(self, agent_id, tool_name, args, timeout, ui_websocket):
        self.dispatched.append((agent_id, tool_name, dict(args)))
        return self.probe_response

    async def execute_authorized_tool(self, **kwargs):
        self.dispatched.append(
            (kwargs["agent_id"], kwargs["tool_name"], dict(kwargs["arguments"]))
        )
        return self.probe_response


def make_orch(**kwargs):
    """Two live agents (alpha owned by alice, beta public) + one hidden draft."""
    cards = {
        "alpha": FakeCard(
            "alpha", "Alpha Agent", "Reads and writes data for analysis pipelines.",
            skills=[
                FakeSkill("get_data", "Fetch records", "tools:read"),
                FakeSkill("write_data", "Modify records", "tools:write"),
            ],
            metadata={"required_credentials": ["api_key"]},
        ),
        "beta": FakeCard("beta", "Beta Agent", "A public helper agent."),
        "ghost": FakeCard("ghost", "Ghost Draft", "Should never appear."),
    }
    db = FakeDB(
        ownership={
            "alpha": {"owner_email": "alice@example.com", "is_public": False},
            "beta": {"owner_email": "bob@example.com", "is_public": True},
        },
        users={"u1": {"email": "alice@example.com"}},
    )
    perms = FakePerms(
        scope_map={"get_data": "tools:read", "write_data": "tools:write"},
        per_tool={
            "get_data": {"tools:read": True},
            "write_data": {"tools:write": False},
        },
    )
    creds = FakeCreds(keys=["api_key"])
    defaults = dict(cards=cards, db=db, perms=perms, creds=creds, draft_ids={"ghost"})
    defaults.update(kwargs)
    return FakeOrch(**defaults)


# ---------------------------------------------------------------------------
# Module contract
# ---------------------------------------------------------------------------

def test_module_contract():
    assert surface.TITLE == "Agents & permissions"
    assert not getattr(surface, "ADMIN_ONLY", False)
    for action in ("chrome_perms_save", "chrome_visibility_set", "chrome_credentials_save",
                   "chrome_credential_delete", "chrome_agent_enabled"):
        assert action in surface.HANDLERS, f"missing handler: {action}"


# ---------------------------------------------------------------------------
# List view
# ---------------------------------------------------------------------------

def test_list_mine_tab_shows_owned_only_and_hides_drafts():
    orch = make_orch()
    html = run(surface.render(orch, "u1", ["user"], {}))
    assert "Alpha Agent" in html
    assert "Beta Agent" not in html  # bob's agent, not mine
    assert "Ghost Draft" not in html  # non-live draft hidden
    assert "Connected" in html  # status/health
    assert "Yours" in html  # owner badge


def test_list_public_tab_shows_public_agents():
    orch = make_orch()
    html = run(surface.render(orch, "u1", ["user"], {"tab": "public"}))
    assert "Beta Agent" in html and "Alpha Agent" not in html
    assert ">Public<" in html  # badge


def test_list_tabs_and_drafts_button():
    orch = make_orch()
    html = run(surface.render(orch, "u1", ["user"], {"tab": "mine"}))
    assert "&quot;tab&quot;: &quot;mine&quot;" in html
    assert "&quot;tab&quot;: &quot;public&quot;" in html
    # Drafts tab opens the drafts surface (not implemented here).
    assert "&quot;surface&quot;: &quot;drafts&quot;" in html
    assert "Drafts" in html


def test_list_row_click_through_and_enable_toggle():
    orch = make_orch()
    html = run(surface.render(orch, "u1", ["user"], {}))
    # Click-through opens detail via chrome_open with agent_id.
    assert 'data-ui-action="chrome_open"' in html
    assert "&quot;agent_id&quot;: &quot;alpha&quot;" in html
    # Enabled agent shows a Disable toggle sending enabled=false.
    assert 'data-ui-action="chrome_agent_enabled"' in html
    assert "&quot;enabled&quot;: false" in html
    assert ">Disable<" in html


def test_list_disabled_agent_shows_enable_and_badge():
    orch = make_orch()
    orch.history.db.disabled.add("alpha")
    html = run(surface.render(orch, "u1", ["user"], {}))
    assert "Disabled by you" in html
    assert "&quot;enabled&quot;: true" in html
    assert ">Enable<" in html


def test_list_escapes_agent_text():
    orch = make_orch()
    orch.agent_cards["alpha"].name = '<script>alert(1)</script>'
    orch.agent_cards["alpha"].description = '<img onerror=x>'
    html = run(surface.render(orch, "u1", ["user"], {}))
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<img" not in html


def test_list_unknown_tab_falls_back_to_mine():
    orch = make_orch()
    html = run(surface.render(orch, "u1", ["user"], {"tab": "evil"}))
    assert 'data-tab="mine"' in html


# ---------------------------------------------------------------------------
# Detail view
# ---------------------------------------------------------------------------

def test_detail_sections_tool_switches_named_tool_kind_with_state():
    orch = make_orch()
    html = run(surface.render(orch, "u1", ["user"], {"agent_id": "alpha"}))
    # Feature 052 (T015/T016): the per-render backfill is gone — it runs once
    # as the _migrate_backfill_tool_kinds_052 boot migration instead.
    assert orch.tool_permissions.backfilled == []
    # Tool switches keep the <tool>::<kind> names; enabled state from internals.
    assert 'name="get_data::tools:read" checked' in html
    assert 'name="write_data::tools:write"' in html
    assert 'name="write_data::tools:write" checked' not in html
    # Only kinds with tools render a section (read + write here).
    assert 'data-perm-section="tools:read"' in html
    assert 'data-perm-section="tools:write"' in html
    assert 'data-perm-section="tools:search"' not in html
    assert 'data-perm-section="tools:system"' not in html
    # Sections live in a data-ui-form and save via collect.
    assert "data-ui-form" in html
    assert 'data-ui-action="chrome_perms_save"' in html
    assert 'data-ui-collect="true"' in html
    # Tool descriptions shown.
    assert "Fetch records" in html


def test_detail_section_masters_reflect_state_and_gate_tools():
    orch = make_orch()
    html = run(surface.render(orch, "u1", ["user"], {"agent_id": "alpha"}))
    # Read has an enabled tool -> master on; its tool switch is interactive.
    assert 'name="__scope::tools:read" checked' in html
    assert 'name="get_data::tools:read" checked disabled' not in html
    # Write has no enabled tool and scope off -> master off; tool disabled + dimmed.
    assert 'name="__scope::tools:write" checked' not in html
    assert 'name="write_data::tools:write" disabled' in html
    assert "opacity-50" in html


def test_detail_section_master_on_from_scope_even_if_all_tools_off():
    orch = make_orch()
    orch.tool_permissions.per_tool["get_data"]["tools:read"] = False
    orch.tool_permissions.scopes["tools:read"] = True
    html = run(surface.render(orch, "u1", ["user"], {"agent_id": "alpha"}))
    assert 'name="__scope::tools:read" checked' in html
    # Tools stay individually off but remain interactive under an on master.
    assert 'name="get_data::tools:read" checked' not in html
    assert 'name="get_data::tools:read" disabled' not in html


def test_detail_unknown_scope_tools_listed_but_not_configurable():
    """Tools with a non-standard scope stay visible (the old matrix listed
    every tool) in an inert Other section instead of vanishing."""
    orch = make_orch()
    orch.tool_permissions.scope_map["weird_tool"] = "tools:custom"
    html = run(surface.render(orch, "u1", ["user"], {"agent_id": "alpha"}))
    assert "weird_tool" in html and "Not configurable" in html
    assert 'name="weird_tool::tools:custom"' not in html  # no switch rendered
    # An agent exposing ONLY unknown-scope tools must not claim it has none.
    orch.tool_permissions.scope_map = {"only_weird": "tools:custom"}
    orch.tool_permissions.per_tool = {}
    html2 = run(surface.render(orch, "u1", ["user"], {"agent_id": "alpha"}))
    assert "exposes no tools" not in html2
    assert "only_weird" in html2


def test_detail_visibility_toggle_owner_only():
    orch = make_orch()
    html = run(surface.render(orch, "u1", ["user"], {"agent_id": "alpha"}))
    assert 'data-ui-action="chrome_visibility_set"' in html
    assert "&quot;is_public&quot;: true" in html  # alpha is private -> offer public
    # Non-owner (beta belongs to bob) gets no visibility section.
    html_beta = run(surface.render(orch, "u1", ["user"], {"agent_id": "beta"}))
    assert 'data-ui-action="chrome_visibility_set"' not in html_beta


def test_detail_credentials_section():
    orch = make_orch()
    html = run(surface.render(orch, "u1", ["user"], {"agent_id": "alpha"}))
    assert "api_key" in html and ">Stored<" in html
    assert 'type="password"' in html and 'name="api_key"' in html
    assert 'data-ui-action="chrome_credentials_save"' in html
    assert 'data-ui-action="chrome_credential_delete"' in html
    assert "&quot;key&quot;: &quot;api_key&quot;" in html


def test_detail_renders_direct_orcid_connect_button():
    orch = make_orch()
    orch.agent_cards["alpha"].metadata["external_identity"] = {
        "provider": "orcid",
        "label": "ORCID iD",
        "authorization_url": "https://panatlas.net/link",
    }
    html = run(surface.render(
        orch, "alice-id", ["user"], {"agent_id": "alpha", "tab": "mine"}
    ))
    assert "Connect ORCID" in html
    assert "/api/agents/alpha/external-identities/orcid/start" in html


def test_detail_shows_linked_orcid_status_instead_of_connect_button():
    orch = make_orch()
    orch.agent_cards["alpha"].metadata["external_identity"] = {
        "provider": "orcid",
        "label": "ORCID iD",
        "authorization_url": "https://panatlas.net/link",
    }
    orch.history.db.preferences["alice-id"] = {
        "verified_external_identities": {
            "orcid": {
                "subject": "0009-0003-6606-0831",
                "issuer": "https://orcid.org",
                "verified_by_agent": "alpha",
            }
        }
    }
    html = run(surface.render(
        orch, "alice-id", ["user"], {"agent_id": "alpha", "tab": "mine"}
    ))
    assert "Connected: 0009-0003-6606-0831" in html
    assert "Connect ORCID" not in html


def test_detail_back_link_and_enable_toggle():
    orch = make_orch()
    html = run(surface.render(orch, "u1", ["user"], {"agent_id": "alpha", "tab": "public"}))
    assert "Back to agents" in html
    assert "&quot;tab&quot;: &quot;public&quot;" in html  # back preserves tab
    assert 'data-ui-action="chrome_agent_enabled"' in html
    assert "&quot;detail&quot;: true" in html


def test_detail_unknown_agent_renders_error_not_raise():
    orch = make_orch()
    html = run(surface.render(orch, "u1", ["user"], {"agent_id": "nope"}))
    assert "not found" in html
    assert "astral-chrome-notice" in html
    assert "Back to agents" in html


# ---------------------------------------------------------------------------
# chrome_perms_save
# ---------------------------------------------------------------------------

def test_perms_save_translates_fields_and_mirrors_scopes():
    orch = make_orch()
    result = run(surface.HANDLERS["chrome_perms_save"](
        orch, None, "u1", ["user"],
        {"agent_id": "alpha",
         "fields": {"get_data::tools:read": False, "write_data::tools:write": True}},
    ))
    key, params, notice = result
    assert key == "agents" and params["agent_id"] == "alpha"
    assert "success" in notice or "green" in notice
    assert ("get_data", "tools:read", False) in orch.tool_permissions.set_calls
    assert ("write_data", "tools:write", True) in orch.tool_permissions.set_calls
    # Scope mirror derived from effective per-tool state (api.py parity).
    assert orch.tool_permissions.scope_calls, "set_agent_scopes not called"
    assert orch.tool_permissions.scope_calls[-1]["tools:write"] is True


def test_perms_save_rejects_unknown_tool_without_writes():
    orch = make_orch()
    key, params, notice = run(surface.HANDLERS["chrome_perms_save"](
        orch, None, "u1", ["user"],
        {"agent_id": "alpha", "fields": {"bogus::tools:read": True}},
    ))
    assert key == "agents"
    assert "not registered" in notice
    assert orch.tool_permissions.set_calls == []


def test_perms_save_rejects_wrong_kind_whole_payload():
    orch = make_orch()
    key, params, notice = run(surface.HANDLERS["chrome_perms_save"](
        orch, None, "u1", ["user"],
        {"agent_id": "alpha",
         "fields": {"get_data::tools:read": True, "get_data::tools:write": True}},
    ))
    assert "does not apply" in notice
    assert orch.tool_permissions.set_calls == []  # no half-applied state


def test_perms_save_unknown_agent_and_empty_fields():
    orch = make_orch()
    _, _, notice = run(surface.HANDLERS["chrome_perms_save"](
        orch, None, "u1", ["user"], {"agent_id": "nope", "fields": {}}))
    assert "not found" in notice
    _, _, notice2 = run(surface.HANDLERS["chrome_perms_save"](
        orch, None, "u1", ["user"], {"agent_id": "alpha", "fields": {}}))
    assert "No permission changes" in notice2


def test_perms_save_master_off_forces_section_off():
    """Section gate wins: a collected tool switch left on cannot survive an
    off master, and the agent-wide scope is written off (not legacy-mirrored
    back on)."""
    orch = make_orch()
    _, _, notice = run(surface.HANDLERS["chrome_perms_save"](
        orch, None, "u1", ["user"],
        {"agent_id": "alpha",
         "fields": {"__scope::tools:read": False, "get_data::tools:read": True}},
    ))
    assert "success" in notice or "green" in notice
    assert ("get_data", "tools:read", False) in orch.tool_permissions.set_calls
    assert ("get_data", "tools:read", True) not in orch.tool_permissions.set_calls
    assert orch.tool_permissions.scope_calls[-1]["tools:read"] is False


def test_perms_save_master_off_blankets_unsubmitted_tools():
    orch = make_orch()
    run(surface.HANDLERS["chrome_perms_save"](
        orch, None, "u1", ["user"],
        {"agent_id": "alpha", "fields": {"__scope::tools:read": False}},
    ))
    assert ("get_data", "tools:read", False) in orch.tool_permissions.set_calls


def test_perms_save_master_on_preserves_individual_tool_offs():
    orch = make_orch()
    _, _, notice = run(surface.HANDLERS["chrome_perms_save"](
        orch, None, "u1", ["user"],
        {"agent_id": "alpha",
         "fields": {"__scope::tools:write": True, "write_data::tools:write": False}},
    ))
    assert "success" in notice or "green" in notice
    assert ("write_data", "tools:write", False) in orch.tool_permissions.set_calls
    # Master writes the scope on even though every tool under it is off.
    assert orch.tool_permissions.scope_calls[-1]["tools:write"] is True


def test_perms_save_rejects_unknown_master_kind_without_writes():
    orch = make_orch()
    _, _, notice = run(surface.HANDLERS["chrome_perms_save"](
        orch, None, "u1", ["user"],
        {"agent_id": "alpha",
         "fields": {"__scope::tools:evil": True, "get_data::tools:read": True}},
    ))
    assert "Unknown permission kind" in notice
    assert orch.tool_permissions.set_calls == []
    assert orch.tool_permissions.scope_calls == []


# ---------------------------------------------------------------------------
# chrome_visibility_set
# ---------------------------------------------------------------------------

def test_visibility_set_owner_succeeds():
    orch = make_orch()
    key, params, notice = run(surface.HANDLERS["chrome_visibility_set"](
        orch, None, "u1", ["user"], {"agent_id": "alpha", "is_public": True}))
    assert key == "agents" and params["agent_id"] == "alpha"
    assert ("set_agent_visibility", "alpha", True) in orch.history.db.calls
    assert "public" in notice


def test_visibility_set_non_owner_rejected_without_write():
    orch = make_orch()
    _, _, notice = run(surface.HANDLERS["chrome_visibility_set"](
        orch, None, "u1", ["user"], {"agent_id": "beta", "is_public": False}))
    assert "owner" in notice
    assert all(c[0] != "set_agent_visibility" for c in orch.history.db.calls)


def test_visibility_set_no_ownership_record():
    orch = make_orch()
    orch.agent_cards["lone"] = FakeCard("lone", "Lone", "No ownership row.")
    _, _, notice = run(surface.HANDLERS["chrome_visibility_set"](
        orch, None, "u1", ["user"], {"agent_id": "lone", "is_public": True}))
    assert "No ownership record" in notice


# ---------------------------------------------------------------------------
# chrome_credentials_save / chrome_credential_delete
# ---------------------------------------------------------------------------

def test_credentials_save_filters_blank_values():
    orch = make_orch()
    key, params, notice = run(surface.HANDLERS["chrome_credentials_save"](
        orch, None, "u1", ["user"],
        {"agent_id": "alpha", "fields": {"api_key": "s3cret", "other": "  "}}))
    assert key == "agents" and params["agent_id"] == "alpha"
    assert ("set_bulk", {"api_key": "s3cret"}) in orch.credential_manager.calls
    assert "Saved 1 credential" in notice


def test_credentials_save_empty_is_error_without_write():
    orch = make_orch()
    _, _, notice = run(surface.HANDLERS["chrome_credentials_save"](
        orch, None, "u1", ["user"], {"agent_id": "alpha", "fields": {"api_key": ""}}))
    assert "No credential values" in notice
    assert orch.credential_manager.calls == []


def test_credentials_save_runs_probe_when_agent_exposes_check():
    orch = make_orch()
    orch.agent_cards["alpha"].skills.append(
        FakeSkill("_credentials_check", "probe", "tools:read"))

    class Resp:
        error = None
        result = {"credential_test": "success", "detail": None}

    orch.probe_response = Resp()
    _, _, notice = run(surface.HANDLERS["chrome_credentials_save"](
        orch, None, "u1", ["user"], {"agent_id": "alpha", "fields": {"api_key": "x"}}))
    assert orch.dispatched and orch.dispatched[0][1] == "_credentials_check"
    assert "Connection test: success" in notice


def test_credentials_save_probe_failure_does_not_block_save():
    orch = make_orch()
    orch.agent_cards["alpha"].skills.append(
        FakeSkill("_credentials_check", "probe", "tools:read"))
    orch.probe_response = None  # no response -> unreachable
    _, _, notice = run(surface.HANDLERS["chrome_credentials_save"](
        orch, None, "u1", ["user"], {"agent_id": "alpha", "fields": {"api_key": "x"}}))
    assert ("set_bulk", {"api_key": "x"}) in orch.credential_manager.calls
    assert "unreachable" in notice


def test_credential_delete():
    orch = make_orch()
    key, params, notice = run(surface.HANDLERS["chrome_credential_delete"](
        orch, None, "u1", ["user"], {"agent_id": "alpha", "key": "api_key"}))
    assert ("delete", "api_key") in orch.credential_manager.calls
    assert "deleted" in notice
    _, _, notice2 = run(surface.HANDLERS["chrome_credential_delete"](
        orch, None, "u1", ["user"], {"agent_id": "alpha"}))
    assert "No credential key" in notice2


# ---------------------------------------------------------------------------
# chrome_agent_enabled
# ---------------------------------------------------------------------------

def test_agent_enabled_toggle_writes_inverse_disabled_flag():
    orch = make_orch()
    key, params, notice = run(surface.HANDLERS["chrome_agent_enabled"](
        orch, None, "u1", ["user"], {"agent_id": "alpha", "enabled": False, "tab": "mine"}))
    assert ("u1", "alpha", True) in orch.tool_permissions.disabled_calls
    assert key == "agents" and params == {"tab": "mine"}
    assert "disabled" in notice

    key, params, _ = run(surface.HANDLERS["chrome_agent_enabled"](
        orch, None, "u1", ["user"],
        {"agent_id": "alpha", "enabled": True, "detail": True, "tab": "mine"}))
    assert ("u1", "alpha", False) in orch.tool_permissions.disabled_calls
    assert params == {"agent_id": "alpha", "tab": "mine"}  # detail re-render


def test_agent_enabled_unknown_agent():
    orch = make_orch()
    _, _, notice = run(surface.HANDLERS["chrome_agent_enabled"](
        orch, None, "u1", ["user"], {"agent_id": "nope", "enabled": True}))
    assert "not found" in notice
    assert orch.history.db.calls == []


# ---------------------------------------------------------------------------
# Regression: dict-shaped required_credentials (real generated-agent metadata)
# ---------------------------------------------------------------------------

def test_detail_renders_with_dict_shaped_required_credentials():
    """Generated agents declare REQUIRED_CREDENTIALS as dicts with key/label/
    description — the surface crashed on dict.fromkeys(unhashable). The
    detail view must render, list the declared keys, and surface labels."""
    orch = make_orch()
    orch.agent_cards["alpha"].metadata = {"required_credentials": [
        {"key": "MS_GRAPH_CLIENT_ID", "label": "Microsoft Graph Client ID",
         "description": "OAuth 2.0 Client ID", "required": True, "type": "oauth_client_id"},
        {"key": "MS_GRAPH_SECRET"},
        "PLAIN_STRING_KEY",
        {"label": "no key — skipped"},
        42,
    ]}
    html = run(surface.render(orch, "u1", [], {"agent_id": "alpha"}))
    assert "astral-chrome-error" not in html
    assert "MS_GRAPH_CLIENT_ID" in html and "MS_GRAPH_SECRET" in html
    assert "PLAIN_STRING_KEY" in html
    assert 'title="Microsoft Graph Client ID"' in html


def test_normalize_credential_entries_shapes():
    keys, labels, optional = surface._normalize_credential_entries(
        [{"key": "A", "label": "Label A"}, "B", {"name": "C"},
         {"key": "D", "required": False}, {"x": 1}, None, 7])
    assert keys == ["A", "B", "C", "D"]
    assert labels == {"A": "Label A"}
    assert optional == {"D"}  # only explicit required:False is optional
    assert surface._normalize_credential_entries(None) == ([], {}, set())


def test_credentials_optional_declaration_shows_optional_not_required():
    """A credential declared required:False (e.g. web_research SEARCH_API_*, which
    has a keyless fallback) must render an Optional badge, not Required."""
    cards = {"alpha": FakeCard(
        "alpha", "Alpha Agent", "Search with an optional provider.",
        skills=[FakeSkill("get_data", "Fetch records", "tools:read")],
        metadata={"required_credentials": [
            {"key": "SEARCH_API_URL", "required": False},
            {"key": "MANDATORY_KEY", "required": True},
        ]},
    )}
    db = FakeDB(
        ownership={"alpha": {"owner_email": "alice@example.com", "is_public": False}},
        users={"u1": {"email": "alice@example.com"}},
    )
    perms = FakePerms(scope_map={"get_data": "tools:read"},
                      per_tool={"get_data": {"tools:read": True}})
    orch = FakeOrch(cards=cards, db=db, perms=perms, creds=FakeCreds(keys=[]))
    html = run(surface.render(orch, "u1", ["user"], {"agent_id": "alpha"}))
    assert "SEARCH_API_URL" in html and "MANDATORY_KEY" in html
    assert ">Optional<" in html  # the optional cred is not mislabeled Required
    assert ">Required<" in html  # the genuinely-required cred still shows Required
# ---------------------------------------------------------------------------
# 052 — pure helpers: email fallback + preferences-blob disabled set
# ---------------------------------------------------------------------------

def test_email_fallback_uses_email_shaped_user_id():
    assert surface._email_fallback("", "dev@example.com") == "dev@example.com"
    assert surface._email_fallback("real@example.com", "dev@example.com") == "real@example.com"
    assert surface._email_fallback(None, "not-an-email") == ""


def test_disabled_from_preferences_tolerates_malformed_blobs():
    assert surface._disabled_from_preferences('{"disabled_agents": ["a", "b"]}') == {"a", "b"}
    assert surface._disabled_from_preferences("not-json{") == set()
    assert surface._disabled_from_preferences('{"disabled_agents": "not-a-list"}') == set()
    assert surface._disabled_from_preferences(None) == set()


# ---------------------------------------------------------------------------
# Feature 063 T074 (FR-025) — pre-grant destructive markers on the verb list
# ---------------------------------------------------------------------------

_ROW_MARKER = '<div class="flex items-center justify-between gap-3 py-2">'


def _tool_row(html, tool_name):
    """The one rendered tool row containing ``>tool_name<`` (badge scoping)."""
    chunks = [c for c in html.split(_ROW_MARKER) if f">{tool_name}<" in c]
    assert len(chunks) == 1, f"expected exactly one row for {tool_name}"
    return chunks[0]


def test_destructive_badge_classifications():
    assert surface._destructive_badge(None) == ""
    assert surface._destructive_badge("never") == ""
    assert ">Destructive<" in surface._destructive_badge("always")
    assert ">Sometimes destructive<" in surface._destructive_badge("if_exists")
    assert ">Sometimes destructive<" in surface._destructive_badge(
        {"by_action": ["remove"]})


def test_detail_marks_destructive_tools_from_skill_metadata():
    orch = make_orch()
    orch.agent_cards["alpha"].skills = [
        FakeSkill("get_data", "Fetch records", "tools:read"),
        FakeSkill("write_data", "Modify records", "tools:write",
                  metadata={"destructive": "always"}),
        FakeSkill("upload_thing", "Upload a file", "tools:write",
                  metadata={"destructive": "if_exists"}),
        FakeSkill("mkdir_thing", "Make a directory", "tools:write",
                  metadata={"destructive": "never"}),
    ]
    orch.tool_permissions.scope_map = {
        "get_data": "tools:read", "write_data": "tools:write",
        "upload_thing": "tools:write", "mkdir_thing": "tools:write",
    }
    html = run(surface.render(orch, "u1", ["user"], {"agent_id": "alpha"}))
    assert ">Destructive<" in _tool_row(html, "write_data")
    assert ">Sometimes destructive<" in _tool_row(html, "upload_thing")
    # never / no declaration → no marker of either strength.
    for clean in ("get_data", "mkdir_thing"):
        row = _tool_row(html, clean)
        assert ">Destructive<" not in row and "Sometimes destructive" not in row


def _remote_compute_card():
    """The REAL remote-compute-1 card, built by the REAL base-agent card
    builder from the REAL unified TOOL_REGISTRY (no keys/sockets needed)."""
    from agents.remote_compute.mcp_tools import TOOL_REGISTRY
    from shared.base_agent import BaseA2AAgent

    class _Stub(BaseA2AAgent):
        def __init__(self):
            self.mcp_server = type("S", (), {"tools": TOOL_REGISTRY})()
            self.service_name = "Remote Compute"
            self.agent_id = "remote-compute-1"
            self.description = "Work with your registered machines."
            self.skill_tags = []
            self.card_metadata = {}
            self._public_key_jwk = {"kty": "EC"}

    return _Stub()._build_agent_card()


def test_remote_compute_card_skills_carry_destructive_metadata():
    """base_agent propagates each registry entry's destructive classification
    onto the card skill metadata — the SAME object the confirmation gate reads
    (FR-028 no-drift), and only where the registry declares one."""
    from agents.remote_compute.mcp_tools import TOOL_REGISTRY
    from orchestrator.remote_confirmation import DESTRUCTIVE_CLASSIFICATION

    by_id = {s.id: s for s in _remote_compute_card().skills}
    assert set(by_id) == set(TOOL_REGISTRY) and len(by_id) == 18
    for verb, classification in DESTRUCTIVE_CLASSIFICATION.items():
        assert by_id[verb].metadata.get("destructive") is classification, verb
    for verb, entry in TOOL_REGISTRY.items():
        if "destructive" not in entry:
            assert "destructive" not in by_id[verb].metadata, verb


def test_remote_compute_full_verb_list_visible_before_any_grant():
    """FR-025: the agents surface shows remote-compute-1's COMPLETE verb list
    — all 18 verbs, each with a non-empty one-line description and a
    destructive marker on the destructive ones — with ZERO agent_scopes rows
    for the viewing user (nothing granted, nothing enabled)."""
    from agents.remote_compute.mcp_tools import TOOL_REGISTRY
    from orchestrator.remote_confirmation import DESTRUCTIVE_CLASSIFICATION
    from webrender.chrome import esc

    assert len(TOOL_REGISTRY) == 18
    card = _remote_compute_card()
    db = FakeDB(
        ownership={"remote-compute-1": {"owner_email": "system@astral",
                                        "is_public": True}},
        users={"u1": {"email": "viewer@example.com"}},
    )
    # Zero agent_scopes rows: no per-tool grants, every scope False.
    perms = FakePerms(
        scope_map={name: entry["scope"] for name, entry in TOOL_REGISTRY.items()},
        per_tool={},
    )
    orch = FakeOrch(cards={"remote-compute-1": card}, db=db, perms=perms,
                    creds=FakeCreds(keys=[]))
    html = run(surface.render(
        orch, "u1", ["user"], {"agent_id": "remote-compute-1", "tab": "public"}))

    assert " checked" not in html  # truly pre-grant: nothing presents as on
    for name, entry in TOOL_REGISTRY.items():
        row = _tool_row(html, name)
        desc = entry["description"]
        assert desc and desc.strip(), f"{name} has no description"
        assert esc(surface._snippet(desc, 90)) in row, f"{name} description missing"
        classification = DESTRUCTIVE_CLASSIFICATION.get(name)
        if classification == "always":
            assert ">Destructive<" in row, name
        elif classification and classification != "never":
            assert ">Sometimes destructive<" in row, name
        else:
            assert ">Destructive<" not in row, name
            assert "Sometimes destructive" not in row, name
