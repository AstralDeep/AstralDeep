"""T076 — every verb of BOTH remote registries maps every driven failure onto a
contracts/result-vocabulary verdict that names the machine and a next action, with
zero generic/empty error strings (SC-011/FR-035).

The sweeps iterate the TOOL_REGISTRYs themselves and coverage guards pin the
argument tables to them, so a future verb cannot dodge this file: it fails
collection-side until it declares its valid and malformed argument cases.

Transport seam + monkeypatched machine resolution; no DB / SSH / network. Verbs
are called directly (post-confirmation state, as in test_remote_control_verbs).
"""
from __future__ import annotations

import pytest

from agents.remote_control import mcp_tools as ctl
from agents.remote_observe import mcp_tools as obs
from orchestrator.credential_manager import CredentialNotConfigured
from orchestrator.remote_transport import FakeTransport, MachineTarget, Verdict, set_transport

USER = "user-1"

# The fixed vocabulary (contracts/result-vocabulary.md); OK is the one success verdict.
VOCABULARY = {v.value for v in Verdict} - {Verdict.OK.value}

_GENERIC_FRAGMENTS = ("something went wrong", "unknown error", "unexpected error",
                      "internal error", "an error occurred")

_MODULES = {"observe": obs, "control": ctl}
ALL_KEYS = ([f"observe:{n}" for n in sorted(obs.TOOL_REGISTRY)]
            + [f"control:{n}" for n in sorted(ctl.TOOL_REGISTRY)])

# list_machines reads only the caller's inventory — it never reaches the transport,
# so its one failure mode is the missing-principal sweep below.
NO_TRANSPORT = {"observe:list_machines"}
TRANSPORT_KEYS = [k for k in ALL_KEYS if k not in NO_TRANSPORT]
MACHINE_KEYS = TRANSPORT_KEYS  # every transport verb resolves a machine ref first

# Arguments that pass every shape guard, so the driven failure is the one under test.
# read_job_output uses an explicit output_path so no tracked-job store is touched.
VALID_ARGS = {
    "observe:list_machines": {},
    "observe:probe_machine": {"machine_id": "dgx"},
    "observe:list_queue": {"machine_id": "dgx"},
    "observe:host_facts": {"machine_id": "dgx"},
    "observe:job_status": {"machine_id": "dgx", "job_id": "42"},
    "observe:job_history": {"machine_id": "dgx"},
    "observe:list_directory": {"machine_id": "dgx", "path": "/home/me"},
    "observe:list_processes": {"machine_id": "dgx"},
    "observe:read_job_output": {"machine_id": "dgx", "output_path": "/abs/out.log"},
    "control:run_job": {"machine_id": "dgx", "script": "nvidia-smi"},
    "control:submit_job": {"machine_id": "dgx", "script_path": "/home/me/run.sbatch"},
    "control:make_directory": {"machine_id": "dgx", "path": "/home/me/x"},
    "control:upload_file": {"machine_id": "dgx", "attachment_id": "a1",
                            "remote_path": "/home/me/f.bin"},
    "control:cancel_job": {"machine_id": "dgx", "job_id": "42"},
    "control:remove_path": {"machine_id": "dgx", "path": "/home/me/x"},
    "control:control_service": {"machine_id": "dgx", "service_name": "nginx", "action": "start"},
    "control:manage_package": {"machine_id": "dgx", "package_name": "htop", "action": "install"},
    "control:signal_process": {"machine_id": "dgx", "pid": "123", "signal": "TERM"},
}

