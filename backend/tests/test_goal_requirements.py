"""Tests strict goal contracts and proof validation against scoped successful observations."""

import pytest

from orchestrator.goal_requirements import (
    PROOF_CATEGORIES,
    GoalContract,
    build_goal_contract,
    parse_goal_contract,
    validate_goal_proofs,
)


ROOT = "/Users/sam/Work/project"
PROJECT = {
    "project_id": "project-a",
    "computer_id": "computer-a",
    "computer_name": "Sam's Mac",
    "workspace_roots": [{"path": ROOT}],
    "primary_root": ROOT,
    "folder_status": "granted",
}


def _call(call_id, tool, *, agent="computer-use-1", args=None, data=None,
          outcome="success", completed=True, computer_id="computer-a",
          computer_name="Sam's Mac", project_id="project-a", sequence=1):
    return {
        "call_id": call_id,
        "tool": tool,
        "agent_id": agent,
        "arguments": args or {},
        "data": {"computer": computer_name, **(data or {})},
        "outcome": outcome,
        "completed": completed,
        "computer_id": computer_id,
        "computer_name": computer_name,
        "project_id": project_id,
        "sequence": sequence,
    }


def _run(call_id, *, argv=None, command=None, cwd=ROOT, data=None, **kwargs):
    args = {"cwd": cwd}
    if argv is not None:
        args["argv"] = argv
    if command is not None:
        args["command"] = command
    return _call(call_id, "run_command", args=args,
                 data=data or {"cwd": cwd, "computer": "Sam's Mac", "exit_code": 0,
                               "timed_out": False, "signal": None}, **kwargs)


def _local_contract():
    return build_goal_contract("local_project", PROJECT)


def test_strict_goal_contract_parser_accepts_only_known_intents():
    assert parse_goal_contract('{"kind":"informational"}') == GoalContract("informational", ())
    assert parse_goal_contract('{"kind":"action"}') == GoalContract("action", ("outcome",))
    classified = parse_goal_contract('{"kind":"local_project"}')
    assert classified == GoalContract("local_project", PROOF_CATEGORIES)
    assert build_goal_contract(classified.kind, PROJECT) == GoalContract(
        "local_project", PROOF_CATEGORIES)


@pytest.mark.parametrize("kind", [None, 1, "unknown"])
def test_goal_contract_builder_rejects_unrecognized_intent(kind):
    with pytest.raises(ValueError):
        build_goal_contract(kind)


@pytest.mark.parametrize("text", [
    '```json\n{"kind":"informational"}\n```',
    '{"kind":"other"}',
    '{"kind":"local_project","categories":[]}',
    '{"kind":"action","reason":"because"}',
    '[{"kind":"action"}]',
    '{"kind":"action"} trailing',
    '{"kind":"action"' + ' ' * 4096 + '}',
])
def test_goal_contract_parser_rejects_noncanonical_or_oversized_input(text):
    with pytest.raises(ValueError):
        parse_goal_contract(text)


def test_local_project_contract_fails_closed_without_current_granted_root():
    contract = parse_goal_contract('{"kind":"local_project"}')
    for context in (None, {}, {**PROJECT, "folder_status": "folder_grant_required"},
                    {**PROJECT, "workspace_roots": []}, {**PROJECT, "computer_id": None}):
        bound = build_goal_contract(contract.kind, context)
        assert bound.blocked_reason
        assert not validate_goal_proofs(bound, {}, [], context).accepted


def test_granted_contract_fails_if_project_context_is_stale_or_revoked():
    contract = build_goal_contract("local_project", PROJECT)
    revoked = {**PROJECT, "folder_status": "authority_unavailable"}
    assert not validate_goal_proofs(contract, {}, [], revoked).accepted


