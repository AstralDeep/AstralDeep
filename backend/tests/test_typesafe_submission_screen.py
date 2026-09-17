"""Feature 089 (T026): the HTTP Work ``kind="chat"`` submission screen.

This path hands an instruction to a background executor with nobody watching
it. It never reaches ``handle_chat_message``, so it gets none of the turn
seams — which is why it has a seam of its own (I7), and why that seam asks the
three security questions and nothing else.

The properties that matter here are the ones a background path makes easy to
get wrong:

* it asks the security questions **only** — no agent, no tool, no layout, so
  the owner's quota is not spent on answers nobody will read;
* a ``confirm_tools`` verdict cannot be satisfied when there is nobody to
  confirm, so it is a pass and the executor's own gate stack still runs;
* a refusal is the only verdict that stops admission, and it stops it before
  anything is recorded;
* every failure — no key, the feature off, a dead service, an open circuit —
  admits the work. A screen that cannot reach its service must not become a
  way to block work.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from orchestrator.typesafe_routing.budget import UserCircuit  # noqa: E402
from orchestrator.typesafe_routing.questions import (  # noqa: E402
    QUESTION_HARM_SCORE, QUESTION_IS_JAILBREAK, QUESTION_THREAT_CATEGORY,
)
from orchestrator.typesafe_routing.runner import screen_instruction  # noqa: E402
from orchestrator.typesafe_routing.security_policy import Verdict  # noqa: E402
from tests.fakes.typesafe_fake import (  # noqa: E402
    AnswerSet, FakeTypeSafeClient, TypeSafeAPIConnectionError, TypeSafeAPITimeoutError,
    high_confidence,
)

KEY = "ts_submission_screen_key_for_tests_only_0000"
FP = "abcdef123456"
TEXT = "Summarise the weekly research digest and file it."


class _EnabledFlags:
    @staticmethod
    def is_enabled(_name: str) -> bool:
        return True


def screen(client, *, text: str = TEXT, api_key: str | None = KEY, circuit=None):
    # A fresh breaker per call unless the test supplies one: the module-level
    # default is shared, so a test that injects failures would otherwise leave
    # the circuit open for whatever ran next.
    return asyncio.run(
        screen_instruction(
            user_id="owner-1",
            text=text,
            api_key=api_key,
            fingerprint=FP,
            client=client,
            circuit=circuit if circuit is not None else UserCircuit(),
            flags=_EnabledFlags,
        )
    )


def benign() -> AnswerSet:
    return AnswerSet(threat_category="none")


# -- it asks the security questions and nothing else ----------------------


def test_only_the_three_security_questions_are_asked() -> None:
    client = FakeTypeSafeClient(default=benign())
    assert screen(client) is Verdict.PASS
    assert client.call_count == 1
    asked = set(client.calls[0].questions)
    assert asked == {QUESTION_IS_JAILBREAK, QUESTION_HARM_SCORE, QUESTION_THREAT_CATEGORY}


def test_no_agent_tool_or_layout_question_reaches_the_service() -> None:
    """Paying for the routing half here would spend the owner's quota on
    answers nobody reads: there is no round one and no canvas."""
    client = FakeTypeSafeClient(default=high_confidence("weather-1", "get_current_weather"))
    screen(client)
    asked = " ".join(client.calls[0].questions)
    for absent in ("target_agent", "tool_for", "response_style", "none_fit"):
        assert absent not in asked


def test_only_the_instruction_is_sent() -> None:
    client = FakeTypeSafeClient(default=benign())
    screen(client)
    state = client.calls[0].state
    assert set(state) == {"current_request"}
    assert state["current_request"] == TEXT


def test_a_long_instruction_is_bounded_before_it_leaves() -> None:
    client = FakeTypeSafeClient(default=benign())
    screen(client, text="x" * 40_000)
    sent = client.calls[0].state["current_request"]
    assert len(sent) < 40_000


# -- the verdicts ---------------------------------------------------------


def test_a_benign_instruction_is_admitted() -> None:
    assert screen(FakeTypeSafeClient(default=benign())) is Verdict.PASS


def test_a_confirm_verdict_is_a_pass_because_nobody_is_there_to_confirm() -> None:
    """A background submission has no one to ask. Treating "confirm" as a
    block would refuse work on a verdict that never said to refuse it; the
    executor's own gate stack is unchanged and still runs."""
    client = FakeTypeSafeClient(
        default=benign().with_security(jailbreak_probability=0.80, harm_score=0.40)
    )
    verdict = screen(client)
    assert verdict is not Verdict.REFUSE


