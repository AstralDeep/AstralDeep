"""Feature 030 — wiring/onboarding fixes (walkthrough findings).

Covers:
* welcome canvas consent card (``welcome_components(tools_available=...)``)
* ToolPermissionManager.has_any_enabled_scope / scopes_required_by_tools
* Orchestrator._enable_recommended_agent_scopes (consent bulk enable)
* Orchestrator._text_only_cta_components (deterministic enable affordance)
* Orchestrator._is_draft_agent public-ownership short-circuit (etf false positive)
* Orchestrator._delegation_required (Constitution VII fail-closed posture)
* Plane-backed first-party visibility and owner-scoped permission state

Run inside the astraldeep container:
    python -m pytest tests/test_wiring_030.py -q
"""
import sys
import types
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.orchestrator import Orchestrator  # noqa: E402
from orchestrator.tool_permissions import ToolPermissionManager  # noqa: E402
from orchestrator.welcome import enable_agents_card, welcome_components  # noqa: E402
from tests.helpers.voice_plane_runtime import isolated_plane_runtime  # noqa: E402


@pytest.fixture(scope="module")
def plane_runtime():
    with isolated_plane_runtime("wiring_030") as runtime:
        yield runtime


# ---------------------------------------------------------------------------
# Welcome canvas consent card
# ---------------------------------------------------------------------------


def _component_types(components):
    return [c.get("type") for c in components]


def test_welcome_default_has_no_enable_card():
    components = welcome_components()
    assert all("Agents are off" not in str(c.get("title", "")) for c in components)
    assert _component_types(components)[0] == "hero"


def test_welcome_tools_available_true_has_no_enable_card():
    components = welcome_components(tools_available=True)
    assert all("Agents are off" not in str(c.get("title", "")) for c in components)


def test_welcome_without_tools_prepends_enable_card_after_hero():
    components = welcome_components(tools_available=False)
    assert components[0]["type"] == "hero"
    card = components[1]
    assert card["type"] == "card"
    assert "Agents are off" in card["title"]
    actions = [child.get("action") for child in card["content"]
               if child.get("type") == "button"]
    assert actions == ["enable_recommended_agents", "chrome_open"]
    # The chrome_open button must target the agents surface.
    chrome_btn = [c for c in card["content"]
                  if c.get("action") == "chrome_open"][0]
    assert chrome_btn["payload"] == {"surface": "agents"}


def test_enable_agents_card_never_promises_write_access():
    card = enable_agents_card()
    text = str(card)
    assert "never write access" in text


# ---------------------------------------------------------------------------
# ToolPermissionManager helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def perms(plane_runtime):
    from orchestrator.user_agents import UserAgentRegistry

    registry = UserAgentRegistry(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
    )
    manager = ToolPermissionManager(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
        user_agent_registry=registry,
    )
    user_id = f"pytest-030-{uuid.uuid4().hex[:12]}"
    yield manager, user_id
    with plane_runtime.transaction() as transaction:
        plane_runtime.repositories.tool_policy_state.remove_owner_state(
            transaction,
            owner_id=user_id,
        )


def test_has_any_enabled_scope_false_for_fresh_user(perms):
    manager, user_id = perms
    assert manager.has_any_enabled_scope(user_id) is False


# ── 030 ∩ 040: who the enable affordance is still FOR ─────────────────────


def test_fresh_user_on_safe_baseline_is_not_shown_the_enable_affordance(perms):
    """The 030 affordance is deliberately unreachable for a fresh account.

    Feature 040 seeds the bundled built-ins safe + public, and the safe
    baseline flips deny→allow for a user with no explicit scope row. So a fresh
    user's tools ARE available and the "Agents are off for this account" card
    must not render — showing it would be a false statement, and the register
    entry calling this "structurally unreachable" describes intended behavior,
    not a defect. This pins the premise so the affordance is not "restored".
    """
    manager, user_id = perms
    agent_id = f"pytest-030-safe-{uuid.uuid4().hex[:10]}"
    manager.user_agent_registry.upsert_agent_safe(agent_id, True, marked_by="pytest")
    manager.user_agent_registry.set_agent_ownership(
        agent_id,
        "o@e.com",
        is_public=True,
    )
    manager.register_tool_scopes(agent_id, {"web_search": "tools:search"})

    # Dispatchable → compute_tools_available_for_user would return True …
    assert manager.is_tool_allowed(user_id, agent_id, "web_search") is True
    # … even though the user has granted nothing explicitly.
    assert manager.has_any_enabled_scope(user_id) is False
    assert all("Agents are off" not in str(c.get("title", ""))
               for c in welcome_components(tools_available=True))


