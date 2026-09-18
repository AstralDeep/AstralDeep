"""Feature 089 (T022): the routing seams inside the real turn loop.

Every test drives ``handle_chat_message`` with a stubbed LLM and the
deterministic TypeSafe fake, so the question under test is always "what did the
orchestrator actually do", not "what would it do if the wiring were right".

The invariant that governs the whole file: **a user with no key must get a
byte-identical first round.** Several tests here exist only to hold that line,
because the cost of breaking it is paid by every user who never opted in.
"""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tests.fakes.typesafe_fake import (  # noqa: E402
    AnswerSet,
    FakeTypeSafeClient,
    TypeSafeAPITimeoutError,
    TypeSafeAuthenticationError,
    high_confidence,
    low_confidence,
    medium_confidence,
)

KEY = "ts_live_CANARY0000NOTAREALKEY000000"
USER = "ts-turn-user"
AGENT = "weather-1"
TOOL_A = "get_current_weather"
TOOL_B = "get_daily_forecast"


# -- harness -------------------------------------------------------------


#: Platform meta-tools that the orchestrator injects into EVERY chat turn when
#: their flags are on (agentic creation, chat memory, scheduling, desktop
#: codegen). They are not part of any agent card, so a test that asserts "round
#: one saw the full eligible list" has to account for them or pin them off.
#:
#: Pinning them off is what this fixture does, and it is deliberate: these tests
#: are about what TypeSafe routing does to the tool list, not about which
#: platform tools a given deployment enables. Without it the expectations below
#: silently depend on deployment configuration -- which is exactly how they came
#: to pass in CI and in the T058 suite run (whose `docker run --env-file` left
#: the flags unparseable, hence false) while failing in any environment that
#: reads the same `.env` through compose. See verification.md 7i.
_PLATFORM_META_TOOL_FLAGS = (
    "agentic_creation",
    "memory_chat",
    "scheduling_chat",
    "desktop_codegen",
)


@pytest.fixture(autouse=True)
def platform_meta_tools_disabled(monkeypatch):
    """Keep the eligible tool list to the registered agent's own tools."""
    from shared.feature_flags import flags as global_flags

    original = global_flags.is_enabled
    monkeypatch.setattr(
        global_flags,
        "is_enabled",
        lambda name: False if name in _PLATFORM_META_TOOL_FLAGS else original(name),
    )


@pytest.fixture
def orch(orchestrator_factory):
    o = orchestrator_factory()
    o._llm_store.set_sync(
        USER,
        provider="openai",
        base_url="http://test.invalid/v1",
        model="test-model",
        api_key="sk-test-000000000000000000000",
    )
    o.audit_recorder = MagicMock()
    o.audit_recorder.record = AsyncMock()
    o._record_llm_call = AsyncMock()
    o._record_llm_unconfigured = AsyncMock()
    o._safe_send = AsyncMock()
    o.send_ui_render = AsyncMock()
    hb = MagicMock()
    hb.cancel = MagicMock()
    o._start_heartbeat = AsyncMock(return_value=hb)
    o._send_or_replace_components = AsyncMock()
    o._emit_llm_usage_report = AsyncMock()
    o._deliver_round_components = AsyncMock(return_value=[])
    # The credential store is durable, so a key saved by one test would still
    # be there for the next one and quietly invalidate every "no key" test.
    o._typesafe_store.clear_sync(USER)
    o._typesafe_store.invalidate(USER)
    from orchestrator.typesafe_routing.budget import UserCircuit, set_circuit

    set_circuit(UserCircuit())
    return o


def _register(o, agent_id=AGENT, tools=(TOOL_A, TOOL_B)):
    from shared.protocol import AgentCard, AgentSkill

    o.agent_cards[agent_id] = AgentCard(
        name="Weather",
        description="Live conditions and forecasts",
        agent_id=agent_id,
        skills=[
            AgentSkill(
                name=t, description=f"does {t}", id=t, input_schema={"type": "object"}
            )
            for t in tools
        ],
    )
    o.agents[agent_id] = MagicMock()
    o.tool_permissions = MagicMock()
    o.tool_permissions.is_tool_allowed.return_value = True


def _ws(o, user_id=USER):
    ws = MagicMock()
    o.ui_sessions[ws] = {"sub": user_id, "preferred_username": user_id}
    return ws


def _msg(content=None, tool_calls=None):
    return SimpleNamespace(
        role="assistant", content=content, tool_calls=tool_calls, reasoning_content=None
    )


def _usage():
    return SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2)


