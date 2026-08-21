"""Feature 052 concurrency probe (SC-011): 20 simultaneous agents-surface opens.

Drives ``chrome_open`` for the agents surface through the real
``Orchestrator`` + ``chrome_events`` dispatch against the live database
(one ``VirtualWebSocket`` per open, mirroring the in-process harness in
``orchestrator/async_tasks.py``), first as sequential singles and then as
N=20 simultaneous opens on one event loop, and asserts
P95(concurrent) <= max(2 x P95(sequential), an absolute floor) so slow or
noisy CI machines don't flake and sub-5ms sequential baselines don't make
the 2x bound meaningless.

Not collected by the default ``test_*.py`` glob — run explicitly:
``pytest tests/perf/concurrent_surfaces.py -q`` or
``python -m tests.perf.concurrent_surfaces`` from ``backend/``.
Set ``ASTRAL_SKIP_PERF=1`` to skip; ``PERF_CONCURRENT_FLOOR_MS`` (default
250) tunes the absolute floor; ``PERF_CONCURRENT_OPENS`` (default 20) the
concurrency level.
"""
from __future__ import annotations

import asyncio
import math
import os
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

pytestmark = [
    pytest.mark.perf,
    pytest.mark.skipif(os.getenv("ASTRAL_SKIP_PERF") == "1",
                       reason="ASTRAL_SKIP_PERF=1"),
]

N_CONCURRENT = int(os.getenv("PERF_CONCURRENT_OPENS", "20"))
FLOOR_MS = float(os.getenv("PERF_CONCURRENT_FLOOR_MS", "250"))
MIN_MEANINGFUL_SEQ_P95_MS = 5.0
_ERROR_MARKERS = ("This surface failed to load", "Unknown settings surface",
                  "Something went wrong", "Not authorized")


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile of a non-empty sample."""
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[rank - 1]


def _fresh_socket(user_id: str):
    """A VirtualWebSocket capturing one open's frames, per the async harness."""
    from orchestrator.async_tasks import BackgroundTask, VirtualWebSocket
    task = BackgroundTask(task_id=uuid.uuid4().hex, chat_id="", user_id=user_id)
    return VirtualWebSocket(task)


def _seed(orch, user_id: str, email: str, agent_ids: list[str]) -> None:
    """Seed the probe user and a few owned agent cards so the render has rows."""
    from shared.protocol import AgentCard, AgentSkill

    # The probe subject is itself email-shaped, so the agents surface's
    # documented mock-auth fallback can derive the owner address without
    # manufacturing a durable identity-provider observation.  Everything we
    # do persist below has an exact typed cleanup path.
    assert user_id == email and "@" in user_id
    # Feature 054: an unconfigured user's chrome_open is refused server-side
    # (first-run gate) — seed the probe user's persisted LLM config so the
    # probe measures the agents surface, not the setup dialog.
    orch._llm_store.set_sync(user_id, provider="custom",
                             base_url="http://test.invalid/v1",
                             model="test-model", api_key="test-key")
    for i, agent_id in enumerate(agent_ids):
        orch.agent_cards[agent_id] = AgentCard(
            name=f"Perf Probe Agent {i}",
            description="Synthetic agent seeded by the SC-011 concurrency probe.",
            agent_id=agent_id,
            skills=[AgentSkill(
                name=f"probe_tool_{i}", description="probe tool",
                id=f"probe_tool_{i}", input_schema={"type": "object"})],
        )
        orch.user_agent_registry.set_agent_ownership(
            agent_id,
            owner_email=email,
            is_public=False,
        )


def _cleanup(orch, user_id: str, agent_ids: list[str]) -> None:
    """Remove every exact row created by :func:`_seed`."""
    from orchestrator.plane_repository_context import (
        PlaneRepositoryContext,
        plane_source_from_orchestrator,
    )

    for agent_id in agent_ids:
        orch.agent_cards.pop(agent_id, None)
    source = plane_source_from_orchestrator(orch)
    ownership = PlaneRepositoryContext(
        repository=source.plane_repositories.agents,
        plane_runtime=source.plane_runtime,
    )
    with ownership.transaction() as transaction:
        for agent_id in agent_ids:
            ownership.repository.remove_ownership(
                transaction,
                agent_id=agent_id,
                owner_email=user_id,
            )
    orch._llm_store.clear_sync(user_id)