def test_explicit_optout_user_still_reaches_the_enable_affordance(perms):
    """…and it is still reachable, and honest, for the opt-out population.

    An explicit ``enabled=False`` row outranks the safe flip, so a user who
    deliberately turned everything off has no dispatchable tools — the card's
    copy is then true, and its Enable button writes the rows that undo it.
    """
    manager, user_id = perms
    agent_id = f"pytest-030-optout-{uuid.uuid4().hex[:10]}"
    manager.user_agent_registry.upsert_agent_safe(agent_id, True, marked_by="pytest")
    manager.user_agent_registry.set_agent_ownership(
        agent_id,
        "o@e.com",
        is_public=True,
    )
    manager.register_tool_scopes(agent_id, {"web_search": "tools:search"})
    manager.set_agent_scopes(user_id, agent_id, {"tools:search": False})

    assert manager.is_tool_allowed(user_id, agent_id, "web_search") is False
    card = welcome_components(tools_available=False)[1]
    assert "Agents are off" in card["title"]


def test_has_any_enabled_scope_false_when_all_rows_disabled(perms):
    manager, user_id = perms
    manager.set_agent_scopes(user_id, "agent-x", {"tools:read": False})
    assert manager.has_any_enabled_scope(user_id) is False


def test_has_any_enabled_scope_true_after_grant(perms):
    manager, user_id = perms
    manager.set_agent_scopes(user_id, "agent-x", {"tools:read": True})
    assert manager.has_any_enabled_scope(user_id) is True


def test_scopes_required_by_tools_excludes_write(perms):
    manager, _ = perms
    manager.register_tool_scopes("agent-w", {
        "fetch": "tools:read", "search": "tools:search",
        "mutate": "tools:write", "cpu": "tools:system",
    })
    assert manager.scopes_required_by_tools("agent-w") == [
        "tools:read", "tools:search", "tools:system"]


def test_scopes_required_by_tools_defaults_to_read_for_unmapped_agent(perms):
    manager, _ = perms
    assert manager.scopes_required_by_tools("never-registered") == ["tools:read"]


# ---------------------------------------------------------------------------
# Orchestrator._enable_recommended_agent_scopes (fake-self pattern)
# ---------------------------------------------------------------------------


def _enable_fake(manager, agent_ids, ownership_rows, drafts=()):
    fake = types.SimpleNamespace(
        agent_cards={aid: object() for aid in agent_ids},
        user_agent_registry=types.SimpleNamespace(
            get_all_agent_ownership=lambda: ownership_rows
        ),
        tool_permissions=manager,
        _is_draft_agent=lambda aid: aid in drafts,
    )
    fake._enable_recommended_agent_scopes = types.MethodType(
        Orchestrator._enable_recommended_agent_scopes, fake)
    return fake


def test_consent_enable_grants_nonwrite_scopes_for_public_agents(perms):
    manager, user_id = perms
    manager.register_tool_scopes("pub-1", {"get": "tools:read", "put": "tools:write"})
    fake = _enable_fake(manager, ["pub-1", "priv-1"], [
        {"agent_id": "pub-1", "is_public": True},
        {"agent_id": "priv-1", "is_public": False},
    ])
    enabled = fake._enable_recommended_agent_scopes(user_id)
    assert enabled == ["pub-1"]
    scopes = manager.get_agent_scopes(user_id, "pub-1")
    assert scopes["tools:read"] is True
    assert scopes["tools:write"] is False  # never granted by consent enable
    assert manager.get_agent_scopes(user_id, "priv-1")["tools:read"] is False


