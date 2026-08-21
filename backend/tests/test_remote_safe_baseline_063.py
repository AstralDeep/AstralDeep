"""Feature 063 US2 — the no-grant permission posture of remote-compute-1 (T030, SC-003).

A signed-in user with NO agent_scopes rows runs EVERY read verb under the
safe-seed baseline, and no mutating verb can produce a destructive effect: the
unified-agent reconciliation (merge ac6ed97) chose the durable confirmation
gate — not the permission baseline — as the control that stops destruction, so
this suite proves both halves where each actually lives:

- ``is_tool_allowed``: every ``tools:read`` verb allowed with zero scope rows,
  and ONLY because of the safe+public flip (denied when either leg is absent;
  an explicit opt-out row still wins);
- ``remote_confirmation.evaluate``: every destructive verb is refused on first
  reach with zero transport effects, and the classification map covers the
  whole mutating registry so no mutating verb can drift past the gate.

Permission decisions run against an isolated current AstralPlane database;
confirmation decisions retain the hermetic proposal store and FakeTransport.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agents.remote_compute import mcp_tools as unified
from agents.remote_control import mcp_tools as ctl
from agents.remote_observe import mcp_tools as obs
from orchestrator import remote_confirmation as rc
from orchestrator.remote_transport import FakeTransport, MachineTarget, set_transport
from orchestrator.tool_permissions import ToolPermissionManager
from tests.helpers.remote_plane_runtime import make_remote_confirmation_plane_source
from tests.helpers.voice_plane_runtime import isolated_plane_runtime

AGENT = "remote-compute-1"
USER = "fresh-user-1"

READ_VERBS = sorted(obs.TOOL_REGISTRY)
MUTATING_VERBS = sorted(ctl.TOOL_REGISTRY)


def _pm(plane_runtime, *, safe=True, public=True):
    with plane_runtime.transaction() as transaction:
        plane_runtime.repositories.agents.upsert_ownership(
            transaction,
            agent_id=AGENT,
            owner_email="remote-baseline@example.test",
            is_public=public,
            observed_at=1,
        )
        plane_runtime.repositories.agents.set_trust(
            transaction,
            agent_id=AGENT,
            is_safe=safe,
            marked_by="pytest",
        )
    pm = ToolPermissionManager(
        plane_runtime=plane_runtime,
        plane_repositories=plane_runtime.repositories,
    )
    # The REAL registered scope map (read tier tools:read; mutating tier
    # tools:write / tools:system) — what registration wires in production.
    pm.register_tool_scopes(AGENT, {v: e["scope"] for v, e in unified.TOOL_REGISTRY.items()})
    return pm


@pytest.fixture(autouse=True)
def _no_audit(monkeypatch):
    # The confirmation gate audits best-effort; keep this suite storage-free.
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: None)
    yield
    set_transport(None)


@pytest.fixture
def plane_runtime(monkeypatch):
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "safe_agents", True)
    with isolated_plane_runtime("remote_baseline") as runtime:
        yield runtime


# ── seed source + registry shape ──────────────────────────────────────────────

def test_remote_compute_is_in_the_boot_seed_source():
    from orchestrator.local_agents import FIRST_PARTY_PUBLIC_AGENT_IDS

    # The boot safe-seed draws from this catalog (filtered by FF_REMOTE_COMPUTE
    # in orchestrator.start) — the unified agent must be in it to be seeded.
    assert AGENT in FIRST_PARTY_PUBLIC_AGENT_IDS


def test_registry_tiers_are_disjoint_and_fully_classified():
    assert set(READ_VERBS).isdisjoint(MUTATING_VERBS)
    assert set(unified.TOOL_REGISTRY) == set(READ_VERBS) | set(MUTATING_VERBS)
    for verb in READ_VERBS:
        assert unified.TOOL_REGISTRY[verb]["scope"] == "tools:read"
        # A read verb has NO destructive classification → never gated (FR-028).
        assert rc.classification_for(verb) is None
    # EVERY mutating verb carries a classification — an unclassified mutating
    # verb would sail past evaluate(), so this equality is the no-drift pin.
    assert set(MUTATING_VERBS) == set(rc.DESTRUCTIVE_CLASSIFICATION)
    for verb in MUTATING_VERBS:
        assert unified.TOOL_REGISTRY[verb]["scope"] in ("tools:write", "tools:system")


# ── read verbs under the safe-seed baseline (is_tool_allowed) ─────────────────

def test_no_grant_user_runs_every_read_verb_under_the_safe_baseline(plane_runtime):
    pm = _pm(plane_runtime)
    for verb in READ_VERBS:
        assert pm.is_tool_allowed(USER, AGENT, verb) is True, verb


def test_read_verbs_denied_without_the_safe_marker(plane_runtime):
    pm = _pm(plane_runtime, safe=False)
    for verb in READ_VERBS:
        assert pm.is_tool_allowed(USER, AGENT, verb) is False, verb


def test_read_verbs_denied_when_the_agent_is_private(plane_runtime):
    # The flip is withheld for a non-public agent (040 anti-fleet-exposure).
    pm = _pm(plane_runtime, public=False)
    for verb in READ_VERBS:
        assert pm.is_tool_allowed(USER, AGENT, verb) is False, verb


def test_explicit_optout_beats_the_safe_baseline(plane_runtime):
    pm = _pm(plane_runtime)
    pm.set_agent_scopes(USER, AGENT, {"tools:read": False})
    for verb in READ_VERBS:
        assert pm.is_tool_allowed(USER, AGENT, verb) is False, verb


def test_mutating_scopes_also_ride_the_safe_baseline(plane_runtime):
    # Reconciled posture pin (merge ac6ed97): the unified agent is safe-seeded,
    # so the PERMISSION layer alone does not distinguish the mutating tier —
    # SC-003's "zero mutating tasks" is enforced by the per-verb confirmation
    # gate below, not here. If the baseline is ever narrowed per-tier, update
    # this pin deliberately.
    pm = _pm(plane_runtime)
    for verb in MUTATING_VERBS:
        assert pm.is_tool_allowed(USER, AGENT, verb) is True, verb


# ── destructive verbs at the confirmation gate (zero effects) ─────────────────

class _ProposalDB:
    def __init__(self):
        self.rows = {}


def _orch(db):
    source = make_remote_confirmation_plane_source(db)
    return SimpleNamespace(
        history=SimpleNamespace(db=db),
        plane_repository_source=source,
        runtime_composition=SimpleNamespace(
            plane=SimpleNamespace(
                runtime=source.plane_runtime,
                repositories=source.plane_repositories,
            )
        ),
        credential_manager=object(),
        ui_sessions={},
    )


_DESTRUCTIVE_CALLS = {
    "remove_path": {"machine_id": "m1", "path": "/data", "recursive": True},
    "cancel_job": {"machine_id": "m1", "job_id": "9"},
    "signal_process": {"machine_id": "m1", "pid": "42", "signal": "KILL"},
    "control_service": {"machine_id": "m1", "service_name": "nginx", "action": "stop"},
    "manage_package": {"machine_id": "m1", "package_name": "vim", "action": "remove"},
    "upload_file": {"machine_id": "m1", "attachment_id": "a1", "remote_path": "/exists.txt"},
}


def test_every_destructive_verb_is_refused_on_first_reach_with_no_effect(monkeypatch):
    tgt = MachineTarget(machine_id="m1", label="dgx", address="10.0.0.5", port=22,
                        username="me", cred_type="password", secret="x")
    monkeypatch.setattr("orchestrator.remote_machines.build_target", lambda *a, **k: tgt)
    for verb, args in _DESTRUCTIVE_CALLS.items():
        db = _ProposalDB()
        t = FakeTransport(files={"/exists.txt": b"x"})  # upload target EXISTS -> destructive
        set_transport(t)
        out = rc.evaluate(_orch(db), object(), AGENT, verb, dict(args), "chat", USER)
        assert out is not None and "confirmation_required" in out[0], verb
        # a proposal was recorded for the caller, and NOTHING executed: the
        # only transport traffic allowed is the read-only if_exists stat.
        assert len(db.rows) == 1
        assert next(iter(db.rows.values()))["verb"] == verb
        assert [c["op"] for c in t.calls if c["op"] != "stat"] == [], verb


def test_read_and_nondestructive_verbs_pass_the_gate_untouched():
    db = _ProposalDB()
    o = _orch(db)
    for verb in READ_VERBS:
        assert rc.evaluate(o, object(), AGENT, verb, {"machine_id": "m1"}, "chat", USER) is None
    for verb, args in (
        ("make_directory", {"machine_id": "m1", "path": "/tmp/x"}),
        ("submit_job", {"machine_id": "m1", "script_path": "/j.sbatch"}),
        ("run_job", {"machine_id": "m1", "script": "echo hi"}),
        ("control_service", {"machine_id": "m1", "service_name": "nginx", "action": "start"}),
        ("manage_package", {"machine_id": "m1", "package_name": "vim", "action": "install"}),
    ):
        assert rc.evaluate(o, object(), AGENT, verb, args, "chat", USER) is None, verb
    assert db.rows == {}