@pytest.mark.parametrize("contract", [
    GoalContract("other", ()),
    GoalContract("informational", ("outcome",)),
    GoalContract("action", ()),
    GoalContract("local_project", ()),
    GoalContract("action", ("outcome",), 1),
])
def test_proof_validator_rejects_manually_malformed_contracts(contract):
    assert not validate_goal_proofs(contract, {}, [], PROJECT).accepted


def test_informational_completion_does_not_require_tool_evidence():
    contract = parse_goal_contract('{"kind":"informational"}')
    assert validate_goal_proofs(contract, {}, [], None).accepted
    assert not validate_goal_proofs(contract, {"files": ["canvas"]}, [], None).accepted
    searched = _call("search", "search", agent="web-search", data={"items": ["source"]})
    assert validate_goal_proofs(contract, {"outcome": ["search"]}, [searched], None).accepted
    assert not validate_goal_proofs(contract, {"outcome": ["missing"]}, [searched], None).accepted
    assert not validate_goal_proofs(contract, {"outcome": ["search", "fabricated"]}, [searched], None).accepted


def test_general_action_requires_a_real_completed_success():
    contract = parse_goal_contract('{"kind":"action"}')
    assert not validate_goal_proofs(contract, {}, [], None).accepted
    failed = _call("f", "write_file", outcome="failure", completed=False)
    assert not validate_goal_proofs(contract, {"outcome": ["f"]}, [failed], None).accepted
    success = _call("ok", "search", data={"items": ["fact"]})
    result = validate_goal_proofs(contract, {"outcome": ["ok"]}, [success], None)
    assert result.accepted
    assert not validate_goal_proofs(
        contract, {"outcome": ["ok", "fabricated"]}, [success], None).accepted


@pytest.mark.parametrize("observations", ["not records", 42, [None],
    [{"call_id": "duplicate"}, {"call_id": "duplicate"}],
    [{"call_id": str(index)} for index in range(257)]])
def test_action_proofs_reject_malformed_or_unbounded_observations(observations):
    contract = parse_goal_contract('{"kind":"action"}')
    assert not validate_goal_proofs(contract, {"outcome": ["ok"]}, observations, None).accepted


@pytest.mark.parametrize("references", [None, [], {"other": ["ok"]}, {"outcome": "ok"},
    {"outcome": ["ok", "ok"]}, {"outcome": ["x" * 129]},
    {"outcome": [str(index) for index in range(9)]}])
def test_action_proofs_reject_malformed_and_bounded_reference_violations(references):
    contract = parse_goal_contract('{"kind":"action"}')
    assert not validate_goal_proofs(contract, references, [], None).accepted


def test_local_project_requires_every_category_and_actual_scoped_successes():
    contract = _local_contract()
    command = _run("cmd", argv=["python", "-m", "pytest"])
    all_refs = {category: ["cmd"] for category in PROOF_CATEGORIES}
    file_call = _call("file", "write_file", args={"path": ROOT + "/main.py"},
                      data={"path": ROOT + "/main.py", "bytes": 10})
    all_refs["files"] = ["file"]
    assert validate_goal_proofs(contract, all_refs, [command, file_call], PROJECT).accepted
    for category in PROOF_CATEGORIES:
        missing = dict(all_refs)
        del missing[category]
        result = validate_goal_proofs(contract, missing, [command, file_call], PROJECT)
        assert not result.accepted
        assert category in result.missing


def test_each_project_category_rejects_mixed_real_and_fabricated_references():
    contract = _local_contract()
    file_call = _call("file", "write_file", args={"path": ROOT + "/main.py"},
                      data={"path": ROOT + "/main.py", "bytes": 10})
    command = _run("cmd", argv=["python", "-m", "pytest"])
    calls = [file_call, command]
    for category in PROOF_CATEGORIES:
        refs = {"files": ["file"], **{name: ["cmd"] for name in PROOF_CATEGORIES[1:]}}
        refs[category] = [refs[category][0], "fabricated"]
        result = validate_goal_proofs(contract, refs, calls, PROJECT)
        assert not result.accepted
        assert category in result.invalid


