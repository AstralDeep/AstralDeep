"""Exercises approval decisions in hitl_confirmation.py and remote_confirmation.py: decision frames can carry a
guidance read, and a withdrawn mode or lost authority cache never replays a hydrated review.
"""

import asyncio

import pytest

from orchestrator import hitl_confirmation as hc
from orchestrator import remote_confirmation as rc
from tests.test_computer_grant_flow_102 import (
    OWNER, ROOT, _call, _fresh_pending_cards as _fresh_pending_cards, _mac, _payload,
)
from tests.test_hitl_public_research import runtime as runtime  # noqa: F401
from tests.test_hitl_task_continuation import task_runtime as task_runtime  # noqa: F401
from tests.test_lets_gate_ordering import gate_orchestrator as gate_orchestrator  # noqa: F401


async def _settle():
    for _ in range(20):
        await asyncio.sleep(0)


def test_decision_frames_can_carry_a_guidance_read(monkeypatch):
    from orchestrator import human_request_authority as hra

    built = []

    class Request:
        def __init__(self, boundary, **kwargs):
            built.append(kwargs)

    monkeypatch.setattr(hra, "_HumanSocketRequest", Request)
    for action, method in (("authorize_action", "WS_READ"), ("remote_op_decision", "WS_READ"),
                           ("chat_message", "WS_READ"), ("chrome_open", None)):
        request = hra.capture_human_socket_request(object(), websocket=object(), context=object(),
            message={"type": "ui_event", "action": action, "payload": {}}, purpose="skill_lookup")
        assert (request is not None) is (method is not None)
    assert [kwargs["method"] for kwargs in built] == ["WS_READ"] * 3
    assert all(kwargs["purpose"] == "skill_lookup" for kwargs in built)


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("cache_loss", [False, True])
async def test_mode_withdrawal_or_authority_cache_loss_never_replays_hydrated_review(
        task_runtime, monkeypatch, remote, cache_loss):
    from orchestrator.approval_receipts import withdraw_computer_reviews
    orch, ws, effects, *_ = task_runtime
    session, host = await _mac(task_runtime, remote=remote)
    card = (await _call(task_runtime, "run_command", argv=["wc", "notes.md"], cwd=ROOT) if remote else
            await _call(task_runtime, "type_text", text="hello", hwnd=42))
    payload = _payload(card, "approve")
    assert card.ui_components[0]["computer_host_id"] == host.host_id
    async def presentation(*args, **kwargs):
        pass
    monkeypatch.setattr(hc, "_finish_card", presentation)
    monkeypatch.setattr(rc, "_replace_card", presentation)
    if cache_loss:
        orch._hitl_pending_calls = {}
        orch._computer_approval_authorities = {}
    else:
        await withdraw_computer_reviews(orch, OWNER, host.host_id)
    await orch.computer_sessions.set_bypass(host, True)
    await orch.computer_sessions.set_bypass(host, False)
    handler = rc.handle_decision if remote else hc.handle_decision
    await handler(orch, ws, OWNER, payload)
    await handler(orch, ws, OWNER, payload)
    await _settle()
    assert not effects
    await orch.computer_sessions.end(session, "user_stop")