class _Recorder:
    """Captures every ``_call_llm`` invocation so round one can be inspected."""

    def __init__(self, reply=None):
        self.calls = []
        self._reply = reply or _msg(content="done")

    async def __call__(self, websocket, messages, tools_desc=None, **kwargs):
        self.calls.append({"tools_desc": tools_desc, "kwargs": dict(kwargs)})
        return self._reply, _usage()

    @property
    def round_one_names(self):
        first = self.calls[0]["tools_desc"] or []
        return [(e.get("function") or {}).get("name") for e in first]

    @property
    def round_one_choice(self):
        return self.calls[0]["kwargs"].get("tool_choice")


def _install_key(o, key=KEY):
    """Give the fixture user a stored TypeSafe key."""
    o._typesafe_store.save_sync(USER, key)


def _install_fake(o, monkeypatch, fake):
    """Route every adapter call through the deterministic fake."""
    import orchestrator.typesafe_routing.runner as runner

    monkeypatch.setattr(runner, "default_adapter_client", lambda: fake)
    from orchestrator.typesafe_routing.budget import UserCircuit, set_circuit

    set_circuit(UserCircuit())
    return fake


async def _turn(o, ws, chat_id, text="What's the weather in Lexington right now?"):
    await asyncio.to_thread(o.history.create_chat, chat_id, user_id=USER)
    await o.handle_chat_message(ws, text, chat_id, user_id=USER)


# -- invariant 1: no key changes nothing ---------------------------------


@pytest.mark.asyncio
async def test_no_key_makes_zero_calls_and_leaves_round_one_identical(
    orch, monkeypatch, user_skills_disabled
):
    _register(orch)
    fake = _install_fake(orch, monkeypatch, FakeTypeSafeClient(default=high_confidence(AGENT, TOOL_A)))
    recorder = _Recorder()
    monkeypatch.setattr(orch, "_call_llm", recorder)

    await _turn(orch, _ws(orch), f"ts-{uuid.uuid4().hex[:8]}")

    assert fake.call_count == 0
    assert sorted(recorder.round_one_names) == sorted([TOOL_A, TOOL_B])
    # The historical signature: no tool_choice keyword at all.
    assert "tool_choice" not in recorder.calls[0]["kwargs"]


@pytest.mark.asyncio
async def test_the_kill_switch_reproduces_the_no_key_path(
    orch, monkeypatch, user_skills_disabled
):
    _register(orch)
    _install_key(orch)
    fake = _install_fake(orch, monkeypatch, FakeTypeSafeClient(default=high_confidence(AGENT, TOOL_A)))
    recorder = _Recorder()
    monkeypatch.setattr(orch, "_call_llm", recorder)
    # Turn off only this flag. Rebuilding the whole FeatureFlags object would
    # reset every other flag to its env default and break unrelated subsystems.
    from shared.feature_flags import flags as global_flags

    original = global_flags.is_enabled
    monkeypatch.setattr(
        global_flags,
        "is_enabled",
        lambda name: False if name == "typesafe_routing" else original(name),
    )

    await _turn(orch, _ws(orch), f"ts-{uuid.uuid4().hex[:8]}")

    assert fake.call_count == 0
    assert sorted(recorder.round_one_names) == sorted([TOOL_A, TOOL_B])
    assert "tool_choice" not in recorder.calls[0]["kwargs"]


# -- tier effects on round one -------------------------------------------


@pytest.mark.asyncio
async def test_high_tier_sends_one_tool_and_forces_it(
    orch, monkeypatch, user_skills_disabled
):
    _register(orch)
    _install_key(orch)
    fake = _install_fake(
        orch, monkeypatch, FakeTypeSafeClient(default=high_confidence(AGENT, TOOL_A))
    )
    recorder = _Recorder()
    monkeypatch.setattr(orch, "_call_llm", recorder)

    await _turn(orch, _ws(orch), f"ts-{uuid.uuid4().hex[:8]}")

    assert fake.call_count == 1
    assert recorder.round_one_names == [TOOL_A]
    assert recorder.round_one_choice == {
        "type": "function",
        "function": {"name": TOOL_A},
    }


@pytest.mark.asyncio
async def test_medium_tier_sends_a_shortlist_without_forcing(
    orch, monkeypatch, user_skills_disabled
):
    _register(orch)
    _install_key(orch)
    _install_fake(
        orch,
        monkeypatch,
        FakeTypeSafeClient(default=medium_confidence(AGENT, [TOOL_A, TOOL_B])),
    )
    recorder = _Recorder()
    monkeypatch.setattr(orch, "_call_llm", recorder)

    await _turn(orch, _ws(orch), f"ts-{uuid.uuid4().hex[:8]}")

    assert set(recorder.round_one_names) == {TOOL_A, TOOL_B}
    assert "tool_choice" not in recorder.calls[0]["kwargs"]


