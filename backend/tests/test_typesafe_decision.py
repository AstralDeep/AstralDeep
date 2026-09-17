"""Feature 089 (T012): the question set, the bounded request, and the tiers.

The theme running through every test here is that uncertainty must degrade to
today's behavior. A missing answer, an invented option, a tool that is not in
the eligible set: each of these produces the low tier, and the low tier means
round one is byte-identical to the unkeyed path.
"""

from __future__ import annotations

import pytest

from orchestrator.typesafe_routing.decision import (
    FORCED_CHOICE_PROVIDERS,
    RoundOnePlan,
    RoutingDecision,
    Tier,
    apply_round_one,
    parse_decision,
    parse_security,
)
from orchestrator.typesafe_routing.questions import (
    MAX_ROUTING_AGENTS,
    MAX_TOOLS_PER_AGENT,
    NONE_FIT,
    NO_TOOL_NEEDED,
    PRESENTATION_STYLES,
    QUESTION_HARM_SCORE,
    QUESTION_IS_JAILBREAK,
    QUESTION_PRESENTATION_STYLE,
    QUESTION_TARGET_AGENT,
    QUESTION_THREAT_CATEGORY,
    ROUTING_MAX_HISTORY_MESSAGES,
    ROUTING_MAX_REQUEST_CHARS,
    SECURITY_QUESTION_IDS,
    TOOL_QUESTION_PREFIX,
    AgentOption,
    RoutingRequest,
    ToolOption,
    build_questions,
    build_security_question_set,
)
from tests.fakes.typesafe_fake import (
    ChoiceAnswer,
    FakeResponse,
    NoulAnswer,
    ScoreAnswer,
    fake_sdk_surface,
)

SDK = fake_sdk_surface()

WEATHER = AgentOption("weather-1", "Weather", "Live conditions and forecasts")
GENERAL = AgentOption("general-1", "General", "Host telemetry and lookups")

CURRENT = "weather-1__get_current_weather"
DAILY = "weather-1__get_daily_forecast"
STATUS = "general-1__get_system_status"


def _request(**overrides: object) -> RoutingRequest:
    kwargs: dict = {
        "current_request": "what is the weather in Lexington?",
        "agents": [WEATHER, GENERAL],
        "tools_by_agent": {
            "weather-1": [
                ToolOption(CURRENT, "Current conditions"),
                ToolOption(DAILY, "Seven day forecast"),
            ],
            "general-1": [ToolOption(STATUS, "Host health")],
        },
    }
    kwargs.update(overrides)
    return RoutingRequest.build(**kwargs)  # type: ignore[arg-type]


def _tools_desc(*names: str) -> list[dict]:
    return [{"type": "function", "function": {"name": name}} for name in names]


# -- request bounds (FR-038) ---------------------------------------------


def test_the_current_request_is_truncated() -> None:
    request = _request(current_request="x" * (ROUTING_MAX_REQUEST_CHARS + 500))
    assert len(request.current_request) <= ROUTING_MAX_REQUEST_CHARS
    assert request.current_request.endswith("…")


def test_history_is_bounded_in_count_and_length() -> None:
    history = [
        {"role": "user", "content": "m" * 5000} for _ in range(20)
    ]
    request = _request(history=history)
    assert len(request.recent_conversation) == ROUTING_MAX_HISTORY_MESSAGES
    assert all(len(turn.text) <= 601 for turn in request.recent_conversation)


