"""Tracks bounded per-request tool evidence for completion verification."""

from __future__ import annotations

import hashlib
import json
import logging
import posixpath
import re
from dataclasses import dataclass, field
from typing import Any


_MAX_TEXT = 4096
_MAX_ITEMS = 32
_MAX_EVIDENCE = 64
_MAX_CALLS = 256
_MAX_FAILURES = 256
_MAX_VALUE_TEXT = 512
_SENSITIVE_KEYS = re.compile(r"(credential|password|secret|token|authorization|api.?key|image|base64|audio|binary|blob)", re.I)
_SENSITIVE_TEXT = re.compile(r"(?:credential|password|secret|token|authorization|api.?key)\s*(?:[:=]|\s)", re.I)
_SENSITIVE_ARG = re.compile(r"(?:^|[-_])(?:credential|password|secret|token|authorization|api.?key)(?:$|=)", re.I)
_BASE64_TEXT = re.compile(r"^[A-Za-z0-9+/]{80,}={0,2}$")
_logger = logging.getLogger(__name__)
_TERMINAL_JOB_STATES = frozenset({"exited", "timed_out", "interrupted"})
_UNCERTAIN_CODES = frozenset({"uncertain", "outcome_uncertain", "effect_uncertain"})
_NO_EFFECT_WRITE_REJECTION_CODES = frozenset({"out_of_range", "invalid_tool_arguments"})


@dataclass(frozen=True)
class CompletionDecision:
    status: str
    remaining: tuple[str, ...] = ()
    reason: str = ""
    resolutions: tuple[tuple[str, str], ...] = ()