def test_consent_enable_skips_drafts_and_honors_requested_subset(perms):
    manager, user_id = perms
    fake = _enable_fake(manager, ["pub-1", "pub-2", "draft-1"], [
        {"agent_id": "pub-1", "is_public": True},
        {"agent_id": "pub-2", "is_public": True},
        {"agent_id": "draft-1", "is_public": True},
    ], drafts={"draft-1"})
    enabled = fake._enable_recommended_agent_scopes(user_id, ["pub-2", "draft-1", "nope"])
    assert enabled == ["pub-2"]
    assert manager.get_agent_scopes(user_id, "pub-1")["tools:read"] is False
    assert manager.get_agent_scopes(user_id, "draft-1")["tools:read"] is False


# ---------------------------------------------------------------------------
# Orchestrator._text_only_cta_components
# ---------------------------------------------------------------------------


def _cta_fake(has_any: bool):
    fake = types.SimpleNamespace(tool_permissions=types.SimpleNamespace(
        has_any_enabled_scope=lambda user_id: has_any))
    fake._text_only_cta_components = types.MethodType(
        Orchestrator._text_only_cta_components, fake)
    return fake


def test_text_only_cta_empty_when_user_has_enabled_scopes():
    assert _cta_fake(True)._text_only_cta_components("u1") == []


def test_text_only_cta_components_for_never_configured_user():
    components = _cta_fake(False)._text_only_cta_components("u1")
    assert _component_types(components) == ["alert", "button", "button"]
    assert components[1]["action"] == "enable_recommended_agents"
    assert components[2]["action"] == "chrome_open"
    assert components[2]["payload"] == {"surface": "agents"}


def test_text_only_cta_fails_safe_on_permission_error():
    fake = types.SimpleNamespace(tool_permissions=types.SimpleNamespace(
        has_any_enabled_scope=lambda user_id: (_ for _ in ()).throw(RuntimeError)))
    fake._text_only_cta_components = types.MethodType(
        Orchestrator._text_only_cta_components, fake)
    assert fake._text_only_cta_components("u1") == []


# ---------------------------------------------------------------------------
# Orchestrator._is_draft_agent — public ownership short-circuit
# ---------------------------------------------------------------------------


def _draft_fake(draft, ownership):
    fake = types.SimpleNamespace(
        lifecycle_manager=types.SimpleNamespace(
            _find_draft_by_agent_id=lambda aid: draft),
        user_agent_registry=types.SimpleNamespace(
            get_agent_ownership=lambda aid: ownership
        ),
    )
    fake._is_draft_agent = types.MethodType(Orchestrator._is_draft_agent, fake)
    return fake


def test_public_agent_never_hidden_by_stale_draft_row():
    fake = _draft_fake({"status": "error"}, {"is_public": True})
    # weather-1 is a surviving bundled public agent (040 retired etf-tracker-1-1).
    assert fake._is_draft_agent("weather-1") is False


def test_private_agent_with_non_live_draft_row_stays_hidden():
    fake = _draft_fake({"status": "testing"}, {"is_public": False})
    assert fake._is_draft_agent("draft-being-tested-1") is True


def test_live_draft_row_is_not_hidden():
    fake = _draft_fake({"status": "live"}, {"is_public": False})
    assert fake._is_draft_agent("promoted-agent-1") is False


# ---------------------------------------------------------------------------
# Orchestrator._delegation_required — Constitution VII posture
# ---------------------------------------------------------------------------


def _delegation_required(monkeypatch, astral_env, override):
    if astral_env is None:
        monkeypatch.delenv("ASTRAL_ENV", raising=False)
    else:
        monkeypatch.setenv("ASTRAL_ENV", astral_env)
    if override is None:
        monkeypatch.delenv("DELEGATION_REQUIRED", raising=False)
    else:
        monkeypatch.setenv("DELEGATION_REQUIRED", override)
    fake = types.SimpleNamespace()
    return types.MethodType(Orchestrator._delegation_required, fake)()


def test_delegation_optional_in_development(monkeypatch):
    assert _delegation_required(monkeypatch, "development", None) is False


def test_delegation_required_in_production_posture(monkeypatch):
    assert _delegation_required(monkeypatch, None, None) is True
    assert _delegation_required(monkeypatch, "production", None) is True


def test_delegation_override_wins_both_ways(monkeypatch):
    assert _delegation_required(monkeypatch, "development", "true") is True
    assert _delegation_required(monkeypatch, "production", "false") is False