def test_project_file_inspection_cannot_substitute_for_execution_categories():
    contract = _local_contract()
    file_call = _call("file", "read_file", args={"path": ROOT + "/pyproject.toml"}, data={
        "path": ROOT + "/pyproject.toml", "size": 10, "truncated": False})
    command = _run("cmd", argv=["python", "-m", "pytest"])
    refs = {category: ["cmd"] for category in PROOF_CATEGORIES}
    for category in PROOF_CATEGORIES[1:]:
        only_file = dict(refs)
        only_file[category] = ["file"]
        result = validate_goal_proofs(contract, only_file, [file_call, command], PROJECT)
        assert not result.accepted
        assert category in result.invalid


@pytest.mark.parametrize("call", [
    _run("canvas", argv=["python", "-m", "pytest"], agent="canvas"),
    _run("wrong-root", argv=["python", "-m", "pytest"], cwd="/tmp/other"),
    _run("wrong-host", argv=["python", "-m", "pytest"], computer_id="other"),
    _run("wrong-computer", argv=["python", "-m", "pytest",],
         data={"cwd": ROOT, "computer": "Other Mac", "exit_code": 0, "timed_out": False}),
    _run("failed", argv=["python", "-m", "pytest"], data={
        "cwd": ROOT, "computer": "Sam's Mac", "exit_code": 1, "timed_out": False}),
    _run("timeout", argv=["python", "-m", "pytest"], data={
        "cwd": ROOT, "computer": "Sam's Mac", "exit_code": 0, "timed_out": True}),
    _run("background", argv=["python", "-m", "pytest"], data={
        "cwd": ROOT, "computer": "Sam's Mac", "job_id": "j", "background": True},
         completed=False),
    _run("noop", argv=["echo", "5 passed"]),
    _run("true", argv=["true"]),
    _run("env-noop", argv=["env", "true"]),
    _run("shell-noop", argv=["bash", "-c", "true"]),
    _run("no-agent", argv=["python", "-m", "pytest"], agent=""),
])
def test_invalid_execution_records_cannot_prove_local_categories(call):
    refs = {category: [call["call_id"]] for category in PROOF_CATEGORIES}
    file_call = _call("file", "write_file", args={"path": ROOT + "/main.py"},
                      data={"path": ROOT + "/main.py", "bytes": 10})
    refs["files"] = ["file"]
    assert not validate_goal_proofs(_local_contract(), refs, [call, file_call], PROJECT).accepted


def test_local_proofs_reject_wrong_tool_agent_or_missing_dispatch_identity():
    refs = {category: ["call"] for category in PROOF_CATEGORIES}
    for call in (
        _call("call", "search", args={"query": "project"}, data={"items": ["done"]}),
        _run("call", argv=["python", "-m", "pytest"], agent="another-agent"),
        _run("call", argv=["python", "-m", "pytest"], computer_id=None),
        _run("call", argv=["python", "-m", "pytest"], computer_name=None),
        _run("call", argv=["python", "-m", "pytest"], project_id="other-project"),
    ):
        assert not validate_goal_proofs(_local_contract(), refs, [call], PROJECT).accepted


@pytest.mark.parametrize("command", ["echo '5 passed'", "printf '5 passed'", "true",
                                     "zsh -c 'echo 5 passed'", "echo hello && true", "env true",
                                     "command -- true", "zsh -c \"zsh -c 'zsh -c true'\""])
def test_obvious_noop_shell_commands_cannot_prove_local_categories(command):
    call = _run("noop", command=command)
    refs = {category: ["noop"] for category in PROOF_CATEGORIES}
    assert not validate_goal_proofs(_local_contract(), refs, [call], PROJECT).accepted