@pytest.mark.asyncio
async def test_low_tier_leaves_round_one_untouched(
    orch, monkeypatch, user_skills_disabled
):
    _register(orch)
    _install_key(orch)
    _install_fake(orch, monkeypatch, FakeTypeSafeClient(default=low_confidence()))
    recorder = _Recorder()
    monkeypatch.setattr(orch, "_call_llm", recorder)

    await _turn(orch, _ws(orch), f"ts-{uuid.uuid4().hex[:8]}")

    assert sorted(recorder.round_one_names) == sorted([TOOL_A, TOOL_B])
    assert "tool_choice" not in recorder.calls[0]["kwargs"]


@pytest.mark.asyncio
async def test_an_unsupported_provider_preset_never_forces_a_choice(
    orch, monkeypatch, user_skills_disabled
):
    """A forced choice an endpoint cannot parse turns a narrowed round into a failed one."""
    _register(orch)
    _install_key(orch)
    orch._llm_store.set_sync(
        USER,
        provider="ollama",
        base_url="http://localhost:11434/v1",
        model="llama",
        api_key="",
    )
    _install_fake(
        orch, monkeypatch, FakeTypeSafeClient(default=high_confidence(AGENT, TOOL_A))
    )
    recorder = _Recorder()
    monkeypatch.setattr(orch, "_call_llm", recorder)

    await _turn(orch, _ws(orch), f"ts-{uuid.uuid4().hex[:8]}")

    assert recorder.round_one_names == [TOOL_A]
    assert "tool_choice" not in recorder.calls[0]["kwargs"]


@pytest.mark.asyncio
async def test_an_ineligible_tool_in_the_answer_is_ignored(
    orch, monkeypatch, user_skills_disabled
):
    """A decision can only ever narrow to tools the user was already allowed."""
    _register(orch)
    _install_key(orch)
    _install_fake(
        orch,
        monkeypatch,
        FakeTypeSafeClient(default=high_confidence(AGENT, "a_tool_that_does_not_exist")),
    )
    recorder = _Recorder()
    monkeypatch.setattr(orch, "_call_llm", recorder)

    await _turn(orch, _ws(orch), f"ts-{uuid.uuid4().hex[:8]}")

    assert sorted(recorder.round_one_names) == sorted([TOOL_A, TOOL_B])


# -- round two and later --------------------------------------------------


@pytest.mark.asyncio
async def test_round_two_always_sees_the_full_eligible_list(
    orch, monkeypatch, user_skills_disabled
):
    """A wrong first guess costs one round, never the turn."""
    _register(orch)
    _install_key(orch)
    _install_fake(
        orch, monkeypatch, FakeTypeSafeClient(default=high_confidence(AGENT, TOOL_A))
    )

    replies = [
        _msg(tool_calls=[SimpleNamespace(
            id="c1", function=SimpleNamespace(name=TOOL_A, arguments="{}")
        )]),
        _msg(content="all done"),
    ]
    calls = []

    async def fake_llm(websocket, messages, tools_desc=None, **kwargs):
        calls.append({"tools_desc": tools_desc, "kwargs": dict(kwargs)})
        return replies[min(len(calls) - 1, len(replies) - 1)], _usage()

    monkeypatch.setattr(orch, "_call_llm", fake_llm)
    monkeypatch.setattr(orch, "execute_single_tool", AsyncMock(return_value=None))

    await _turn(orch, _ws(orch), f"ts-{uuid.uuid4().hex[:8]}")

    assert len(calls) >= 2
    first = [(e.get("function") or {}).get("name") for e in (calls[0]["tools_desc"] or [])]
    second = [(e.get("function") or {}).get("name") for e in (calls[1]["tools_desc"] or [])]
    assert first == [TOOL_A]
    assert sorted(second) == sorted([TOOL_A, TOOL_B])
    assert "tool_choice" not in calls[1]["kwargs"]


