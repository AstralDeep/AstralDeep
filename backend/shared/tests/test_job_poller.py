"""Tests for shared/job_poller.JobPoller: intermediate/terminal phase emission,
failure-threshold handling and recovery, and cancellation.
"""

import asyncio
import json

import pytest

from shared.job_poller import JobPoller


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send_text(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


@pytest.fixture
def make_poller():
    def _factory(poll_fn, *, poll_interval=0.0, failure_threshold=5, cap_job_id="cap-job-1"):
        ws = _FakeWS()
        return JobPoller(
            ws=ws,
            request_id="req-42",
            agent_id="classify-1",
            tool_name="train_classifier",
            cap_job_id=cap_job_id,
            poll_fn=poll_fn,
            poll_interval=poll_interval,
            failure_threshold=failure_threshold,
        ), ws
    return _factory


@pytest.mark.asyncio
async def test_emits_intermediate_then_terminal(make_poller) -> None:
    states = iter([
        {"status": "in_progress", "percentage": 25, "message": "Training step 1"},
        {"status": "in_progress", "percentage": 50, "message": "Training step 2"},
        {"status": "succeeded", "percentage": 100, "message": "Done.",
         "result": {"accuracy": 0.92}},
    ])
    poller, ws = make_poller(lambda: next(states))
    await asyncio.wait_for(poller.run(), timeout=2.0)
    phases = [m["metadata"]["phase"] for m in ws.sent]
    assert phases == ["training", "training", "completed"]
    assert ws.sent[-1]["metadata"]["terminal"] is True
    assert ws.sent[-1]["metadata"]["result"] == {"accuracy": 0.92}
    assert ws.sent[-1]["metadata"]["cap_job_id"] == "cap-job-1"
    assert ws.sent[-1]["metadata"]["request_id"] == "req-42"


@pytest.mark.asyncio
async def test_terminal_failed_emits_failed_phase(make_poller) -> None:
    poller, ws = make_poller(lambda: {"status": "failed", "message": "bad CSV"})
    await asyncio.wait_for(poller.run(), timeout=1.0)
    assert len(ws.sent) == 1
    assert ws.sent[0]["metadata"]["phase"] == "failed"
    assert ws.sent[0]["metadata"]["terminal"] is True
    assert ws.sent[0]["message"] == "bad CSV"


@pytest.mark.asyncio
async def test_five_consecutive_failures_emit_status_unknown(make_poller) -> None:
    def _always_fail():
        raise ConnectionError("upstream gone")

    poller, ws = make_poller(_always_fail, failure_threshold=5)
    await asyncio.wait_for(poller.run(), timeout=1.0)
    assert len(ws.sent) == 1
    assert ws.sent[0]["metadata"]["phase"] == "status_unknown"
    assert ws.sent[0]["metadata"]["terminal"] is True
    assert "try again later" in ws.sent[0]["message"].lower()


@pytest.mark.asyncio
async def test_failures_below_threshold_recover(make_poller) -> None:
    calls = {"n": 0}
    def _flaky():
        calls["n"] += 1
        if calls["n"] <= 3:
            raise ConnectionError("transient")
        return {"status": "succeeded", "result": {"ok": True}}

    poller, ws = make_poller(_flaky, failure_threshold=5)
    await asyncio.wait_for(poller.run(), timeout=1.0)
    phases = [m["metadata"]["phase"] for m in ws.sent]
    assert phases == ["completed"]


@pytest.mark.asyncio
async def test_cancellation_emits_status_unknown(make_poller) -> None:
    async def _never_terminal():
        return {"status": "in_progress", "message": "still going"}

    poller, ws = make_poller(lambda: {"status": "in_progress", "message": "still going"})
    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert any(m["metadata"]["phase"] == "status_unknown" for m in ws.sent)


@pytest.mark.asyncio
async def test_no_cap_job_id_omits_metadata_field(make_poller) -> None:
    poller, ws = make_poller(
        lambda: {"status": "succeeded", "result": {"x": 1}},
        cap_job_id=None,
    )
    await asyncio.wait_for(poller.run(), timeout=1.0)
    assert "cap_job_id" not in ws.sent[0]["metadata"]


@pytest.mark.asyncio
async def test_non_dict_poll_result_treated_as_in_progress(make_poller) -> None:
    states = iter([
        "garbage non-dict",
        {"status": "succeeded", "message": "done"},
    ])
    poller, ws = make_poller(lambda: next(states))
    await asyncio.wait_for(poller.run(), timeout=1.0)
    phases = [m["metadata"]["phase"] for m in ws.sent]
    assert phases[-1] == "completed"