@dataclass(init=False)
class GoalProgress:
    _request: str = field(repr=False)
    pending_jobs: set[str] = field(default_factory=set)
    tool_count: int = 0
    _evidence: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    _last_failure: str | None = field(default=None, init=False, repr=False)
    _repeat_failures: int = field(default=0, init=False, repr=False)
    _stalled: bool = field(default=False, init=False, repr=False)
    _no_tool_continues: int = field(default=0, init=False, repr=False)
    _uncertain_jobs: set[str] = field(default_factory=set, init=False, repr=False)
    _uncertain_result: bool = field(default=False, init=False, repr=False)
    _sequence: int = field(default=0, init=False, repr=False)
    _call_ids: set[str] = field(default_factory=set, init=False, repr=False)
    _unresolved_failures: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _successful_calls: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _call_outcomes: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    _failure_overflow: int = field(default=0, init=False, repr=False)
    _unresolved_unknown: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _unknown_overflow: int = field(default=0, init=False, repr=False)
    _private_observations: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    _pending_approvals: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _early_approval_results: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)

    def __init__(self, request: str) -> None:
        if not isinstance(request, str):
            raise TypeError("request must be a string")
        self._request = request
        self.pending_jobs = set()
        self.tool_count = 0
        self._evidence = []
        self._last_failure = None
        self._repeat_failures = 0
        self._stalled = False
        self._no_tool_continues = 0
        self._uncertain_jobs = set()
        self._uncertain_result = False
        self._sequence = 0
        self._call_ids = set()
        self._unresolved_failures = {}
        self._successful_calls = {}
        self._call_outcomes = []
        self._failure_overflow = 0
        self._unresolved_unknown = {}
        self._unknown_overflow = 0
        self._private_observations = []
        self._pending_approvals = {}
        self._early_approval_results = {}

    @property
    def request(self) -> str:
        return self._request

    @property
    def stalled(self) -> bool:
        return self._stalled

    @property
    def requires_inspection(self) -> bool:
        return bool(self._uncertain_result or self._unresolved_unknown or self._unknown_overflow)

    @property
    def requires_approval(self) -> bool:
        return bool(self._pending_approvals)

    @staticmethod
    def is_approval_pending(result: object) -> bool:
        data, _error = GoalProgress._unwrap(result)
        return isinstance(data, dict) and data.get("status") == "confirmation_required"

    @staticmethod
    def is_completed_success(tool_name: str, result: object) -> bool:
        data, error = GoalProgress._unwrap(result)
        outcome = GoalProgress._outcome(data, error, GoalProgress._job_state(data))
        return outcome == "success" and GoalProgress._completed(tool_name, data, outcome)

    def record(
        self,
        tool_call_id: str,
        tool_name: str,
        arguments: str | dict,
        result: object,
        *,
        agent_id: str | None = None,
        computer_id: str | None = None,
        computer_name: str | None = None,
        project_context: dict[str, Any] | None = None,
        approval_token: str | None = None,
        worker_attested: bool = False,
    ) -> None:
        self.tool_count += 1
        self._no_tool_continues = 0
        args = self._arguments(arguments)
        data, error = self._unwrap(result)
        status = self._job_state(data)
        outcome = self._outcome(data, error, status)
        error_code, effect_state = self._error_metadata(error)
        self._sequence += 1
        provider_call_id = str(tool_call_id)[:128]
        call_id = self._observation_call_id(provider_call_id, self._sequence)
        fingerprint = self._fingerprint(tool_name, args)
        completed = self._completed(tool_name, data, outcome)
        observation_args = self._observation_arguments(tool_name, args)
        observation_result = self._observation_result(tool_name, data)
        context = project_context if isinstance(project_context, dict) else {}
        identity = {
            "agent_id": self._bounded_identity(agent_id),
            "computer_id": self._bounded_identity(computer_id if computer_id is not None else context.get("computer_id")),
            "computer_name": self._bounded_identity(computer_name if computer_name is not None else context.get("computer_name")),
            "project_id": self._bounded_identity(context.get("project_id")),
        }
        failure_signature = json.dumps([tool_name, self._safe(args), self._safe(data), self._safe(error)], sort_keys=True, separators=(",", ":"))
        no_effect_malformed_write = (
            tool_name == "write_file" and outcome == "failure"
            and error_code in _NO_EFFECT_WRITE_REJECTION_CODES and effect_state == "not_started"
            and not (isinstance(args.get("path"), str) and args["path"].strip())
        )
        if outcome == "failure":
            if failure_signature == self._last_failure:
                self._repeat_failures += 1
            else:
                self._last_failure = failure_signature
                self._repeat_failures = 1
            self._stalled = self._repeat_failures >= 3
        else:
            self._last_failure = None
            self._repeat_failures = 0
            self._stalled = False
        if outcome == "uncertain":
            job_id = data.get("job_id") if isinstance(data, dict) else None
            if tool_name == "run_command" and isinstance(job_id, str) and job_id:
                self._uncertain_jobs.add(job_id)
                self.pending_jobs.add(job_id)
            elif tool_name in {"read_output", "interrupt_command"}:
                job_id = args.get("job_id")
                if isinstance(job_id, str) and job_id in self.pending_jobs:
                    self._uncertain_jobs.add(job_id)
                else:
                    self._uncertain_result = True
            else:
                self._uncertain_result = True
        if outcome == "pending_approval":
            token = approval_token if isinstance(approval_token, str) and approval_token else call_id
            self._pending_approvals[token[:128]] = {
                "call_id": call_id,
                "tool": str(tool_name)[:128],
                "agent_id": identity["agent_id"],
                "sequence": self._sequence,
                "worker_attested": worker_attested is True,
            }
        self._update_jobs(tool_name, args, data, status)
        if outcome == "failure" and not no_effect_malformed_write:
            if call_id not in self._unresolved_failures:
                if len(self._unresolved_failures) < _MAX_FAILURES:
                    self._unresolved_failures[call_id] = {
                        "fingerprint": fingerprint,
                        "sequence": self._sequence,
                        "tool": str(tool_name)[:128],
                        "arguments": observation_args,
                        "result": observation_result,
                        "error_code": error_code,
                        "effect_state": effect_state,
                        **identity,
                    }
                else:
                    self._failure_overflow += 1
        elif outcome == "unknown":
            if call_id not in self._unresolved_unknown:
                if len(self._unresolved_unknown) < _MAX_FAILURES:
                    self._unresolved_unknown[call_id] = {
                        "fingerprint": fingerprint,
                        "sequence": self._sequence,
                        "tool": str(tool_name)[:128],
                        "arguments": observation_args,
                        "result": observation_result,
                        **identity,
                    }
                else:
                    self._unknown_overflow += 1
        elif outcome == "success" and completed:
            for failed_id, failure in tuple(self._unresolved_failures.items()):
                if (failure["fingerprint"] == fingerprint and failure["sequence"] < self._sequence
                        and self._same_identity(failure, identity)):
                    del self._unresolved_failures[failed_id]
            self._successful_calls[call_id] = {
                "sequence": self._sequence,
                "tool": str(tool_name)[:128],
                "fingerprint": fingerprint,
                "arguments": observation_args,
                "result": observation_result,
                **identity,
            }
            if len(self._successful_calls) > _MAX_CALLS:
                del self._successful_calls[next(iter(self._successful_calls))]
        self._call_outcomes.append({
            "call_id": call_id,
            "tool": str(tool_name)[:128],
            "outcome": outcome,
            "completed": completed,
            "sequence": self._sequence,
            "fingerprint": fingerprint,
        })
        if len(self._call_outcomes) > _MAX_CALLS:
            del self._call_outcomes[:-_MAX_CALLS]
        self._private_observations.append({
            "call_id": call_id,
            **({"provider_call_id": provider_call_id} if call_id != provider_call_id else {}),
            "tool": str(tool_name)[:128],
            **identity,
            "arguments": observation_args,
            "result": observation_result,
            "outcome": outcome,
            "completed": completed,
            "sequence": self._sequence,
            **({"attested_worker": True} if worker_attested is True else {}),
            **({"error_code": error_code} if error_code else {}),
            **({"effect_state": effect_state} if effect_state else {}),
        })
        if len(self._private_observations) > _MAX_CALLS:
            del self._private_observations[:-_MAX_CALLS]
        item = {
            "call_id": call_id,
            "tool": str(tool_name)[:128],
            "arguments": self._safe(args),
            "result": self._safe(data),
            "error": self._safe(error),
            "status": str(status)[:64] if status is not None else "unknown",
            "outcome": outcome,
        }
        self._evidence.append(item)
        if len(self._evidence) > _MAX_EVIDENCE:
            del self._evidence[:-_MAX_EVIDENCE]
        early = self._early_approval_results.pop(approval_token, None) if outcome == "pending_approval" and approval_token else None
        if early is not None:
            self.resolve_approval(
                approval_token, early["result"], tool_name=early["tool_name"],
                arguments=early["arguments"], agent_id=early["agent_id"],
                computer_id=early["computer_id"], computer_name=early["computer_name"],
                project_context=early["project_context"],
            )

    def _observation_call_id(self, provider_call_id: str, sequence: int) -> str:
        if provider_call_id and provider_call_id not in self._call_ids:
            self._call_ids.add(provider_call_id)
            return provider_call_id
        if provider_call_id:
            _logger.warning("goal.provider_call_id_reused sequence=%d", sequence)
        base = f"goal-call-{sequence}"
        candidate = base
        suffix = 1
        while candidate in self._call_ids:
            suffix += 1
            candidate = f"{base}-{suffix}"
        self._call_ids.add(candidate)
        return candidate

    def evidence(self) -> dict[str, Any]:
        return {
            "request": self.request,
            "tools": [dict(item) for item in self._evidence],
            "pending_jobs": sorted(self.pending_jobs)[:_MAX_ITEMS],
            "uncertain_jobs": sorted(self._uncertain_jobs)[:_MAX_ITEMS],
            "unresolved_failures": sorted(self._unresolved_failures),
            "unresolved_failure_details": [
                {
                    "call_id": call_id, "tool": item["tool"], "sequence": item["sequence"],
                    "outcome": "failure", "fingerprint": item["fingerprint"],
                    "error_code": item.get("error_code"),
                    "effect_state": item.get("effect_state"),
                }
                for call_id, item in self._unresolved_failures.items()
            ],
            "failure_tracking_overflow": self._failure_overflow,
            "unresolved_unknown": sorted(self._unresolved_unknown),
            "unresolved_unknown_details": [
                {"call_id": call_id, "tool": item["tool"], "sequence": item["sequence"], "outcome": "unknown", "fingerprint": item["fingerprint"]}
                for call_id, item in self._unresolved_unknown.items()
            ],
            "unknown_tracking_overflow": self._unknown_overflow,
            "call_outcomes": [dict(item) for item in self._call_outcomes],
            "completed_successes": [
                {"call_id": call_id, **item, "outcome": "success", "completed": True}
                for call_id, item in self._successful_calls.items()
            ],
        }

    def private_observations(self) -> list[dict[str, Any]]:
        return [
            {
                **item,
                "arguments": {
                    key: list(value) if isinstance(value, list) else value
                    for key, value in item["arguments"].items()
                },
                "result": dict(item["result"]),
            }
            for item in self._private_observations
        ]

    def resolve_approval(
        self,
        approval_token: str,
        result: object,
        *,
        tool_name: str,
        arguments: str | dict,
        agent_id: str | None = None,
        computer_id: str | None = None,
        computer_name: str | None = None,
        project_context: dict[str, Any] | None = None,
    ) -> bool:
        pending = self._pending_approvals.get(approval_token)
        if self.is_approval_pending(result):
            return False
        if pending is None:
            if (not isinstance(approval_token, str) or not approval_token or len(approval_token) > 128
                    or approval_token in self._early_approval_results
                    or len(self._early_approval_results) >= 32):
                return False
            self._early_approval_results[approval_token] = {
                "result": result, "tool_name": tool_name, "arguments": arguments,
                "agent_id": agent_id, "computer_id": computer_id, "computer_name": computer_name,
                "project_context": project_context,
            }
            return True
        if tool_name != pending["tool"] or agent_id != pending["agent_id"]:
            return False
        self._pending_approvals.pop(approval_token)
        self.record(
            f"approval-{pending['call_id']}", tool_name, arguments, result,
            agent_id=agent_id, computer_id=computer_id, computer_name=computer_name,
            project_context=project_context,
            worker_attested=pending.get("worker_attested") is True,
        )
        return True

    def assess(self, decision: CompletionDecision) -> CompletionDecision:
        if self._stalled:
            return CompletionDecision("blocked", (), "Repeated identical tool failures indicate the request is stalled.")
        if self._pending_approvals:
            return CompletionDecision("blocked", (), "A requested action is waiting for owner approval.")
        if self._uncertain_result:
            return CompletionDecision("blocked", (), "Inspect the uncertain tool effect and verify whether it completed before retrying or declaring completion.")
        if self._uncertain_jobs:
            jobs = ", ".join(sorted(self._uncertain_jobs)[:_MAX_ITEMS])
            return self._continue_or_block((f"Inspect uncertain computer jobs with read_output before completion: {jobs}.",), decision.reason)
        if self.pending_jobs:
            jobs = ", ".join(sorted(self.pending_jobs)[:_MAX_ITEMS])
            return self._continue_or_block((f"Poll pending computer jobs with read_output: {jobs}.",), decision.reason)
        if decision.status == "complete":
            self._apply_resolutions(decision.resolutions)
            if (self._unresolved_failures or self._failure_overflow
                    or self._unresolved_unknown or self._unknown_overflow):
                failure_ids = ", ".join(sorted(self._unresolved_failures)[:_MAX_ITEMS])
                unknown_ids = ", ".join(sorted(self._unresolved_unknown)[:_MAX_ITEMS])
                if self._failure_overflow:
                    failure_ids = f"{failure_ids}, plus {self._failure_overflow} untracked failures" if failure_ids else f"{self._failure_overflow} untracked failures"
                if self._unknown_overflow:
                    unknown_ids = f"{unknown_ids}, plus {self._unknown_overflow} untracked outcomes" if unknown_ids else f"{self._unknown_overflow} untracked outcomes"
                details = []
                if failure_ids:
                    details.append(f"failed calls: {failure_ids}")
                if unknown_ids:
                    details.append(f"calls without a known result: {unknown_ids}")
                return self._continue_or_block((f"Recover or explicitly verify these tool outcomes before completion: {'; '.join(details)}.",), decision.reason)
        if decision.status == "continue":
            return self._continue_or_block(decision.remaining, decision.reason)
        self._no_tool_continues = 0
        return decision

    def _continue_or_block(self, remaining: tuple[str, ...], reason: str) -> CompletionDecision:
        self._no_tool_continues += 1
        if self._no_tool_continues >= 3:
            return CompletionDecision("blocked", (), "Three completion checks continued without any new tool calls.")
        return CompletionDecision("continue", remaining, reason)

    def _apply_resolutions(self, resolutions: tuple[tuple[str, str], ...]) -> None:
        if not isinstance(resolutions, (tuple, list)):
            return
        for pair in resolutions:
            if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                continue
            failed_id, success_id = pair
            if (
                not isinstance(failed_id, str) or not failed_id or len(failed_id) > 128
                or not isinstance(success_id, str) or not success_id or len(success_id) > 128
                or failed_id == success_id
            ):
                continue
            failure = self._unresolved_failures.get(failed_id)
            unknown = self._unresolved_unknown.get(failed_id)
            success = self._successful_calls.get(success_id)
            unresolved = failure or unknown
            if unresolved and success and self._resolution_matches(unresolved, success, unknown=unknown is not None):
                self._unresolved_failures.pop(failed_id, None)
                self._unresolved_unknown.pop(failed_id, None)

    @staticmethod
    def _resolution_matches(unresolved: dict[str, Any], success: dict[str, Any], *, unknown: bool) -> bool:
        if success["sequence"] <= unresolved["sequence"]:
            return False
        if not GoalProgress._same_identity(unresolved, success):
            return False
        file_effect = unresolved["tool"] in {"write_file", "delete_path", "read_file"}
        tool = unresolved["tool"]
        if (not unknown and tool == "write_file"
                and unresolved.get("error_code") in _NO_EFFECT_WRITE_REJECTION_CODES
                and unresolved.get("effect_state") == "not_started"):
            from orchestrator.computer_use_policy import MAX_WRITE_BYTES

            failed_size = unresolved["arguments"].get("content_size")
            success_size = success["arguments"].get("content_size")
            return (
                success["tool"] == "write_file"
                and type(failed_size) is int and failed_size > MAX_WRITE_BYTES
                and type(success_size) is int and 0 < success_size < failed_size
                and success_size <= MAX_WRITE_BYTES
                and GoalProgress._same_path(
                    unresolved["arguments"].get("path"), unresolved["arguments"].get("cwd"),
                    success["arguments"].get("path"), success["arguments"].get("cwd"),
                )
            )
        if unresolved["fingerprint"] == success["fingerprint"] and not (unknown and file_effect):
            return True
        if file_effect:
            if (not unknown and tool == "write_file" and success["tool"] == "write_file"
                    and unresolved["arguments"].get("if_exists", "refuse") == "refuse"
                    and success["arguments"].get("if_exists") == "overwrite"):
                expected_digest = unresolved["arguments"].get("content_sha256")
                return (
                    GoalProgress._same_path(
                        unresolved["arguments"].get("path"), unresolved["arguments"].get("cwd"),
                        success["arguments"].get("path"), success["arguments"].get("cwd"),
                    )
                    and isinstance(expected_digest, str)
                    and success["arguments"].get("content_sha256") == expected_digest
                )
            if not unknown or tool == "delete_path":
                return False
            if success["tool"] != "read_file" or success["result"].get("truncated") is True:
                return False
            if success["result"].get("has_meaningful_content") is not True:
                return False
            same_path = GoalProgress._same_path(
                unresolved["arguments"].get("path"), unresolved["arguments"].get("cwd"),
                success["arguments"].get("path"), success["arguments"].get("cwd"),
            )
            if tool == "write_file":
                expected_digest = unresolved["arguments"].get("content_sha256")
                return (same_path and isinstance(expected_digest, str)
                        and success["result"].get("content_sha256") == expected_digest)
            return same_path
        if tool == "run_command":
            original_result = unresolved["result"]
            job_id = original_result.get("job_id")
            return (
                original_result.get("background") is True
                and isinstance(job_id, str) and bool(job_id)
                and success["tool"] == "read_output"
                and success["arguments"].get("job_id") == job_id
                and success["result"].get("job_id", job_id) == job_id
                and success["result"].get("state", success["result"].get("status")) == "exited"
                and success["result"].get("exit_code") == 0
            )
        if tool == "read_output":
            job_id = unresolved["arguments"].get("job_id")
            return (
                isinstance(job_id, str) and bool(job_id)
                and success["tool"] == "read_output"
                and success["arguments"].get("job_id") == job_id
                and success["result"].get("job_id", job_id) == job_id
                and success["result"].get("state", success["result"].get("status")) == "exited"
                and success["result"].get("exit_code") == 0
            )
        return False

    @staticmethod
    def _same_identity(first: dict[str, Any], second: dict[str, Any]) -> bool:
        return all(first.get(key) == second.get(key)
                   for key in ("agent_id", "computer_id", "computer_name", "project_id"))

    @staticmethod
    def _same_path(first: Any, first_cwd: Any, second: Any, second_cwd: Any) -> bool:
        if not all(isinstance(path, str) and path for path in (first, second)):
            return False
        def canonical(path: str, cwd: Any) -> str:
            if not posixpath.isabs(path):
                if not isinstance(cwd, str) or not posixpath.isabs(cwd):
                    return ""
                path = posixpath.join(cwd, path)
            return posixpath.normpath(path)
        first_path = canonical(first, first_cwd)
        second_path = canonical(second, second_cwd)
        return bool(first_path and second_path and first_path == second_path)

    @staticmethod
    def _bounded_identity(value: Any) -> str | None:
        return value[:128] if isinstance(value, str) and value else None

    @classmethod
    def _observation_arguments(cls, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        allowed = {"cwd", "argv", "command", "path", "job_id", "computer", "if_exists"}
        observed = {}
        for key in allowed:
            value = args.get(key)
            if isinstance(value, str):
                if (key != "if_exists" or value in {"refuse", "overwrite"}) and not _SENSITIVE_TEXT.search(value):
                    observed[key] = value[:512]
            elif key == "argv" and isinstance(value, list):
                argv = [item[:512] if isinstance(item, str) else cls._safe(item) for item in value[:32]]
                if not any(isinstance(item, str) and (_SENSITIVE_TEXT.search(item) or _SENSITIVE_ARG.search(item)) for item in argv):
                    observed[key] = argv
        if tool_name == "write_file" and isinstance(args.get("content"), str):
            encoded = args["content"].encode("utf-8")
            observed["content_sha256"] = hashlib.sha256(encoded).hexdigest()
            observed["content_size"] = len(encoded)
        return observed

    @classmethod
    def _observation_result(cls, tool_name: str, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            return {}
        allowed = {"cwd", "path", "computer", "exit_code", "timed_out", "signal", "state", "status", "background", "job_id", "truncated", "size", "count", "has_entries", "bytes", "bytes_written"}
        observed = {}
        for key in allowed:
            value = data.get(key)
            if isinstance(value, (str, int, float, bool)):
                observed[key] = value[:512] if isinstance(value, str) else value
        if tool_name == "read_file":
            content = next((data[key] for key in ("text", "source", "content", "code")
                            if key in data and cls._meaningful_content(data[key])), None)
            observed["has_meaningful_content"] = cls._meaningful_content(content)
            if isinstance(content, str):
                observed["content_sha256"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
            observed["truncated"] = bool(data.get("truncated", data.get("was_truncated", False)))
        return observed

    @staticmethod
    def _meaningful_content(value: Any) -> bool:
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, dict)):
            return bool(value)
        return False

    @staticmethod
    def _fingerprint(tool_name: str, args: dict[str, Any]) -> str:
        payload = json.dumps([tool_name, args], sort_keys=True, separators=(",", ":"), default=lambda item: f"[{type(item).__name__}]")
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _completed(tool_name: str, data: Any, outcome: str) -> bool:
        if outcome != "success":
            return False
        if tool_name == "run_command":
            if not isinstance(data, dict):
                return False
            return (
                data.get("background") is not True
                and data.get("job_id") is None
                and GoalProgress._job_state(data) not in {"running", "uncertain"}
                and data.get("timed_out") is not True
                and data.get("exit_code") == 0
            )
        if tool_name == "read_output":
            if not isinstance(data, dict):
                return False
            return data.get("state", data.get("status")) == "exited" and data.get("exit_code") == 0
        if tool_name == "interrupt_command":
            return False
        return isinstance(data, (dict, list, str)) and bool(data)

    @staticmethod
    def _unwrap(result: object) -> tuple[Any, Any]:
        error = getattr(result, "error", None)
        raw = getattr(result, "result", result)
        if isinstance(raw, dict) and "_error" in raw and error is None:
            error = raw.get("_error")
        if isinstance(raw, dict) and "error" in raw and error is None:
            error = raw.get("error")
        if isinstance(raw, dict) and "_data" in raw:
            raw = raw.get("_data")
        if isinstance(raw, dict) and {"code", "message", "next_action"} <= raw.keys() and error is None:
            error = {key: raw[key] for key in ("code", "message", "next_action") if key in raw}
        return raw, error

    @staticmethod
    def _error_metadata(error: Any) -> tuple[str | None, str | None]:
        if isinstance(error, dict):
            code = error.get("code")
            effect_state = error.get("effect_state")
        else:
            code = getattr(error, "code", None)
            effect_state = getattr(error, "effect_state", None)
        return (
            code[:64] if isinstance(code, str) else None,
            effect_state[:64] if isinstance(effect_state, str) else None,
        )

    @staticmethod
    def _outcome(data: Any, error: Any, status: Any) -> str:
        if isinstance(data, dict) and data.get("status") == "confirmation_required":
            return "pending_approval"
        error_code = error.get("code") if isinstance(error, dict) else getattr(error, "code", None)
        data_code = data.get("code") if isinstance(data, dict) else None
        if (
            (isinstance(error_code, str) and error_code in _UNCERTAIN_CODES)
            or (isinstance(data_code, str) and data_code in _UNCERTAIN_CODES)
            or (isinstance(status, str) and status in _UNCERTAIN_CODES)
        ):
            return "uncertain"
        if error:
            return "failure"
        if isinstance(data, dict):
            if data.get("error") or data.get("ok") is False:
                return "failure"
            if {"code", "message", "next_action"} <= data.keys():
                return "failure"
            if data.get("timed_out") is True or data.get("exit_code") not in (None, 0):
                return "failure"
        if status in {"error", "failed", "failure", "timed_out"}:
            return "failure"
        if status == "uncertain":
            return "uncertain"
        if not isinstance(data, dict) or not data:
            return "success" if isinstance(data, (list, str)) and bool(data) else "unknown"
        return "success"

    def _update_jobs(self, tool_name: str, args: dict[str, Any], data: Any, status: Any) -> None:
        if tool_name == "run_command" and isinstance(data, dict):
            job_id = data.get("job_id")
            if isinstance(job_id, str) and job_id and data.get("background") is True:
                if status in _TERMINAL_JOB_STATES or data.get("timed_out") is True:
                    self.pending_jobs.discard(job_id)
                else:
                    self.pending_jobs.add(job_id)
        if tool_name in {"read_output", "interrupt_command"}:
            job_id = args.get("job_id")
            if isinstance(job_id, str) and job_id in self.pending_jobs and isinstance(data, dict):
                observed_id = data.get("job_id", job_id)
                if observed_id == job_id:
                    if tool_name == "interrupt_command" and job_id in self._uncertain_jobs:
                        return
                    if status in _TERMINAL_JOB_STATES:
                        self.pending_jobs.discard(job_id)
                        self._uncertain_jobs.discard(job_id)
                    elif status == "uncertain":
                        self._uncertain_jobs.add(job_id)
                    elif status == "running":
                        self._uncertain_jobs.discard(job_id)

    @staticmethod
    def _arguments(arguments: str | dict) -> dict[str, Any]:
        if isinstance(arguments, dict):
            return arguments
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments)
            except (json.JSONDecodeError, TypeError):
                return {"text": arguments}
            return parsed if isinstance(parsed, dict) else {"text": arguments}
        return {}

    @staticmethod
    def _job_state(data: Any) -> Any:
        if isinstance(data, dict):
            state = data.get("state", data.get("status"))
            if not isinstance(state, str):
                return None
            if state in _UNCERTAIN_CODES:
                return "uncertain"
            if state in _TERMINAL_JOB_STATES or state == "running":
                return state
            if state in {"error", "failed", "failure"}:
                return state
        return None

    @classmethod
    def _safe(cls, value: Any, depth: int = 0) -> Any:
        if depth > 5:
            return "[truncated]"
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            if _BASE64_TEXT.fullmatch(value):
                return "[omitted]"
            return value[:_MAX_VALUE_TEXT]
        if isinstance(value, (list, tuple)):
            return [cls._safe(item, depth + 1) for item in value[:_MAX_ITEMS]]
        if isinstance(value, dict):
            safe = {}
            for key, item in list(value.items())[:_MAX_ITEMS]:
                name = str(key)[:128]
                if _SENSITIVE_KEYS.search(name):
                    continue
                safe[name] = cls._safe(item, depth + 1)
            return safe
        return str(type(value).__name__)


