"""FR-051 contract test — the fixed, closed verb set of the remote-compute agent.

The read-only and mutating verbs were merged into ONE agent (remote-compute-1) but
still live in two risk-tiered libraries (``remote_observe``/``remote_control``) that
the agent unions. This asserts, against those live registries AND the unified
``remote_compute`` registry the agent actually exposes, the exact verb set, per-verb
scope, required argument keys, enum members, destructive classification, retry
posture, and declared timeout — so adding a verb, widening an argument,
reclassifying a destructive operation, or flipping a retry posture cannot pass
unnoticed (specs/063 contracts/verbs.md).

Stdlib-only; no DB, no network — pure registry inspection.
"""
from agents.remote_compute.mcp_tools import TOOL_REGISTRY as REG
from agents.remote_control.mcp_tools import TOOL_REGISTRY as CTL
from agents.remote_observe.mcp_tools import TOOL_REGISTRY as OBS
from orchestrator.remote_confirmation import DESTRUCTIVE_CLASSIFICATION

READ_VERBS = {
    "list_machines", "probe_machine", "list_queue", "job_status",
    "job_history", "host_facts", "list_directory", "list_processes",
    "read_job_output",
}

MUTATING_VERBS = {
    "submit_job", "make_directory", "upload_file", "cancel_job",
    "remove_path", "control_service", "manage_package", "signal_process",
    "run_job",
}

# The authoritative scope + destructive table (contracts/verbs.md §mutating).
CTL_SCOPE = {
    "run_job": "tools:write", "submit_job": "tools:write", "make_directory": "tools:write",
    "upload_file": "tools:write", "cancel_job": "tools:write",
    "remove_path": "tools:write", "control_service": "tools:system",
    "manage_package": "tools:system", "signal_process": "tools:system",
}

CTL_DESTRUCTIVE = {
    "run_job": "never",
    "submit_job": "never",
    "make_directory": "never",
    "upload_file": "if_exists",
    "cancel_job": "always",
    "remove_path": "always",
    "control_service": {"by_action": ["stop", "disable", "restart"]},
    "manage_package": {"by_action": ["remove"]},
    "signal_process": "always",
}

CTL_REQUIRED = {
    "run_job": {"machine_id", "script"},
    "submit_job": {"machine_id", "script_path"},
    "make_directory": {"machine_id", "path"},
    "upload_file": {"machine_id", "attachment_id", "remote_path"},
    "cancel_job": {"machine_id", "job_id"},
    "remove_path": {"machine_id", "path"},
    "control_service": {"machine_id", "service_name", "action"},
    "manage_package": {"machine_id", "package_name", "action"},
    "signal_process": {"machine_id", "pid", "signal"},
}

READ_REQUIRED = {
    "list_machines": set(),
    "probe_machine": {"machine_id"},
    "list_queue": {"machine_id"},
    "job_status": {"machine_id", "job_id"},
    "job_history": {"machine_id"},
    "host_facts": {"machine_id"},
    "list_directory": {"machine_id", "path"},
    "list_processes": {"machine_id"},
    "read_job_output": {"machine_id"},
}


# ── exact verb sets (contract #1) ──────────────────────────────────────────────

def test_read_agent_verb_set_is_exactly_the_eight():
    assert set(OBS) == READ_VERBS


def test_control_agent_verb_set_is_exactly_the_eight():
    assert set(CTL) == MUTATING_VERBS


# ── scope (contract #2) ─────────────────────────────────────────────────────────

def test_read_verbs_all_scope_read():
    assert all(v["scope"] == "tools:read" for v in OBS.values())


def test_control_verb_scopes_match_table():
    assert {name: v["scope"] for name, v in CTL.items()} == CTL_SCOPE


# ── argument schema (contract #3) ───────────────────────────────────────────────

def test_read_required_args_match():
    assert {n: set(v["input_schema"].get("required", [])) for n, v in OBS.items()} == READ_REQUIRED


def test_control_required_args_match():
    assert {n: set(v["input_schema"].get("required", [])) for n, v in CTL.items()} == CTL_REQUIRED


def test_control_enum_members_are_the_closed_sets():
    props = lambda verb: CTL[verb]["input_schema"]["properties"]
    assert props("control_service")["action"]["enum"] == ["start", "stop", "restart", "enable", "disable"]
    assert props("manage_package")["action"]["enum"] == ["install", "remove"]
    assert props("signal_process")["signal"]["enum"] == ["TERM", "KILL"]


# ── destructive classification (contract #4) ────────────────────────────────────

def test_control_destructive_classification_matches_declared_map():
    assert {name: v["destructive"] for name, v in CTL.items()} == CTL_DESTRUCTIVE


def test_registry_destructive_is_the_same_object_as_the_gate_map():
    # FR-028: verb + classification cannot drift — the registry stamps the SAME
    # object the confirmation gate reads.
    for name, v in CTL.items():
        assert v["destructive"] is DESTRUCTIVE_CLASSIFICATION[name]


def test_gate_classification_map_covers_exactly_the_mutating_verbs():
    assert set(DESTRUCTIVE_CLASSIFICATION) == MUTATING_VERBS


# ── retry posture (contract #5) ─────────────────────────────────────────────────

def test_every_control_verb_is_non_retryable():
    assert all(v["retryable"] is False for v in CTL.values())


def test_read_verbs_declare_retryable_true():
    assert all(v.get("retryable") is True for v in OBS.values())


# ── timeout declared and > 0 (contract #6) ──────────────────────────────────────

def test_every_verb_declares_a_positive_timeout():
    for name, v in {**OBS, **CTL}.items():
        assert isinstance(v.get("timeout"), (int, float)) and v["timeout"] > 0, name


# ── the unified agent exposes exactly the union (the merge, FR-024/FR-025) ──────

def test_unified_registry_is_exactly_the_full_verb_set():
    assert set(REG) == (READ_VERBS | MUTATING_VERBS)
    assert len(REG) == 18


def test_unified_registry_is_the_union_of_the_two_risk_tiers():
    # The merged agent unions the two libraries by REFERENCE — the same entry
    # dicts — so no metadata (scope/destructive/etc.) can diverge between what the
    # contract asserts per tier and what the agent actually serves.
    for name, entry in {**OBS, **CTL}.items():
        assert REG[name] is entry


def test_unified_registry_destructive_still_matches_the_gate_map():
    for name in MUTATING_VERBS:
        assert REG[name]["destructive"] is DESTRUCTIVE_CLASSIFICATION[name]
