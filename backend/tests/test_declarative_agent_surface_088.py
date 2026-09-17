"""Chrome dispatch surface for declarative agents (feature 088 T032/T037).

Exercises the actual ``chrome_declarative_view``/``chrome_declarative_command``
HANDLERS entries and the "agent_authoring" surface's ``render``/``components``
declarative sub-view, over the SAME real IAM/Plane/audit qualified by
``test_declarative_agent_lifecycle_postgres_088`` — never a synthetic caller,
and never the service layer directly for the behaviors under test here.

The two wire actions are DEFINED in ``authoring.py`` but REGISTERED in
``guidance.HANDLERS`` (test_declarative_agent_definition_088.py pins
``authoring.HANDLERS`` to name no "declarative" action — see the comment at
the registration site in guidance.py for why); ``chrome_events.
collect_handlers()`` aggregates every surface module by action name alone, so
this is a dispatch-transparent relocation, not a behavior change.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator.human_request_authority import bind_human_caller
from orchestrator.projection_surfaces import authoring, guidance
from persistent_agents.models import AssignmentError
from tests.test_declarative_agent_lifecycle_postgres_088 import (
    api as api, body as body, caller as caller, counts as counts, declarations as declarations,
    fixture as fixture, next_body as next_body, plane as plane, research_definition as research_definition,
    research_service as research_service, runtime as runtime, service as service,
    signing_key as signing_key, source_service as source_service,
)

# pytest.ini sets asyncio_mode = auto: async tests below need no marker.


@pytest.fixture
def wired(declarations):
    """The fixture under test: what orchestrator boot wires next to explicit_notes."""
    declarations.api.orch.declarative_agents = declarations.service
    return declarations


async def dispatch(state, fn, payload, *, owner_caller=None, user_id=None):
    """Bind one authenticated caller and invoke a HANDLERS-contract function."""
    current = owner_caller or await caller(state)
    with bind_human_caller(current):
        return await fn(state.api.orch, None, user_id or current.owner_id, [], payload)


async def render_declarative(state, params, *, owner_caller=None):
    current = owner_caller or await caller(state)
    with bind_human_caller(current):
        return await authoring.render(state.api.orch, current.owner_id, [], {"declarative": params})


async def components_declarative(state, params, *, owner_caller=None):
    current = owner_caller or await caller(state)
    with bind_human_caller(current):
        return await authoring.components(state.api.orch, current.owner_id, [], {"declarative": params})


def test_handlers_are_registered_under_their_wire_names():
    # Registered in guidance.HANDLERS, not authoring.HANDLERS — see the
    # module docstring above and the registration comment in guidance.py.
    assert "chrome_declarative_view" not in authoring.HANDLERS
    assert "chrome_declarative_command" not in authoring.HANDLERS
    assert guidance.HANDLERS["chrome_declarative_view"] is authoring._h_declarative_view
    assert guidance.HANDLERS["chrome_declarative_command"] is authoring._h_declarative_command
    from orchestrator.projection_surfaces import collect_handlers
    aggregated = collect_handlers()
    assert aggregated["chrome_declarative_view"] == ("guidance", authoring._h_declarative_view)
    assert aggregated["chrome_declarative_command"] == ("guidance", authoring._h_declarative_command)


async def test_view_and_command_401_without_a_pending_human_caller(declarations):
    orch = declarations.api.orch
    owner = declarations.api.fixture[1]
    with pytest.raises(AssignmentError, match="declarative_authentication_required") as caught:
        await authoring._h_declarative_view(orch, None, owner, [], {"mode": "list"})
    assert caught.value.status_code == 401
    with pytest.raises(AssignmentError, match="declarative_authentication_required"):
        await authoring._h_declarative_command(orch, None, owner, [], body().model_dump())


async def test_flag_off_refuses_before_touching_iam(declarations, monkeypatch):
    from shared.feature_flags import flags
    monkeypatch.setitem(flags._flags, "byo_agents", False)
    surface, params, notice = await authoring._h_declarative_view(
        declarations.api.orch, None, declarations.api.fixture[1], [], {"mode": "list"})
    assert surface == authoring.SURFACE_KEY and params == {}
    assert "not enabled" in notice
    surface, params, notice = await authoring._h_declarative_command(
        declarations.api.orch, None, declarations.api.fixture[1], [], body().model_dump())
    assert surface == authoring.SURFACE_KEY and params == {} and "not enabled" in notice


async def test_create_command_renders_in_the_list_view(wired):
    state = wired
    surface, params, notice = await dispatch(
        state, authoring._h_declarative_command, body(display_name="Evidence agent").model_dump())
    assert surface == authoring.SURFACE_KEY
    assert params == {"declarative": {"mode": "list"}}
    assert "Agent created." in notice
    assert counts(state) == (1, 1, 1, 1)

    html = await render_declarative(state, {"mode": "list"})
    assert "Evidence agent" in html and "← My agents" in html

    comps = await components_declarative(state, {"mode": "list"})
    assert isinstance(comps, list) and any("Evidence agent" in str(item) for item in comps)


async def test_exact_receipt_replay_through_the_handler_creates_no_second_row(wired):
    state = wired
    payload = body(display_name="Replay agent").model_dump()
    first = await dispatch(state, authoring._h_declarative_command, payload)
    second = await dispatch(state, authoring._h_declarative_command, payload)
    assert first == second
    assert counts(state) == (1, 1, 1, 1)


async def test_malformed_command_payload_is_a_closed_refusal(declarations):
    with pytest.raises(AssignmentError, match="declarative_command_invalid"):
        await dispatch(declarations, authoring._h_declarative_command, {"not": "a valid command"})


async def test_history_activate_archive_delete_round_trip_through_the_handler(wired):
    state = wired
    result = await state.service.command(caller=await caller(state),
        body=body(display_name="Lifecycle agent", definition=research_definition(state)))
    agent_id = result.agent.agent_id
    revision_id = result.revision.revision_id

    html = await render_declarative(state, {"mode": "history", "agent_id": agent_id})
    assert "Lifecycle agent" in html and "Revision 1" in html

    activation = next_body(result, "activate", revision_id=revision_id).model_dump()
    surface, params, notice = await dispatch(state, authoring._h_declarative_command, activation)
    assert "Revision activated." in notice and params == {"declarative": {"mode": "list"}}
    html = await render_declarative(state, {"mode": "list"})
    assert "Active" in html

    with state.api.runtime.transaction() as tx:
        row = tx.fetch_one(
            "SELECT state_revision FROM user_agent WHERE agent_id=%s", (agent_id,))
    archive = next_body(SimpleNamespace(agent=SimpleNamespace(
        agent_id=agent_id, state_revision=row["state_revision"])), "archive").model_dump()
    surface, params, notice = await dispatch(state, authoring._h_declarative_command, archive)
    assert "Agent archived." in notice

    with state.api.runtime.transaction() as tx:
        row = tx.fetch_one(
            "SELECT state_revision FROM user_agent WHERE agent_id=%s", (agent_id,))
    delete = next_body(SimpleNamespace(agent=SimpleNamespace(
        agent_id=agent_id, state_revision=row["state_revision"])), "delete").model_dump()
    surface, params, notice = await dispatch(state, authoring._h_declarative_command, delete)
    assert "Agent deleted." in notice
    # create + activate + archive + delete = 4 receipts and 4 audit rows.
    assert counts(state) == (1, 1, 4, 4)


async def test_foreign_owner_sees_no_rows_and_cannot_reach_the_history(wired):
    state = wired
    result = await state.service.command(caller=await caller(state),
        body=body(display_name="Owner-only agent"))
    other = await caller(state, cookie=False, owner="another-owner")

    html = await render_declarative(state, {"mode": "list"}, owner_caller=other)
    assert "Owner-only agent" not in html

    with bind_human_caller(other):
        with pytest.raises(AssignmentError, match="declarative_not_found"):
            await authoring.declarative_view_state(
                state.api.orch, other, {"mode": "history", "agent_id": result.agent.agent_id})


async def test_declarative_link_appears_on_the_ordinary_home_page(
    declarations, monkeypatch, user_skills_disabled,
):
    from orchestrator import agent_authoring as aa
    # The step-editor session list needs a Plane draft store this lightweight
    # test double never wires; stubbing it is orthogonal to what is under test
    # here (that the new nav entry point renders on the ordinary home page).
    # ``user_skills_disabled`` sidesteps the skills catalog's os.O_DIRECTORY
    # capture, which the pinned test suite runs under Linux only.
    monkeypatch.setattr(aa, "list_sessions", lambda *_a, **_k: [])
    current = await caller(declarations)
    with bind_human_caller(current):
        html = await authoring.render(declarations.api.orch, current.owner_id, [], {})
    assert "Declarative agents (preview)" in html