# ---------------------------------------------------------------------------
# Current Plane-backed catalog visibility policy
# ---------------------------------------------------------------------------


def test_visibility_policy_flips_only_listed_agents_and_is_idempotent(plane_runtime):
    from orchestrator.local_agents import FIRST_PARTY_PUBLIC_AGENT_IDS
    from orchestrator.user_agents import UserAgentRegistry

    registry = UserAgentRegistry(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
    )
    listed = "web-research-1"
    unlisted = f"pytest-030-unlisted-{uuid.uuid4().hex[:8]}"
    owner_email = f"pytest-030-{uuid.uuid4().hex[:8]}@example.invalid"
    registry.set_agent_ownership(listed, owner_email, is_public=False)
    registry.set_agent_ownership(unlisted, owner_email, is_public=False)
    try:
        for _ in range(2):
            for agent_id in (listed, unlisted):
                if agent_id in FIRST_PARTY_PUBLIC_AGENT_IDS:
                    assert registry.set_agent_visibility(agent_id, True) is True
        assert registry.get_agent_ownership(listed)["is_public"] is True
        assert registry.get_agent_ownership(unlisted)["is_public"] is False
    finally:
        with plane_runtime.transaction() as transaction:
            for agent_id in (listed, unlisted):
                plane_runtime.repositories.agents.remove_ownership(
                    transaction,
                    agent_id=agent_id,
                    owner_email=owner_email,
                )


def test_first_party_catalog_constants_match_post_029_catalog():
    from orchestrator.local_agents import FIRST_PARTY_PUBLIC_AGENT_IDS
    from orchestrator.orchestrator import RETIRED_AGENT_IDS

    ids = set(FIRST_PARTY_PUBLIC_AGENT_IDS)
    # The two 029 plug-and-play agents the walkthrough found invisible MUST
    # be in the visibility backfill.
    assert {"web-research-1", "summarizer-1"} <= ids
    # Drafts / retired ids must never be listed.
    assert not any(agent_id in ids for agent_id in RETIRED_AGENT_IDS)


# ---------------------------------------------------------------------------
# Provenance caption — REMOVED by owner decision (2026-08-03): chat replies
# carry no appended disclaimer. Pin the removal so it cannot silently return.
# ---------------------------------------------------------------------------


def test_chat_replies_carry_no_provenance_caption():
    assert not hasattr(Orchestrator, "_provenance_caption")


# ---------------------------------------------------------------------------
# PHI notice detection — fail-open semantics (030 second wave)
# ---------------------------------------------------------------------------


def _gate(analyzer):
    from personalization.phi_gate import PHIGate
    return PHIGate(analyzer=analyzer, build_if_missing=False)


class _HitAnalyzer:
    def analyze(self, text, language, entities, score_threshold):
        return [object()]


class _BoomAnalyzer:
    def analyze(self, text, language, entities, score_threshold):
        raise RuntimeError("analyzer down")


def test_detect_for_notice_prefilter_fires_without_analyzer():
    gate = _gate(None)
    assert gate.detect_for_notice("Patient MRN 000-00-0001 reported dizziness") is True


def test_detect_for_notice_fails_open_when_analyzer_missing():
    gate = _gate(None)
    # contains_phi fails CLOSED here; the notice path must NOT.
    text = "metformin dosing considerations for my trial"
    assert gate.contains_phi(text) is True
    assert gate.detect_for_notice(text) is False


def test_detect_for_notice_fails_open_on_analyzer_error():
    assert _gate(_BoomAnalyzer()).detect_for_notice("some clinical narrative") is False


def test_detect_for_notice_analyzer_hit_fires():
    assert _gate(_HitAnalyzer()).detect_for_notice("John Testcase visited") is True


def test_detect_for_notice_empty_text_clean():
    assert _gate(_HitAnalyzer()).detect_for_notice("   ") is False
    assert _gate(_HitAnalyzer()).detect_for_notice(None) is False


# ---------------------------------------------------------------------------
# Scheduling from chat (030 second wave)
# ---------------------------------------------------------------------------