# One malformed-argument case per verb (list_machines takes no arguments). The
# guard must refuse BEFORE any transport traffic. read_job_output maps a relative
# path to not_found ("no tracked output…") — a vocabulary verdict, by design.
MALFORMED_ARGS = {
    "observe:probe_machine": ({}, Verdict.INVALID_ARGUMENT.value),
    "observe:list_queue": ({}, Verdict.INVALID_ARGUMENT.value),
    "observe:host_facts": ({}, Verdict.INVALID_ARGUMENT.value),
    "observe:job_status": ({"machine_id": "dgx", "job_id": "42; rm -rf /"},
                           Verdict.INVALID_ARGUMENT.value),
    "observe:job_history": ({}, Verdict.INVALID_ARGUMENT.value),
    "observe:list_directory": ({"machine_id": "dgx", "path": "../../etc"},
                               Verdict.INVALID_ARGUMENT.value),
    "observe:list_processes": ({}, Verdict.INVALID_ARGUMENT.value),
    "observe:read_job_output": ({"machine_id": "dgx", "output_path": "relative/out.log"},
                                Verdict.NOT_FOUND.value),
    "control:run_job": ({"machine_id": "dgx", "script": "   "},
                        Verdict.INVALID_ARGUMENT.value),
    "control:submit_job": ({"machine_id": "dgx", "script_path": "run.sbatch"},
                           Verdict.INVALID_ARGUMENT.value),
    "control:make_directory": ({"machine_id": "dgx", "path": "x/y"},
                               Verdict.INVALID_ARGUMENT.value),
    "control:upload_file": ({"machine_id": "dgx", "attachment_id": "a1",
                             "remote_path": "dest.bin"}, Verdict.INVALID_ARGUMENT.value),
    "control:cancel_job": ({"machine_id": "dgx", "job_id": "$(reboot)"},
                           Verdict.INVALID_ARGUMENT.value),
    "control:remove_path": ({"machine_id": "dgx", "path": "*"},
                            Verdict.INVALID_ARGUMENT.value),
    "control:control_service": ({"machine_id": "dgx", "service_name": "nginx",
                                 "action": "obliterate"}, Verdict.INVALID_ARGUMENT.value),
    "control:manage_package": ({"machine_id": "dgx", "package_name": "vim && curl evil",
                                "action": "install"}, Verdict.INVALID_ARGUMENT.value),
    "control:signal_process": ({"machine_id": "dgx", "pid": "1", "signal": "HUP"},
                               Verdict.INVALID_ARGUMENT.value),
}

# Control verbs where the remote RAN the command and refused it (non-zero exit).
# upload_file has no exit path — its transfer failures are the transport sweeps.
REMOTE_REJECTION = sorted(k for k in ALL_KEYS
                          if k.startswith("control:") and k != "control:upload_file")
# manage_package's `which` probe must find a manager for the rejection to land on
# the mutating command itself.
_REJECTION_STDOUT = {"control:manage_package": "/usr/bin/apt-get"}


def _target():
    return MachineTarget(machine_id="m1", label="dgx", address="10.0.0.5", port=22,
                         username="me", cred_type="password", secret="x")


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    obs.register_deps(object(), object())
    ctl.register_deps(object(), object())
    monkeypatch.setattr("orchestrator.remote_machines.resolve_machine",
                        lambda db, uid, ref: {"machine_id": "m1", "label": "dgx"})
    monkeypatch.setattr("orchestrator.remote_machines.build_target",
                        lambda db, cm, uid, mid: _target())
    yield
    set_transport(None)


def _fake(**kw):
    t = FakeTransport(**kw)
    set_transport(t)
    return t


def _fn(key):
    mod, name = key.split(":", 1)
    return _MODULES[mod].TOOL_REGISTRY[name]["function"]


def _setup(key, monkeypatch, tmp_path):
    # upload_file resolves the attachment before it touches the transport.
    if key == "control:upload_file":
        from types import SimpleNamespace
        blob = tmp_path / "payload.bin"
        blob.write_bytes(b"abc")
        monkeypatch.setattr("orchestrator.attachments.repository.AttachmentRepository.get_by_id",
                            lambda self, aid, uid: SimpleNamespace(filename="payload.bin",
                                                                   size_bytes=3))
        monkeypatch.setattr("orchestrator.attachments.store.read_path",
                            lambda uid, aid, fn: blob)


def _assert_vocab_failure(key, res, expected=None):
    data = res.get("_data") or {}
    verdict = data.get("verdict")
    assert verdict in VOCABULARY, f"{key}: verdict {verdict!r} is not in the result vocabulary"
    if expected is not None:
        assert verdict == expected, f"{key}: expected {expected!r}, got {verdict!r}"
    assert (data.get("machine") or "").strip(), f"{key}: failure does not name a machine"
    next_action = (data.get("next_action") or "").strip()
    assert next_action, f"{key}: failure carries no next_action"
    low = next_action.lower()
    assert not any(g in low for g in _GENERIC_FRAGMENTS), \
        f"{key}: generic error text {next_action!r}"
    # The rendered Alert is built from the same typed fields — never a raw traceback.
    alert = str(res.get("_ui_components"))
    assert verdict in alert and "Traceback" not in alert