@pytest.mark.parametrize("command", ["pytest -q", "echo starting && pytest -q", "zsh -c 'pytest -q'"])
def test_meaningful_shell_command_can_be_referenced(command):
    call = _run("command", command=command)
    refs = {category: ["command"] for category in PROOF_CATEGORIES}
    assert validate_goal_proofs(_local_contract(), refs, [call], PROJECT).accepted


@pytest.mark.parametrize("tool,args,data", [
    ("write_file", {"path": ROOT + "/main.py"}, {"path": ROOT + "/main.py", "bytes": 10}),
    ("read_file", {"path": ROOT + "/pyproject.toml"},
     {"path": ROOT + "/pyproject.toml", "text": "[project]", "size": 9, "truncated": False}),
    ("list_dir", {"path": ROOT}, {"path": ROOT, "entries": [{"name": "main.py"}], "count": 1}),
])
def test_local_file_proof_requires_scoped_success_and_matching_path(tool, args, data):
    contract = _local_contract()
    call = _call("file", tool, args=args, data=data)
    command = _run("command", argv=["python", "-m", "pytest"])
    refs = {category: ["command"] for category in PROOF_CATEGORIES}
    refs["files"] = ["file"]
    assert validate_goal_proofs(contract, refs, [call, command], PROJECT).accepted
    bad = _call("file", tool, args=args, data={**data, "path": "/tmp/outside"})
    assert not validate_goal_proofs(contract, refs, [bad, command], PROJECT).accepted


def test_file_observation_accepts_privacy_safe_result_markers_without_content():
    contract = _local_contract()
    call = _call("read", "read_file", args={"path": ROOT + "/pyproject.toml"}, data={
        "path": ROOT + "/pyproject.toml", "size": 20, "truncated": False,
        "has_meaningful_content": True})
    command = _run("command", argv=["python", "-m", "pytest"])
    refs = {category: ["command"] for category in PROOF_CATEGORIES}
    refs["files"] = ["read"]
    assert validate_goal_proofs(contract, refs, [call, command], PROJECT).accepted


@pytest.mark.parametrize("tool,data", [
    ("read_file", {"path": ROOT + "/a", "size": 2, "truncated": True}),
    ("read_file", {"path": ROOT + "/a", "size": -1, "truncated": False}),
    ("list_dir", {"path": ROOT + "/a", "count": -1}),
    ("write_file", {"path": ROOT + "/a", "bytes": -1}),
])
def test_local_file_proofs_require_valid_privacy_safe_inspection_result(tool, data):
    call = _call("file", tool, args={"path": ROOT + "/a"}, data=data)
    refs = {category: ["file"] for category in PROOF_CATEGORIES}
    assert not validate_goal_proofs(_local_contract(), refs, [call], PROJECT).accepted


@pytest.mark.parametrize("path", ["../escape", ROOT + "/../outside", "/tmp/outside", ROOT + "/x/../../bad"])
def test_local_file_proof_rejects_noncanonical_or_outside_paths(path):
    call = _call("read_file", "read_file", args={"path": path}, data={
        "path": path, "text": "x", "size": 1, "truncated": False})
    refs = {category: ["read"] for category in PROOF_CATEGORIES}
    assert not validate_goal_proofs(_local_contract(), refs, [call], PROJECT).accepted


def test_background_execution_proof_requires_correlated_terminal_success():
    contract = _local_contract()
    start = _call("start", "run_command", args={"argv": ["python", "-m", "pytest"], "cwd": ROOT},
                  data={"job_id": "job-a", "background": True, "computer": "Sam's Mac"},
                  completed=False, sequence=1)
    poll = _call("poll", "read_output", args={"job_id": "job-a"},
                 data={"job_id": "job-a", "state": "exited", "exit_code": 0,
                       "computer": "Sam's Mac"}, sequence=2)
    file_call = _call("file", "write_file", args={"path": ROOT + "/main.py"},
                      data={"path": ROOT + "/main.py", "bytes": 10})
    refs = {category: ["poll"] for category in PROOF_CATEGORIES}
    refs["files"] = ["file"]
    assert validate_goal_proofs(contract, refs, [start, poll, file_call], PROJECT).accepted
    for bad_poll_data in (
        {"job_id": "job-b", "state": "exited", "exit_code": 0, "computer": "Sam's Mac"},
        {"job_id": "job-a", "state": "running", "exit_code": None, "computer": "Sam's Mac"},
        {"job_id": "job-a", "state": "exited", "exit_code": 1, "computer": "Sam's Mac"},
    ):
        bad = _call("poll", "read_output", args={"job_id": "job-a"}, data=bad_poll_data)
        assert not validate_goal_proofs(contract, refs, [start, bad, file_call], PROJECT).accepted