@pytest.mark.asyncio
async def test_narrowing_also_removes_the_platform_meta_tools(
    orch, monkeypatch, user_skills_disabled
):
    """With the platform meta-tools ON, round one is still only the routed tool.

    Every other test in this module pins the meta-tools off so its expectations
    say what they mean. This one deliberately turns them back on, because the
    interaction is real product behaviour and nothing else covers it: a keyed
    high-tier turn narrows away `remember`, `create_capability`,
    `schedule_recurring_task` and friends along with the unrouted agent tools,
    and round two gets all of them back. See verification.md 7i.
    """
    from shared.feature_flags import flags as global_flags

    narrowed = global_flags.is_enabled  # the autouse fixture's view
    monkeypatch.setattr(
        global_flags,
        "is_enabled",
        lambda name: True if name in _PLATFORM_META_TOOL_FLAGS else narrowed(name),
    )

    _register(orch)
    _install_key(orch)
    _install_fake(
        orch, monkeypatch, FakeTypeSafeClient(default=high_confidence(AGENT, TOOL_A))
    )

    replies = [
        _msg(tool_calls=[SimpleNamespace(
            id="c1", function=SimpleNamespace(name=TOOL_A, arguments="{}")
        )]),
        _msg(content="all done"),
    ]
    calls = []

    async def fake_llm(websocket, messages, tools_desc=None, **kwargs):
        calls.append({"tools_desc": tools_desc, "kwargs": dict(kwargs)})
        return replies[min(len(calls) - 1, len(replies) - 1)], _usage()

    monkeypatch.setattr(orch, "_call_llm", fake_llm)
    monkeypatch.setattr(orch, "execute_single_tool", AsyncMock(return_value=None))

    await _turn(orch, _ws(orch), f"ts-{uuid.uuid4().hex[:8]}")

    assert len(calls) >= 2
    first = [(e.get("function") or {}).get("name") for e in (calls[0]["tools_desc"] or [])]
    second = [(e.get("function") or {}).get("name") for e in (calls[1]["tools_desc"] or [])]

    # Round one: the routed tool alone -- no agent siblings, no meta-tools.
    assert first == [TOOL_A]
    # Round two: the agent's tools back, and the meta-tools with them.
    assert {TOOL_A, TOOL_B} <= set(second)
    assert "create_capability" in second, (
        "round two should restore the platform meta-tools; if this fails the "
        "meta-tool flags are off and this test is no longer testing anything"
    )


# -- turns that must not call at all --------------------------------------


@pytest.mark.asyncio
async def test_an_empty_eligible_set_makes_no_call(
    orch, monkeypatch, user_skills_disabled
):
    _install_key(orch)  # a key, but no agents registered
    fake = _install_fake(orch, monkeypatch, FakeTypeSafeClient(default=low_confidence()))
    monkeypatch.setattr(orch, "_call_llm", _Recorder())

    await _turn(orch, _ws(orch), f"ts-{uuid.uuid4().hex[:8]}")

    assert fake.call_count == 0


# -- failure never reaches the user ---------------------------------------


@pytest.mark.asyncio
async def test_a_transport_failure_falls_back_to_the_full_list(
    orch, monkeypatch, user_skills_disabled
):
    _register(orch)
    _install_key(orch)
    _install_fake(
        orch,
        monkeypatch,
        FakeTypeSafeClient(
            default=high_confidence(AGENT, TOOL_A),
            faults=[TypeSafeAPITimeoutError, TypeSafeAPITimeoutError, TypeSafeAPITimeoutError],
        ),
    )
    recorder = _Recorder()
    monkeypatch.setattr(orch, "_call_llm", recorder)

    await _turn(orch, _ws(orch), f"ts-{uuid.uuid4().hex[:8]}")

    assert sorted(recorder.round_one_names) == sorted([TOOL_A, TOOL_B])
    assert "tool_choice" not in recorder.calls[0]["kwargs"]


@pytest.mark.asyncio
async def test_an_auth_failure_marks_the_key_rejected(
    orch, monkeypatch, user_skills_disabled
):
    _register(orch)
    _install_key(orch)
    _install_fake(
        orch,
        monkeypatch,
        FakeTypeSafeClient(
            default=high_confidence(AGENT, TOOL_A), faults=[TypeSafeAuthenticationError]
        ),
    )
    monkeypatch.setattr(orch, "_call_llm", _Recorder())

    await _turn(orch, _ws(orch), f"ts-{uuid.uuid4().hex[:8]}")
    # Outcome recording is fire-and-forget; let the scheduled task land.
    for _ in range(50):
        if orch._typesafe_store.status_sync(USER).name == "rejected":
            break
        await asyncio.sleep(0.01)

    assert orch._typesafe_store.status_sync(USER).name == "rejected"