def _sched_fake(manager, scheduled_job_store, agent_ids=("web-research-1",)):
    """Fake orchestrator self for scheduling_chat over the typed Plane store."""
    renders = []

    async def send_ui_render(ws, components, target="canvas"):
        renders.append((components, target))

    fake = types.SimpleNamespace(
        agent_cards={aid: object() for aid in agent_ids},
        scheduled_job_store=scheduled_job_store,
        tool_permissions=manager,
        _is_draft_agent=lambda aid: False,
        send_ui_render=send_ui_render,
    )
    fake._renders = renders
    return fake


@pytest.fixture
def sched_env(perms, plane_runtime):
    """Scheduling fixture over the same isolated application Plane."""
    from scheduler.store import ScheduledJobStore

    manager, user_id = perms
    store = ScheduledJobStore(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
    )
    return manager, store, user_id


def test_schedule_meta_tool_returns_consent_card_and_creates_nothing(sched_env):
    import asyncio

    from orchestrator import scheduling_chat

    manager, store, user_id = sched_env
    fake = _sched_fake(manager, store)
    resp = asyncio.run(scheduling_chat.handle_meta_tool(
        fake, "schedule_recurring_task",
        {"name": "Weekly digest", "instruction": "Compile new publications",
         "schedule_kind": "interval", "schedule_expr": "1d"},
        user_id=user_id, chat_id="chat-1", websocket=object()))
    assert resp.error is None
    card = resp.ui_components[0]
    assert card["type"] == "card" and "Schedule proposal" in card["title"]
    actions = [c.get("payload", {}).get("decision") for c in card["content"]
               if c.get("type") == "button"]
    assert actions == ["approve", "discard"]
    # NOTHING persisted before consent.
    assert store.list_jobs(user_id) == []
    assert len(fake._schedule_proposals) == 1


def test_schedule_meta_tool_rejects_bad_proposals(sched_env):
    import asyncio

    from orchestrator import scheduling_chat

    manager, store, user_id = sched_env
    fake = _sched_fake(manager, store)
    for args in (
        {"name": "", "instruction": "x", "schedule_kind": "interval", "schedule_expr": "1d"},
        {"name": "n", "instruction": "x", "schedule_kind": "weekly", "schedule_expr": "1d"},
        {"name": "n", "instruction": "x", "schedule_kind": "interval", "schedule_expr": "5s"},
        {"name": "n", "instruction": "x", "schedule_kind": "interval", "schedule_expr": "1d",
         "agent_id": "ghost-9"},
    ):
        resp = asyncio.run(scheduling_chat.handle_meta_tool(
            fake, "schedule_recurring_task", args,
            user_id=user_id, chat_id="c", websocket=object()))
        assert resp.error is not None, args


def test_schedule_decision_approve_creates_job_with_bounded_scopes(sched_env):
    import asyncio

    from orchestrator import scheduling_chat

    manager, store, user_id = sched_env
    manager.register_tool_scopes("web-research-1", {"web_search": "tools:search"})
    manager.set_agent_scopes(user_id, "web-research-1", {"tools:search": True})
    fake = _sched_fake(manager, store)
    asyncio.run(scheduling_chat.handle_meta_tool(
        fake, "schedule_recurring_task",
        {"name": "Weekly digest", "instruction": "Compile new publications",
         "schedule_kind": "interval", "schedule_expr": "1d",
         "agent_id": "web-research-1"},
        user_id=user_id, chat_id="chat-1", websocket=object()))
    proposal_id = next(iter(fake._schedule_proposals))
    asyncio.run(scheduling_chat.handle_decision(
        fake, object(), user_id,
        {"proposal_id": proposal_id, "decision": "approve"}))
    jobs = store.list_jobs(user_id)
    assert len(jobs) == 1
    row = jobs[0]
    assert row["name"] == "Weekly digest"
    assert row["status"] == "active"
    assert row["target_chat_id"] == "chat-1"
    assert row["offline_grant_id"] is None
    # Bounded to CURRENT grants — only the search scope that was enabled.
    assert row["consented_scopes"] == ["tools:search"]
    assert fake._schedule_proposals == {}
    # Success alert + manage button rendered to chat.
    components, target = fake._renders[-1]
    assert target == "chat"
    assert components[0]["variant"] == "success"