def parse_completion(text: str) -> CompletionDecision:
    if not isinstance(text, str) or len(text) > _MAX_TEXT:
        raise ValueError("completion response must be bounded JSON text")
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("completion response must be a JSON object") from exc
    if type(value) is not dict or set(value) not in ({"status", "remaining", "reason"}, {"status", "remaining", "reason", "resolutions"}):
        raise ValueError("completion response has an invalid shape")
    status, remaining, reason = value["status"], value["remaining"], value["reason"]
    resolutions = value.get("resolutions", {})
    if type(status) is not str or status not in {"complete", "continue", "blocked"}:
        raise ValueError("completion status is invalid")
    if type(remaining) is not list or len(remaining) > _MAX_ITEMS or any(type(item) is not str or not item or len(item) > _MAX_VALUE_TEXT for item in remaining):
        raise ValueError("completion remaining items are invalid")
    if type(reason) is not str or len(reason) > _MAX_VALUE_TEXT:
        raise ValueError("completion reason is invalid")
    if type(resolutions) is not dict or len(resolutions) > 32 or any(
        type(failed_id) is not str or not failed_id or len(failed_id) > 128
        or type(success_id) is not str or not success_id or len(success_id) > 128
        for failed_id, success_id in resolutions.items()
    ):
        raise ValueError("completion resolutions are invalid")
    if status == "complete" and remaining:
        raise ValueError("complete responses cannot have remaining work")
    if status == "continue" and not remaining:
        raise ValueError("continue responses require remaining work")
    if status == "blocked" and not reason:
        raise ValueError("blocked responses require a reason")
    return CompletionDecision(status, tuple(remaining), reason, tuple(resolutions.items()))