def test_background_poll_cannot_prove_without_matching_real_origin_call():
    poll = _call("poll", "read_output", args={"job_id": "job-a"},
                 data={"job_id": "job-a", "state": "exited", "exit_code": 0}, sequence=2)
    file_call = _call("file", "write_file", args={"path": ROOT + "/main.py"},
                      data={"path": ROOT + "/main.py", "bytes": 10})
    refs = {category: ["poll"] for category in PROOF_CATEGORIES}
    refs["files"] = ["file"]
    assert not validate_goal_proofs(_local_contract(), refs, [poll, file_call], PROJECT).accepted


def test_background_poll_requires_origin_from_same_agent_host_and_project_root():
    contract = _local_contract()
    poll = _call("poll", "read_output", args={"job_id": "job-a"},
                 data={"job_id": "job-a", "state": "exited", "exit_code": 0}, sequence=2)
    refs = {category: ["poll"] for category in PROOF_CATEGORIES}
    file_call = _call("file", "write_file", args={"path": ROOT + "/main.py"},
                      data={"path": ROOT + "/main.py", "bytes": 10})
    refs["files"] = ["file"]
    origin_args = {"argv": ["python", "-m", "pytest"], "cwd": ROOT}
    for override in (
        {"agent_id": "other"}, {"computer_id": "other"}, {"computer_name": "other"},
        {"project_id": "other-project"},
        {"arguments": {**origin_args, "cwd": "/tmp"}},
        {"result": {"job_id": "job-b", "background": True, "cwd": ROOT}},
        {"result": {"job_id": "job-a", "background": False, "cwd": ROOT}},
        {"arguments": {"argv": ["echo", "x"], "cwd": ROOT}},
        {"sequence": 3},
        {"result": {"job_id": "job-a", "background": True, "cwd": "/tmp"}},
    ):
        origin = _call("origin", "run_command", args=origin_args,
                       data={"job_id": "job-a", "background": True,
                             "computer": "Sam's Mac"}, completed=False, sequence=1)
        origin["completed"] = False
        if "arguments" in override:
            origin["arguments"] = override["arguments"]
        if "result" in override:
            origin["result"] = override["result"]
        if "data" in override:
            origin["data"] = override["data"]
        for key in ("agent_id", "computer_id", "computer_name"):
            if key in override:
                origin[key] = override[key]
        if "project_id" in override:
            origin["project_id"] = override["project_id"]
        if "sequence" in override:
            origin["sequence"] = override["sequence"]
        assert not validate_goal_proofs(contract, refs, [origin, poll, file_call], PROJECT).accepted


@pytest.mark.parametrize("refs", [
    {"files": ["missing"], **{c: ["ok"] for c in PROOF_CATEGORIES if c != "files"}},
    {c: ["ok"] for c in PROOF_CATEGORIES} | {"unknown": ["ok"]},
    {c: ["ok"] * 9 for c in PROOF_CATEGORIES},
    {c: ["x" * 129] for c in PROOF_CATEGORIES},
])
def test_local_proof_references_are_bounded_and_must_resolve(refs):
    call = _run("ok", argv=["python", "-m", "pytest"])
    assert not validate_goal_proofs(_local_contract(), refs, [call], PROJECT).accepted