def test_schedule_decision_discard_and_foreign_user_refused(sched_env):
    import asyncio

    from orchestrator import scheduling_chat

    manager, store, user_id = sched_env
    fake = _sched_fake(manager, store)
    asyncio.run(scheduling_chat.handle_meta_tool(
        fake, "schedule_recurring_task",
        {"name": "n", "instruction": "x", "schedule_kind": "interval",
         "schedule_expr": "1d"},
        user_id=user_id, chat_id="c", websocket=object()))
    proposal_id = next(iter(fake._schedule_proposals))
    # Another user cannot action this proposal.
    asyncio.run(scheduling_chat.handle_decision(
        fake, object(), "someone-else", {"proposal_id": proposal_id,
                                         "decision": "approve"}))
    assert proposal_id in fake._schedule_proposals
    # Discard removes it and creates nothing.
    asyncio.run(scheduling_chat.handle_decision(
        fake, object(), user_id, {"proposal_id": proposal_id,
                                  "decision": "discard"}))
    assert fake._schedule_proposals == {}
    assert store.list_jobs(user_id) == []


def test_schedule_decision_expired_proposal_refused(sched_env):
    import asyncio

    from orchestrator import scheduling_chat

    manager, store, user_id = sched_env
    fake = _sched_fake(manager, store)
    asyncio.run(scheduling_chat.handle_meta_tool(
        fake, "schedule_recurring_task",
        {"name": "n", "instruction": "x", "schedule_kind": "interval",
         "schedule_expr": "1d"},
        user_id=user_id, chat_id="c", websocket=object()))
    proposal_id = next(iter(fake._schedule_proposals))
    fake._schedule_proposals[proposal_id]["created_at"] -= (
        scheduling_chat.PROPOSAL_TTL_S + 1)
    asyncio.run(scheduling_chat.handle_decision(
        fake, object(), user_id, {"proposal_id": proposal_id,
                                  "decision": "approve"}))
    assert fake._schedule_proposals == {}
    assert store.list_jobs(user_id) == []


def test_schedule_human_cadence_lines():
    from orchestrator.scheduling_chat import human_cadence
    assert human_cadence("interval", "1d", "UTC") == "every 1d (UTC)"
    assert "cron" in human_cadence("cron", "0 9 * * 1", "UTC")
    assert human_cadence("one_shot", "2026-07-01T09:00:00", "UTC").startswith("once")


# ---------------------------------------------------------------------------
# Welcome button accessible names (030 second wave)
# ---------------------------------------------------------------------------


def test_welcome_buttons_have_unique_accessible_names():
    components = welcome_components()
    grid = [c for c in components if c.get("type") == "grid"][0]
    labels = []
    for card in grid.get("children", []) or []:
        for child in card.get("content", []) or []:
            if child.get("type") == "button" and child.get("action") == "chat_message":
                # astralprims to_dict() merges `attributes` at the top level.
                labels.append(child.get("aria-label"))
    assert len(labels) == 6
    assert all(label and label.startswith("Run example: ") for label in labels)
    assert len(set(labels)) == 6  # all distinct


# ---------------------------------------------------------------------------
# Draft permission leakage (030 wave 3)
# ---------------------------------------------------------------------------


def test_orphan_draft_permission_sweep_and_delete_purge(
    perms,
    plane_runtime,
    tmp_path,
):
    from orchestrator.agent_lifecycle import AgentLifecycleManager
    from orchestrator.draft_plane_store import PlaneDraftStore

    manager, user_id = perms
    fake = types.SimpleNamespace(
        draft_store=PlaneDraftStore(
            plane_runtime=plane_runtime,
            plane_repositories=plane_runtime.repositories,
        ),
        _agents_dir=str(tmp_path),
    )
    purge = types.MethodType(
        AgentLifecycleManager._purge_agent_permission_rows, fake)
    fake._purge_agent_permission_rows = purge  # the sweep calls it via self
    sweep = types.MethodType(
        AgentLifecycleManager.reconcile_orphaned_draft_permissions, fake)

    orphan = f"pytest-orphan-{uuid.uuid4().hex[:6]}-1"     # no dir, no draft row
    marked = f"pytest-marked-{uuid.uuid4().hex[:6]}-1"     # dir WITH .draft, no row
    keeper = f"pytest-keeper-{uuid.uuid4().hex[:6]}-1"     # real dir, no marker
    for agent_id in (orphan, marked, keeper):
        manager.set_agent_scopes(user_id, agent_id, {"tools:read": True})
    marked_dir = tmp_path / marked[:-2].replace("-", "_")
    marked_dir.mkdir()
    (marked_dir / ".draft").write_text("x")
    (tmp_path / keeper[:-2].replace("-", "_")).mkdir()

    assert sweep(agent_ids=[orphan, marked, keeper]) == 2
    assert manager.get_agent_scopes(user_id, orphan)["tools:read"] is False
    assert manager.get_agent_scopes(user_id, marked)["tools:read"] is False
    assert manager.get_agent_scopes(user_id, keeper)["tools:read"] is True

    purge(
        keeper,
        owner_user_id=user_id,
    )  # the delete-time purge helper removes rows through Plane
    assert manager.get_agent_scopes(user_id, keeper)["tools:read"] is False