async def _open_once(orch, user_id: str, batch_t0: float | None = None) -> float:
    """One chrome_open of the agents surface; returns wall time in ms.

    For concurrent batches the latency each user experiences runs from the
    moment everyone clicked (``batch_t0``), not from when this coroutine
    happened to get scheduled — per-coroutine starts would hide any
    serialization caused by loop-blocking work.
    """
    from orchestrator import chrome_events
    ws = _fresh_socket(user_id)
    orch.ui_sessions[ws] = {"realm_access": {"roles": ["user"]}}
    try:
        started = batch_t0 if batch_t0 is not None else time.perf_counter()
        handled = await chrome_events.handle_chrome_event(
            orch, ws, "chrome_open",
            {"surface": "agents", "params": {"tab": "mine"}}, user_id)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
    finally:
        orch.ui_sessions.pop(ws, None)
    assert handled is True, "chrome_open was not handled"
    frames = [f for f in ws.task.outputs if f.get("type") == "chrome_render"]
    assert frames, f"no chrome_render frame (outputs: {[f.get('type') for f in ws.task.outputs]})"
    html = frames[-1].get("html", "")
    for marker in _ERROR_MARKERS:
        assert marker not in html, f"surface render failed: {marker!r} in modal"
    assert "astral-agent" in html or "Agents" in html, "unexpected modal body"
    return elapsed_ms


async def _run_probe(n: int = N_CONCURRENT) -> dict:
    """Sequential-singles baseline then n simultaneous opens; returns the timings."""
    from orchestrator.orchestrator import Orchestrator
    orch = await asyncio.to_thread(Orchestrator)
    user_id = f"perf-probe-{uuid.uuid4().hex[:8]}@perf.local"
    email = user_id
    agent_ids = [f"perf-probe-agent-{i}-{uuid.uuid4().hex[:6]}" for i in range(3)]
    try:
        await asyncio.to_thread(_seed, orch, user_id, email, agent_ids)
        for _ in range(3):
            await _open_once(orch, user_id)
        sequential = [await _open_once(orch, user_id) for _ in range(n)]
        batch_t0 = time.perf_counter()
        concurrent = list(await asyncio.gather(
            *(_open_once(orch, user_id, batch_t0=batch_t0) for _ in range(n))))
    finally:
        try:
            await asyncio.to_thread(_cleanup, orch, user_id, agent_ids)
        finally:
            await orch._close_started_services()
    return {
        "n": n,
        "seq_p95_ms": _percentile(sequential, 95.0),
        "seq_p50_ms": _percentile(sequential, 50.0),
        "conc_p95_ms": _percentile(concurrent, 95.0),
        "conc_p50_ms": _percentile(concurrent, 50.0),
    }


def _check(result: dict) -> str:
    """Apply the SC-011 bound with the absolute floor; returns a summary line."""
    seq_p95 = result["seq_p95_ms"]
    conc_p95 = result["conc_p95_ms"]
    threshold = max(2.0 * seq_p95, FLOOR_MS)
    summary = (
        f"n={result['n']} sequential p50/p95 = "
        f"{result['seq_p50_ms']:.1f}/{seq_p95:.1f} ms; concurrent p50/p95 = "
        f"{result['conc_p50_ms']:.1f}/{conc_p95:.1f} ms; "
        f"threshold = {threshold:.1f} ms (floor {FLOOR_MS:.0f} ms)"
    )
    if seq_p95 < MIN_MEANINGFUL_SEQ_P95_MS and conc_p95 <= FLOOR_MS:
        return summary + " [floor-only: sequential baseline under 5 ms]"
    assert conc_p95 <= threshold, f"SC-011 violated: {summary}"
    return summary


async def test_concurrent_agents_surface_opens_within_2x_single_user_p95():
    """SC-011: with 20 concurrent agents-surface opens, P95 stays within 2x singles."""
    try:
        result = await _run_probe()
    except Exception as exc:
        if type(exc).__name__ == "OperationalError":
            pytest.skip(f"database unavailable: {exc}")
        raise
    print(_check(result))


def main() -> int:
    """CLI entry: run the probe and print the timing summary."""
    result = asyncio.run(_run_probe())
    print(_check(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