# ── coverage guards: a future verb cannot dodge these sweeps ──────────────────

def test_valid_args_cover_every_registry_verb():
    assert set(VALID_ARGS) == set(ALL_KEYS), \
        "every registry verb must declare valid args so the failure sweeps can drive it"


def test_malformed_args_cover_every_verb_with_arguments():
    assert set(MALFORMED_ARGS) == set(ALL_KEYS) - {"observe:list_machines"}, \
        "every argument-taking verb must declare a malformed-argument case"


def test_registries_have_no_colliding_verb_names():
    assert not set(obs.TOOL_REGISTRY) & set(ctl.TOOL_REGISTRY)


# ── missing principal: the ONE failure every verb shares (incl. list_machines) ─

@pytest.mark.parametrize("key", ALL_KEYS)
def test_missing_principal_is_refused_with_vocabulary_verdict(key):
    t = _fake()
    res = _fn(key)(**VALID_ARGS[key])  # no user_id on the call
    _assert_vocab_failure(key, res, Verdict.UNATTENDED_REFUSED.value)
    assert t.calls == [], f"{key}: refusal must precede any transport traffic"


# ── transport failures ────────────────────────────────────────────────────────

@pytest.mark.parametrize("key", TRANSPORT_KEYS)
def test_unreachable_transport_maps_to_unreachable(key, monkeypatch, tmp_path):
    _setup(key, monkeypatch, tmp_path)
    _fake(reachable=False)
    res = _fn(key)(user_id=USER, **VALID_ARGS[key])
    _assert_vocab_failure(key, res, Verdict.UNREACHABLE.value)


@pytest.mark.parametrize("key", TRANSPORT_KEYS)
def test_auth_failure_maps_to_auth_failed(key, monkeypatch, tmp_path):
    _setup(key, monkeypatch, tmp_path)
    _fake(authenticated=False)
    res = _fn(key)(user_id=USER, **VALID_ARGS[key])
    _assert_vocab_failure(key, res, Verdict.AUTH_FAILED.value)


# ── inventory / credential failures ───────────────────────────────────────────

@pytest.mark.parametrize("key", MACHINE_KEYS)
def test_unknown_machine_maps_to_not_found(key, monkeypatch):
    monkeypatch.setattr("orchestrator.remote_machines.resolve_machine",
                        lambda db, uid, ref: None)
    t = _fake()
    res = _fn(key)(user_id=USER, **VALID_ARGS[key])
    _assert_vocab_failure(key, res, Verdict.NOT_FOUND.value)
    assert t.calls == []


@pytest.mark.parametrize("key", MACHINE_KEYS)
def test_missing_credential_maps_to_credential_not_configured(key, monkeypatch):
    def _raise(db, cm, uid, mid):
        raise CredentialNotConfigured("m1")
    monkeypatch.setattr("orchestrator.remote_machines.build_target", _raise)
    t = _fake()
    res = _fn(key)(user_id=USER, **VALID_ARGS[key])
    _assert_vocab_failure(key, res, Verdict.CREDENTIAL_NOT_CONFIGURED.value)
    assert t.calls == []


# ── malformed arguments: refused before any transport traffic ─────────────────

@pytest.mark.parametrize("key", sorted(MALFORMED_ARGS))
def test_malformed_args_map_to_vocabulary_without_touching_transport(key):
    kwargs, expected = MALFORMED_ARGS[key]
    t = _fake()
    res = _fn(key)(user_id=USER, **kwargs)
    _assert_vocab_failure(key, res, expected)
    assert t.calls == [], f"{key}: a bad argument must never reach the transport"


# ── remote rejection: the command RAN and the machine refused it ──────────────

@pytest.mark.parametrize("key", REMOTE_REJECTION)
def test_remote_rejection_is_surfaced_not_swallowed(key):
    _fake(command_exit=1, command_stderr="sbatch: error: Permission denied",
          command_stdout=_REJECTION_STDOUT.get(key, ""))
    res = _fn(key)(user_id=USER, **VALID_ARGS[key])
    _assert_vocab_failure(key, res, Verdict.PARTIAL.value)
    # the actionable stderr tail reaches the user, not a generic message
    assert "Permission denied" in res["_data"]["next_action"]