def test_legacy_directory_ownership_sweep_deletes_exact_ids_only(
    perms,
    plane_runtime,
):
    """Boot sweep for the junk rows a removed legacy filesystem discovery keyed
    by agents/ DIRECTORY names: every table is cleared for the literal id, the
    real ``<slug>-1`` runtime id and an id that merely ENDS with a target are
    untouched, and a second run is a no-op."""
    from orchestrator.agent_lifecycle import (
        LEGACY_DIRECTORY_AGENT_IDS,
        AgentLifecycleManager,
    )
    from orchestrator.draft_plane_store import PlaneDraftStore
    from orchestrator.user_agents import UserAgentRegistry

    manager, user_id = perms
    registry = UserAgentRegistry(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
    )
    fake = types.SimpleNamespace(
        draft_store=PlaneDraftStore(
            plane_runtime=plane_runtime,
            plane_repositories=plane_runtime.repositories,
        ),
    )
    sweep = types.MethodType(
        AgentLifecycleManager.reconcile_legacy_directory_ownership, fake)
    other_user = f"{user_id}-other"
    email = f"{user_id}@example.test"
    junk, real, lookalike = "weather", "weather-1", "pytest-weather"
    assert junk in LEGACY_DIRECTORY_AGENT_IDS
    assert real not in LEGACY_DIRECTORY_AGENT_IDS
    assert "tests" in LEGACY_DIRECTORY_AGENT_IDS
    try:
        for agent_id in (junk, real, lookalike, "tests"):
            registry.set_agent_ownership(agent_id, email, is_public=True)
            manager.set_agent_scopes(user_id, agent_id, {"tools:read": True})
        # An ownerless override (no scope row to enumerate its owner from).
        manager.set_tool_overrides(other_user, junk, {"get_weather": False})
        assert manager.get_tool_overrides(other_user, junk) == {"get_weather": False}
        registry.upsert_agent_safe(junk, True, marked_by="pytest")
        registry.upsert_agent_safe(real, True, marked_by="pytest")

        removed = sweep(agent_ids=[junk, "tests", real, lookalike])
        assert removed["agent_ownership"] == 2            # weather + tests
        assert removed["policy_rows"] >= 3                # 2 scope rows + 1 override
        assert removed["agent_trust_neutralised"] == 1    # weather only

        assert registry.get_agent_ownership(junk) is None
        assert registry.get_agent_ownership("tests") is None
        assert registry.get_agent_ownership(real)["owner_email"] == email
        assert registry.get_agent_ownership(lookalike)["owner_email"] == email
        assert manager.get_agent_scopes(user_id, junk)["tools:read"] is False
        assert manager.get_agent_scopes(user_id, real)["tools:read"] is True
        assert manager.get_agent_scopes(user_id, lookalike)["tools:read"] is True
        assert manager.get_tool_overrides(other_user, junk) == {}
        assert registry.get_agent_is_safe(junk) is False
        assert registry.get_agent_is_safe(real) is True

        # Repeat-safe: nothing left to match, nothing written.
        assert sweep(agent_ids=[junk, "tests", real, lookalike]) == {
            "agent_ownership": 0, "policy_rows": 0, "agent_trust_neutralised": 0,
        }
        # The candidate restriction can only narrow the literal set.
        assert sweep(agent_ids=[real, lookalike, "not-a-legacy-id"]) == {
            "agent_ownership": 0, "policy_rows": 0, "agent_trust_neutralised": 0,
        }
    finally:
        with plane_runtime.transaction() as transaction:
            repos = plane_runtime.repositories
            for agent_id in (junk, real, lookalike, "tests"):
                repos.agents.remove_ownership(
                    transaction, agent_id=agent_id, owner_email=email)
                repos.tool_policy_state.remove_agent_state(
                    transaction, owner_id=user_id, agent_id=agent_id)
            repos.tool_policy_state.remove_owner_state(
                transaction, owner_id=other_user)