def test_the_verdict_comes_from_the_same_policy_as_an_interactive_turn() -> None:
    """Not a second, laxer policy for background work."""
    from orchestrator.typesafe_routing.decision import parse_security
    from orchestrator.typesafe_routing.security_policy import verdict_for
    from tests.fakes.typesafe_fake import _response_for  # noqa: PLC2701

    answers = benign().with_security(jailbreak_probability=0.80, harm_score=0.40)
    client = FakeTypeSafeClient(default=answers)
    observed = screen(client)
    rendered = _response_for(answers, client.calls[0].questions)
    assert observed is verdict_for(parse_security(rendered))


# -- every failure admits the work ----------------------------------------


def test_no_key_means_no_call_and_no_block() -> None:
    client = FakeTypeSafeClient(default=benign())
    assert screen(client, api_key=None) is Verdict.PASS
    assert client.call_count == 0


def test_empty_text_makes_no_call() -> None:
    client = FakeTypeSafeClient(default=benign())
    assert screen(client, text="") is Verdict.PASS
    assert client.call_count == 0


def test_the_feature_flag_being_off_makes_no_call() -> None:
    class Off:
        @staticmethod
        def is_enabled(_name: str) -> bool:
            return False

    client = FakeTypeSafeClient(default=benign())
    verdict = asyncio.run(
        screen_instruction(user_id="o", text=TEXT, api_key=KEY, client=client, flags=Off)
    )
    assert verdict is Verdict.PASS
    assert client.call_count == 0


@pytest.mark.parametrize("fault", [TypeSafeAPITimeoutError, TypeSafeAPIConnectionError, RuntimeError])
def test_a_service_failure_admits_the_work(fault) -> None:
    client = FakeTypeSafeClient(default=benign(), faults=[fault])
    assert screen(client) is Verdict.PASS


def test_enough_service_failures_open_the_circuit() -> None:
    """The screen shares the turn path's breaker, so a background submission
    that keeps failing stops paying for the round trip too."""
    circuit = UserCircuit()
    client = FakeTypeSafeClient(
        default=benign(), faults=[TypeSafeAPITimeoutError] * 6
    )
    for _ in range(6):
        assert screen(client, circuit=circuit) is Verdict.PASS
    assert not circuit.allows("owner-1", fingerprint=FP)


def test_an_open_circuit_skips_the_call_entirely() -> None:
    circuit = UserCircuit()
    for _ in range(10):
        circuit.record_failure("owner-1")
    client = FakeTypeSafeClient(default=benign())
    assert screen(client, circuit=circuit) is Verdict.PASS
    assert client.call_count == 0


def test_a_success_keeps_the_circuit_closed() -> None:
    circuit = UserCircuit()
    client = FakeTypeSafeClient(default=benign())
    screen(client, circuit=circuit)
    assert circuit.allows("owner-1", fingerprint=FP)


# -- the key never leaves its argument ------------------------------------


def test_the_key_is_passed_only_as_the_call_argument() -> None:
    client = FakeTypeSafeClient(default=benign())
    screen(client)
    call = client.calls[0]
    assert call.api_key == KEY
    assert KEY not in repr(call)
    assert KEY not in str(call.state) and KEY not in str(call.questions)


def test_the_screen_never_puts_the_key_in_the_environment() -> None:
    before = dict(os.environ)
    screen(FakeTypeSafeClient(default=benign()))
    assert dict(os.environ) == before
    for name in ("TYPESAFE_API_KEY", "TYPESAFE_BASE_URL", "TYPESAFE_DEFAULT_MODEL"):
        assert name not in os.environ


# -- background turns still receive the routing notices -------------------


def test_a_virtual_websocket_accepts_a_routing_notice() -> None:
    """Background turns run over ``VirtualWebSocket``; a notice sent to one
    must not raise, because a dead or synthetic socket is not an error on the
    notice path."""
    from orchestrator.typesafe_routing.runner import NOTICE_FALLBACK, _notify_once

    delivered: list[tuple[str, str]] = []

    async def notifier(status: str, message: str) -> None:
        delivered.append((status, message))

    asyncio.run(_notify_once(notifier, "fallback", NOTICE_FALLBACK))
    assert delivered == [("fallback", NOTICE_FALLBACK)]


def test_a_dead_socket_does_not_break_the_turn() -> None:
    from orchestrator.typesafe_routing.runner import NOTICE_RETRYING, _notify_once

    async def dead(_status: str, _message: str) -> None:
        raise RuntimeError("socket closed")

    asyncio.run(_notify_once(dead, "retrying", NOTICE_RETRYING))  # must not raise