def test_history_keeps_only_user_and_assistant_text() -> None:
    history = [
        {"role": "system", "content": "you are a helpful assistant"},
        {"role": "tool", "content": "{...tool output...}"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": None},
    ]
    request = _request(history=history)
    assert [turn.role for turn in request.recent_conversation] == ["user"]


def test_the_state_payload_carries_only_bounded_fields() -> None:
    state = _request(selected_tools=[CURRENT]).state()
    assert set(state) == {
        "current_request",
        "recent_conversation",
        "active_agent",
        "user_selected_tools",
    }


def test_the_request_holds_no_attachment_or_tool_output_field() -> None:
    """FR-038 is a shape guarantee, not a filtering step."""
    fields = set(RoutingRequest.__dataclass_fields__)
    for forbidden in ("attachments", "tool_outputs", "files", "memory", "guidance"):
        assert forbidden not in fields


def test_agents_are_truncated_with_the_active_agent_kept_first() -> None:
    agents = [AgentOption(f"a{i}", f"Agent {i}") for i in range(MAX_ROUTING_AGENTS + 10)]
    tools = {agent.agent_id: [ToolOption(f"{agent.agent_id}__t")] for agent in agents}
    request = RoutingRequest.build(
        current_request="hi",
        agents=agents,
        tools_by_agent=tools,
        active_agent="a25",
    )
    kept = [agent.agent_id for agent in request.agents]
    assert len(kept) == MAX_ROUTING_AGENTS
    assert kept[0] == "a25"


def test_tools_are_truncated_with_selected_tools_kept_first() -> None:
    names = [f"weather-1__t{i}" for i in range(MAX_TOOLS_PER_AGENT + 5)]
    request = RoutingRequest.build(
        current_request="hi",
        agents=[WEATHER],
        tools_by_agent={"weather-1": [ToolOption(name) for name in names]},
        selected_tools=[names[-1]],
    )
    kept = [tool.name for tool in request.tools_by_agent["weather-1"]]
    assert len(kept) == MAX_TOOLS_PER_AGENT
    assert kept[0] == names[-1]


def test_an_agent_with_no_eligible_tools_is_dropped() -> None:
    request = RoutingRequest.build(
        current_request="hi",
        agents=[WEATHER, GENERAL],
        tools_by_agent={"weather-1": [ToolOption(CURRENT)], "general-1": []},
    )
    assert [agent.agent_id for agent in request.agents] == ["weather-1"]


def test_an_empty_catalog_is_recognized() -> None:
    assert RoutingRequest.build(current_request="hi").is_empty is True
    assert _request().is_empty is False


# -- question construction -----------------------------------------------


def test_the_question_set_has_the_security_core_plus_one_question_per_agent() -> None:
    question_set = build_questions(_request(), SDK)
    ids = set(question_set.questions)
    assert set(SECURITY_QUESTION_IDS) <= ids
    assert QUESTION_TARGET_AGENT in ids
    assert QUESTION_PRESENTATION_STYLE in ids
    tool_questions = {i for i in ids if i.startswith(TOOL_QUESTION_PREFIX)}
    assert len(tool_questions) == 2


def test_the_agent_question_offers_no_tool_needed() -> None:
    question_set = build_questions(_request(), SDK)
    criteria = question_set.questions[QUESTION_TARGET_AGENT].criteria
    assert NO_TOOL_NEEDED in criteria
    assert set(criteria) - {NO_TOOL_NEEDED} == {"weather-1", "general-1"}


def test_each_tool_question_offers_none_fit_and_only_that_agents_tools() -> None:
    question_set = build_questions(_request(), SDK)
    question_id = question_set.tool_question_id("weather-1")
    criteria = question_set.questions[question_id].criteria
    assert NONE_FIT in criteria
    assert set(criteria) - {NONE_FIT} == {CURRENT, DAILY}
    assert STATUS not in criteria


def test_tool_options_use_prefixed_names_so_collisions_are_distinguishable() -> None:
    """Two agents can expose the same unqualified verb."""
    request = RoutingRequest.build(
        current_request="run the thing",
        agents=[WEATHER, GENERAL],
        tools_by_agent={
            "weather-1": [ToolOption("weather-1__status")],
            "general-1": [ToolOption("general-1__status")],
        },
    )
    question_set = build_questions(request, SDK)
    weather_options = set(
        question_set.questions[question_set.tool_question_id("weather-1")].criteria
    )
    general_options = set(
        question_set.questions[question_set.tool_question_id("general-1")].criteria
    )
    assert "weather-1__status" in weather_options
    assert "weather-1__status" not in general_options


def test_the_style_question_offers_every_declared_style() -> None:
    question_set = build_questions(_request(), SDK)
    assert set(question_set.questions[QUESTION_PRESENTATION_STYLE].criteria) == set(
        PRESENTATION_STYLES
    )


def test_a_security_only_question_set_asks_nothing_else() -> None:
    question_set = build_security_question_set(SDK)
    assert set(question_set.questions) == set(SECURITY_QUESTION_IDS)
    assert question_set.tool_names == {}


def test_no_question_text_contains_the_users_request() -> None:
    secret = "my social security number is 000-00-0000"
    question_set = build_questions(_request(current_request=secret), SDK)
    rendered = repr(question_set.questions)
    assert secret not in rendered


# -- response parsing ----------------------------------------------------


def _response(
    *,
    agent: str | None = "weather-1",
    agent_confidence: float = 0.94,
    tool: str | None = CURRENT,
    tool_confidence: float = 0.91,
    tool_probabilities: dict | None = None,
    style: str = "dashboard",
    jailbreak: float = 0.0,
    harm: float = 0.0,
    threat: str = "none",
    question_set=None,
) -> FakeResponse:
    choices = {
        QUESTION_THREAT_CATEGORY: ChoiceAnswer(threat, 1.0, {threat: 1.0}),
        QUESTION_PRESENTATION_STYLE: ChoiceAnswer(style, 1.0, {style: 1.0}),
    }
    if agent is not None:
        choices[QUESTION_TARGET_AGENT] = ChoiceAnswer(
            agent, agent_confidence, {agent: agent_confidence}
        )
    if tool is not None and question_set is not None and agent:
        question_id = question_set.tool_question_id(agent)
        if question_id:
            choices[question_id] = ChoiceAnswer(
                tool,
                tool_confidence,
                tool_probabilities or {tool: tool_confidence, NONE_FIT: 0.02},
            )
    return FakeResponse(
        nouls={QUESTION_IS_JAILBREAK: NoulAnswer(jailbreak)},
        scores={QUESTION_HARM_SCORE: ScoreAnswer(harm)},
        choices=choices,
    )


def test_a_confident_answer_is_the_high_tier() -> None:
    question_set = build_questions(_request(), SDK)
    decision = parse_decision(_response(question_set=question_set), question_set)
    assert decision.tier is Tier.HIGH
    assert decision.agent_id == "weather-1"
    assert decision.tool_name == CURRENT
    assert decision.shortlist == (CURRENT,)
    assert decision.style == "dashboard"


def test_a_spread_answer_is_the_medium_tier() -> None:
    question_set = build_questions(_request(), SDK)
    response = _response(
        agent_confidence=0.70,
        tool_confidence=0.45,
        tool_probabilities={CURRENT: 0.45, DAILY: 0.40, NONE_FIT: 0.15},
        question_set=question_set,
    )
    decision = parse_decision(response, question_set)
    assert decision.tier is Tier.MEDIUM
    assert set(decision.shortlist) == {CURRENT, DAILY}


def test_a_low_agent_confidence_is_the_low_tier() -> None:
    question_set = build_questions(_request(), SDK)
    response = _response(agent_confidence=0.30, question_set=question_set)
    decision = parse_decision(response, question_set)
    assert decision.tier is Tier.LOW
    assert decision.narrows_round_one is False


def test_no_tool_needed_is_the_low_tier_and_is_recorded() -> None:
    question_set = build_questions(_request(), SDK)
    response = _response(agent=NO_TOOL_NEEDED, tool=None, question_set=question_set)
    decision = parse_decision(response, question_set)
    assert decision.tier is Tier.LOW
    assert decision.no_tool_needed is True


def test_none_fit_is_the_low_tier() -> None:
    question_set = build_questions(_request(), SDK)
    response = _response(tool=NONE_FIT, question_set=question_set)
    decision = parse_decision(response, question_set)
    assert decision.tier is Tier.LOW
    assert decision.tool_name is None


def test_an_invented_agent_is_the_low_tier() -> None:
    question_set = build_questions(_request(), SDK)
    response = _response(agent="agent-that-does-not-exist", question_set=question_set)
    assert parse_decision(response, question_set).tier is Tier.LOW


def test_a_tool_outside_the_eligible_set_is_the_low_tier() -> None:
    question_set = build_questions(_request(), SDK)
    response = _response(tool="weather-1__secret_admin_tool", question_set=question_set)
    decision = parse_decision(response, question_set)
    assert decision.tier is Tier.LOW
    assert decision.tool_name is None


def test_a_missing_answer_is_the_low_tier() -> None:
    question_set = build_questions(_request(), SDK)
    assert parse_decision(FakeResponse(), question_set).tier is Tier.LOW


def test_an_unknown_style_falls_back_to_as_delivered() -> None:
    question_set = build_questions(_request(), SDK)
    response = _response(style="interpretive_dance", question_set=question_set)
    assert parse_decision(response, question_set).style == "as_delivered"


def test_parsing_never_raises_on_a_malformed_response() -> None:
    question_set = build_questions(_request(), SDK)
    for malformed in (FakeResponse(), object(), None):
        decision = parse_decision(malformed, question_set)
        assert decision.tier is Tier.LOW


def test_a_decision_carries_no_request_text() -> None:
    question_set = build_questions(_request(current_request="a secret phrase"), SDK)
    decision = parse_decision(_response(question_set=question_set), question_set)
    assert "secret phrase" not in repr(decision)


# -- security parsing ----------------------------------------------------


def test_security_answers_are_read() -> None:
    judgment = parse_security(_response(jailbreak=0.82, harm=2.7, threat="destructive"))
    assert judgment.jailbreak_probability == pytest.approx(0.82)
    assert judgment.harm_score == pytest.approx(2.7)
    assert judgment.threat_category == "destructive"


def test_an_unknown_threat_category_defaults_to_none() -> None:
    assert parse_security(_response(threat="vibes")).threat_category == "none"


def test_missing_security_answers_default_to_benign() -> None:
    judgment = parse_security(FakeResponse())
    assert judgment.jailbreak_probability == 0.0
    assert judgment.harm_score == 0.0
    assert judgment.threat_category == "none"


# -- round one -----------------------------------------------------------


def test_no_decision_leaves_round_one_untouched() -> None:
    tools = _tools_desc(CURRENT, DAILY, STATUS)
    plan = apply_round_one(None, tools)
    assert plan.tools_desc is tools
    assert plan.tool_choice == "auto"
    assert plan.narrowed is False


def test_a_low_tier_decision_leaves_round_one_untouched() -> None:
    tools = _tools_desc(CURRENT, DAILY, STATUS)
    plan = apply_round_one(RoutingDecision(tier=Tier.LOW), tools)
    assert plan.tools_desc is tools
    assert plan.tool_choice == "auto"


def test_the_high_tier_sends_one_tool_and_forces_it_where_supported() -> None:
    tools = _tools_desc(CURRENT, DAILY, STATUS)
    decision = RoutingDecision(
        tier=Tier.HIGH, agent_id="weather-1", tool_name=CURRENT, shortlist=(CURRENT,)
    )
    plan = apply_round_one(decision, tools, "openai")
    assert [t["function"]["name"] for t in plan.tools_desc] == [CURRENT]
    assert plan.tool_choice == {"type": "function", "function": {"name": CURRENT}}
    assert plan.narrowed is True


@pytest.mark.parametrize("preset", ["custom", "ollama", "lmstudio", "", None])
def test_unsupported_presets_get_auto_instead_of_a_forced_choice(preset) -> None:
    tools = _tools_desc(CURRENT, DAILY)
    decision = RoutingDecision(
        tier=Tier.HIGH, agent_id="weather-1", tool_name=CURRENT, shortlist=(CURRENT,)
    )
    plan = apply_round_one(decision, tools, preset)
    assert plan.tool_choice == "auto"
    assert [t["function"]["name"] for t in plan.tools_desc] == [CURRENT]


@pytest.mark.parametrize("preset", sorted(FORCED_CHOICE_PROVIDERS))
def test_every_allowlisted_preset_forces_the_choice(preset: str) -> None:
    tools = _tools_desc(CURRENT)
    decision = RoutingDecision(
        tier=Tier.HIGH, agent_id="weather-1", tool_name=CURRENT, shortlist=(CURRENT,)
    )
    assert isinstance(apply_round_one(decision, tools, preset).tool_choice, dict)


def test_the_medium_tier_sends_a_shortlist_with_auto() -> None:
    tools = _tools_desc(CURRENT, DAILY, STATUS)
    decision = RoutingDecision(
        tier=Tier.MEDIUM, agent_id="weather-1", shortlist=(CURRENT, DAILY)
    )
    plan = apply_round_one(decision, tools, "openai")
    assert [t["function"]["name"] for t in plan.tools_desc] == [CURRENT, DAILY]
    assert plan.tool_choice == "auto"


def test_round_one_is_always_a_subset_of_the_eligible_list() -> None:
    tools = _tools_desc(CURRENT, STATUS)
    decision = RoutingDecision(
        tier=Tier.MEDIUM, agent_id="weather-1", shortlist=(CURRENT, DAILY)
    )
    plan = apply_round_one(decision, tools)
    names = {t["function"]["name"] for t in plan.tools_desc}
    assert names <= {CURRENT, STATUS}


def test_a_shortlist_that_matches_nothing_falls_back_to_the_full_list() -> None:
    tools = _tools_desc(STATUS)
    decision = RoutingDecision(
        tier=Tier.HIGH, agent_id="weather-1", tool_name=CURRENT, shortlist=(CURRENT,)
    )
    plan = apply_round_one(decision, tools, "openai")
    assert plan.tools_desc is tools
    assert plan.tool_choice == "auto"
    assert plan.narrowed is False


def test_an_empty_tool_list_is_returned_unchanged() -> None:
    decision = RoutingDecision(tier=Tier.HIGH, tool_name=CURRENT, shortlist=(CURRENT,))
    plan = apply_round_one(decision, [])
    assert plan.tools_desc == []
    assert plan.tool_choice == "auto"


def test_apply_round_one_is_pure() -> None:
    tools = _tools_desc(CURRENT, DAILY)
    before = [dict(entry) for entry in tools]
    decision = RoutingDecision(
        tier=Tier.HIGH, agent_id="weather-1", tool_name=CURRENT, shortlist=(CURRENT,)
    )
    apply_round_one(decision, tools, "openai")
    assert tools == before


def test_the_plan_is_immutable() -> None:
    plan = RoundOnePlan(tools_desc=[], tool_choice="auto")
    with pytest.raises((AttributeError, TypeError)):
        plan.tool_choice = "forced"  # type: ignore[misc]