# ---------------------------------------------------------------------------
# Chat-vs-canvas narrative split (030 wave 3)
# ---------------------------------------------------------------------------


def test_narrative_is_long_detects_length_headings_tables():
    assert Orchestrator._narrative_is_long("x" * 800) is True
    assert Orchestrator._narrative_is_long("## Specific Aims\nshort") is True
    assert Orchestrator._narrative_is_long("| a | b |\n| 1 | 2 |") is True
    assert Orchestrator._narrative_is_long("A short plain answer.") is False


def test_concise_lead_strips_structure_and_ends_at_sentence():
    content = ("# Title\nFirst sentence of the lead. Second sentence here. "
               + "Filler words " * 60 + "\n| t | r |\n# H2\nmore")
    lead = Orchestrator._concise_lead(content)
    assert lead.startswith("First sentence of the lead.")
    assert "#" not in lead and "|" not in lead
    assert len(lead) <= 321


def test_narrative_doc_card_identity_stable_per_title():
    a1 = Orchestrator._narrative_doc_card("chat-1", "## Specific Aims\nv1 text")
    a2 = Orchestrator._narrative_doc_card("chat-1", "## Specific Aims\nv2 revised")
    b = Orchestrator._narrative_doc_card("chat-1", "## Budget Plan\ntext")
    other_chat = Orchestrator._narrative_doc_card("chat-2", "## Specific Aims\nv1")
    assert a1["id"] == a2["id"]            # same doc iterates in place
    assert a1["id"] != b["id"]             # different doc appends
    assert a1["id"] != other_chat["id"]    # per-chat identity
    assert a1["title"] == "Specific Aims"
    assert a1["content"][0]["variant"] == "markdown"


# ---------------------------------------------------------------------------
# Tool dispatch hardening (030 wave 3)
# ---------------------------------------------------------------------------


def test_tool_timeout_overrides_cover_long_running_verbs():
    from orchestrator.orchestrator import TOOL_TIMEOUT_OVERRIDES
    assert TOOL_TIMEOUT_OVERRIDES["research_brief"] > 100
    assert TOOL_TIMEOUT_OVERRIDES.get("fetch_page", 0) > 30


def test_draft_decision_cards_carry_stable_author_id():
    from orchestrator.agentic_creation import _terminal_card, creation_card
    draft = {"id": "d-123", "agent_name": "Web Researcher", "description": "d"}
    live = creation_card(draft, {"status": "passed", "summary": "ok"})
    done = _terminal_card("d-123", "Discarded: Web Researcher", "Removed.")
    assert live["id"] == "draft-card-d-123" == done["id"]
    # Terminal card must carry no actionable buttons.
    assert all(c.get("type") != "button" for c in done["content"])
    assert any(c.get("type") == "button" for c in live["content"])


def test_renderer_honors_flattened_attributes_shape():
    """astralprims to_dict() flattens `attributes` to top-level keys — the
    renderer must honor both that and the nested hand-built shape (030)."""
    from webrender.renderer import _base_attrs

    flattened = {"type": "button", "label": "x", "aria-label": "Run example: A"}
    nested = {"type": "button", "label": "x",
              "attributes": {"aria-label": "Run example: B"}}
    hostile = {"type": "button", "label": "x", "onclick": "alert(1)",
               "aria-label": 'x" onmouseover="alert(1)'}
    assert 'aria-label="Run example: A"' in _base_attrs(flattened)
    assert 'aria-label="Run example: B"' in _base_attrs(nested)
    out = _base_attrs(hostile)
    assert "onclick" not in out
    assert 'onmouseover="alert' not in out  # escaped, not live