@pytest.mark.asyncio
async def test_a_successful_turn_marks_the_key_valid(
    orch, monkeypatch, user_skills_disabled
):
    _register(orch)
    _install_key(orch)
    _install_fake(
        orch, monkeypatch, FakeTypeSafeClient(default=high_confidence(AGENT, TOOL_A))
    )
    monkeypatch.setattr(orch, "_call_llm", _Recorder())
    orch._typesafe_store.record_outcome_sync(
        USER, "unavailable", orch._typesafe_store.get_key_sync(USER).fingerprint
    )

    await _turn(orch, _ws(orch), f"ts-{uuid.uuid4().hex[:8]}")
    for _ in range(50):
        if orch._typesafe_store.status_sync(USER).name == "active":
            break
        await asyncio.sleep(0.01)

    assert orch._typesafe_store.status_sync(USER).name == "active"


# -- the request never carries what it must not ---------------------------


@pytest.mark.asyncio
async def test_the_request_carries_no_key_and_no_tool_output(
    orch, monkeypatch, user_skills_disabled
):
    _register(orch)
    _install_key(orch)
    fake = _install_fake(
        orch, monkeypatch, FakeTypeSafeClient(default=high_confidence(AGENT, TOOL_A))
    )
    monkeypatch.setattr(orch, "_call_llm", _Recorder())

    await _turn(orch, _ws(orch), f"ts-{uuid.uuid4().hex[:8]}")

    state = fake.calls[0].state
    rendered = str(state)
    assert KEY not in rendered
    assert set(state) == {
        "current_request",
        "recent_conversation",
        "active_agent",
        "user_selected_tools",
    }


@pytest.mark.asyncio
async def test_the_key_travels_only_as_the_api_key_argument(
    orch, monkeypatch, user_skills_disabled
):
    _register(orch)
    _install_key(orch)
    fake = _install_fake(
        orch, monkeypatch, FakeTypeSafeClient(default=high_confidence(AGENT, TOOL_A))
    )
    monkeypatch.setattr(orch, "_call_llm", _Recorder())

    await _turn(orch, _ws(orch), f"ts-{uuid.uuid4().hex[:8]}")

    call = fake.calls[0]
    assert call.api_key == KEY
    assert KEY not in str(call.questions)
    # The recorded Call dataclass hides the key from repr for the same reason
    # the store's record does: it gets logged.
    assert KEY not in repr(call)


@pytest.mark.asyncio
async def test_environment_typesafe_variables_are_never_consulted(
    orch, monkeypatch, user_skills_disabled
):
    """FR-005 at the turn level, not just at boot."""
    monkeypatch.setenv("TYPESAFE_API_KEY", "env-key-must-not-be-used")
    monkeypatch.setenv("TYPESAFE_BASE_URL", "https://env.invalid")
    monkeypatch.setenv("TYPESAFE_DEFAULT_MODEL", "env-model")
    _register(orch)
    fake = _install_fake(
        orch, monkeypatch, FakeTypeSafeClient(default=high_confidence(AGENT, TOOL_A))
    )
    monkeypatch.setattr(orch, "_call_llm", _Recorder())

    await _turn(orch, _ws(orch), f"ts-{uuid.uuid4().hex[:8]}")

    # No stored key means no call, whatever the environment says.
    assert fake.call_count == 0


# -- at most one call per turn --------------------------------------------


@pytest.mark.asyncio
async def test_at_most_three_attempts_and_one_decision_per_turn(
    orch, monkeypatch, user_skills_disabled
):
    _register(orch)
    _install_key(orch)
    fake = _install_fake(
        orch,
        monkeypatch,
        FakeTypeSafeClient(
            default=high_confidence(AGENT, TOOL_A),
            faults=[TypeSafeAPITimeoutError, None],
        ),
    )
    recorder = _Recorder()
    monkeypatch.setattr(orch, "_call_llm", recorder)

    await _turn(orch, _ws(orch), f"ts-{uuid.uuid4().hex[:8]}")

    assert fake.call_count == 2  # one failure, one success
    assert recorder.round_one_names == [TOOL_A]


@pytest.mark.asyncio
async def test_a_malformed_answer_degrades_to_the_full_list(
    orch, monkeypatch, user_skills_disabled
):
    _register(orch)
    _install_key(orch)
    _install_fake(
        orch, monkeypatch, FakeTypeSafeClient(default=AnswerSet(agent_id=None))
    )
    recorder = _Recorder()
    monkeypatch.setattr(orch, "_call_llm", recorder)

    await _turn(orch, _ws(orch), f"ts-{uuid.uuid4().hex[:8]}")

    assert sorted(recorder.round_one_names) == sorted([TOOL_A, TOOL_B])
