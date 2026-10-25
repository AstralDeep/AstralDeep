"""CLI over astral_sdk.client.AstralClient (python -m astral_sdk), used both as an
operator tool and, spawned as a real subprocess, by
backend/tests/test_framework_conformance_088.py; prints one JSON document per
command.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from typing import Any, Optional

from astral_sdk.client import AstralClient
from astral_sdk.errors import AstralError, AstralHTTPError


def _client(args: argparse.Namespace) -> AstralClient:
    base_url = args.base_url or os.environ.get("ASTRAL_BASE_URL")
    token = args.token or os.environ.get("ASTRAL_TOKEN")
    if not base_url:
        raise SystemExit("--base-url or $ASTRAL_BASE_URL is required")
    if not token:
        raise SystemExit("--token or $ASTRAL_TOKEN is required")
    return AstralClient(base_url, token, timeout=args.timeout)


def _dump(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        value = [dataclasses.asdict(v) if dataclasses.is_dataclass(v) and not isinstance(v, type) else v
                 for v in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", default=None, help="Deep base URL (or $ASTRAL_BASE_URL)")
    parser.add_argument("--token", default=None, help="Bearer token (or $ASTRAL_TOKEN); emergency resume requires a current Keycloak owner access token")
    parser.add_argument("--timeout", type=float, default=30.0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m astral_sdk")
    sub = parser.add_subparsers(dest="command", required=True)

    tools = sub.add_parser("tools", help="List the credential's projected Work tools")
    _add_common(tools)

    submit = sub.add_parser("submit", help="Submit one chat-kind Work operation")
    _add_common(submit)
    submit.add_argument("--idempotency-key", required=True)
    submit.add_argument("--name", required=True)
    submit.add_argument("--instructions", required=True)
    submit.add_argument("--conversation-id", default=None)
    submit.add_argument("--deadline-in-seconds", type=int, default=None)

    get = sub.add_parser("get", help="Read one operation")
    _add_common(get)
    get.add_argument("operation_id")

    list_ = sub.add_parser("list", help="List recent operations")
    _add_common(list_)
    list_.add_argument("--limit", type=int, default=50)
    list_.add_argument("--after-id", default=None)

    poll = sub.add_parser("poll", help="Poll one operation for a revision change")
    _add_common(poll)
    poll.add_argument("operation_id")
    poll.add_argument("--after-revision", type=int, default=None)

    cancel = sub.add_parser("cancel", help="Cancel one operation")
    _add_common(cancel)
    cancel.add_argument("operation_id")
    cancel.add_argument("--expected-revision", type=int, required=True)
    cancel.add_argument("--submission-id", default=None)

    pause = sub.add_parser("pause", help="Pause one operation")
    _add_common(pause)
    pause.add_argument("operation_id")
    pause.add_argument("--expected-revision", type=int, required=True)
    pause.add_argument("--submission-id", default=None)

    result = sub.add_parser("result", help="Read one operation's retained result")
    _add_common(result)
    result.add_argument("operation_id")

    subscribe = sub.add_parser("subscribe-completion",
                               help="Subscribe one operation to wake when another finishes")
    _add_common(subscribe)
    subscribe.add_argument("source_operation_id")
    subscribe.add_argument("target_operation_id")
    subscribe.add_argument("--expected-revision", type=int, required=True)
    subscribe.add_argument("--submission-id", default=None)

    subscriptions = sub.add_parser("subscriptions",
                                   help="List pending completion subscriptions")
    _add_common(subscriptions)
    subscriptions.add_argument("--limit", type=int, default=50)
    subscriptions.add_argument("--after-id", default=None)

    unsubscribe = sub.add_parser("cancel-completion",
                                 help="Withdraw one pending completion subscription")
    _add_common(unsubscribe)
    unsubscribe.add_argument("source_operation_id")
    unsubscribe.add_argument("target_operation_id")
    unsubscribe.add_argument("--expected-revision", type=int, required=True)
    unsubscribe.add_argument("--submission-id", default=None)

    status = sub.add_parser("emergency-status", help="Read the emergency stop status")
    _add_common(status)

    stop = sub.add_parser("emergency-stop", help="Engage the owner emergency stop")
    _add_common(stop)
    stop.add_argument("--reason", default=None, help="Optional reason recorded in the audit log")

    resume = sub.add_parser("emergency-resume", help="Explicitly resume after an emergency stop")
    _add_common(resume)
    resume.add_argument("--expected-revision", type=int, required=True,
                        help="Current stop revision from emergency-status")

    tcreate = sub.add_parser("terminal-create", help="Open an interactive terminal session")
    _add_common(tcreate)
    tcreate.add_argument("--cols", type=int, default=80)
    tcreate.add_argument("--rows", type=int, default=24)
    tcreate.add_argument("--approval-timeout", type=float, default=120,
                         help="Seconds to wait for approval in the signed-in Astral UI")
    tcreate.add_argument("--shell-command", dest="shell_command", default=None,
                         help="Run one command instead of an interactive shell")
    tcreate.add_argument("--target", default=None,
                         help="Approved machine id for the terminal runtime (defaults to the current profile)")
    tcreate.add_argument("--directory", default=None,
                         help="Absolute starting directory on the approved runtime")

    tlist = sub.add_parser("terminal-list", help="List your open terminal sessions")
    _add_common(tlist)

    twrite = sub.add_parser("terminal-write", help="Write one base64-free text/byte chunk to a session's stdin")
    _add_common(twrite)
    twrite.add_argument("session_id")
    twrite.add_argument("--approval-timeout", type=float, default=120)
    twrite.add_argument("--data", required=True,
                        help="Text to send; prefix 'base64:' to send raw bytes")

    tread = sub.add_parser("terminal-read", help="Read new output from a session")
    _add_common(tread)
    tread.add_argument("session_id")
    tread.add_argument("--offset", type=int, default=0)
    tread.add_argument("--wait-seconds", type=float, default=0.0)

    tresize = sub.add_parser("terminal-resize", help="Resize a session's terminal")
    _add_common(tresize)
    tresize.add_argument("session_id")
    tresize.add_argument("--cols", type=int, required=True)
    tresize.add_argument("--rows", type=int, required=True)

    tinterrupt = sub.add_parser("terminal-interrupt", help="Send an interrupt (SIGINT) to a session's foreground process")
    _add_common(tinterrupt)
    tinterrupt.add_argument("session_id")

    tclose = sub.add_parser("terminal-close", help="Close a terminal session and terminate its processes")
    _add_common(tclose)
    tclose.add_argument("session_id")

    tattach = sub.add_parser("terminal-attach", help="Attach stdin/stdout to a live session until it ends")
    _add_common(tattach)
    tattach.add_argument("session_id")

    tapproval = sub.add_parser("terminal-approval", help="Check a terminal request awaiting human approval")
    _add_common(tapproval)
    tapproval.add_argument("proposal_id")

    return parser


def _terminal_payload(data: str) -> bytes:
    if data.startswith("base64:"):
        import base64

        return base64.b64decode(data[len("base64:"):])
    return data.encode("utf-8")


def _attach(client: AstralClient, session_id: str) -> int:
    import queue
    import threading

    chunks: "queue.Queue[Optional[bytes]]" = queue.Queue()

    def _pump() -> None:
        while True:
            try:
                data = os.read(0, 4096)
            except OSError:
                data = b""
            chunks.put(data or None)
            if not data:
                return

    threading.Thread(target=_pump, daemon=True).start()
    offset = 0
    stdin_open = True
    while True:
        while stdin_open:
            try:
                data = chunks.get_nowait()
            except queue.Empty:
                break
            if data is None:
                stdin_open = False
                break
            try:
                client.write_terminal(session_id, data)
            except AstralHTTPError as exc:
                if exc.code in ("terminal_session_closed", "terminal_session_not_found"):
                    return 0
                raise
        try:
            out = client.read_terminal(session_id, offset=offset, wait_seconds=0.5)
        except AstralHTTPError as exc:
            if exc.code in ("terminal_session_closed", "terminal_session_not_found"):
                return 0
            raise
        if out.data:
            sys.stdout.buffer.write(out.data)
            sys.stdout.buffer.flush()
        offset = out.next_offset
        if out.eof:
            return 0


def run(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    client = _client(args)
    try:
        if args.command == "tools":
            value: Any = client.list_tools()
        elif args.command == "submit":
            value = client.submit_operation(
                idempotency_key=args.idempotency_key, name=args.name,
                instructions=args.instructions, conversation_id=args.conversation_id,
                deadline_in_seconds=args.deadline_in_seconds)
        elif args.command == "get":
            value = client.get_operation(args.operation_id)
        elif args.command == "list":
            value = client.list_operations(limit=args.limit, after_id=args.after_id)
        elif args.command == "poll":
            value = client.poll_operation(args.operation_id, after_revision=args.after_revision)
        elif args.command == "cancel":
            value = client.cancel_operation(args.operation_id, submission_id=args.submission_id,
                                            expected_revision=args.expected_revision)
        elif args.command == "pause":
            value = client.pause_operation(args.operation_id, submission_id=args.submission_id,
                                           expected_revision=args.expected_revision)
        elif args.command == "result":
            value = client.get_artifact(args.operation_id)
        elif args.command == "subscribe-completion":
            value = client.subscribe_operation_completion(
                source_operation_id=args.source_operation_id,
                target_operation_id=args.target_operation_id,
                expected_revision=args.expected_revision, submission_id=args.submission_id)
        elif args.command == "subscriptions":
            value = client.list_completion_subscriptions(limit=args.limit, after_id=args.after_id)
        elif args.command == "cancel-completion":
            value = client.cancel_operation_completion_subscription(
                source_operation_id=args.source_operation_id,
                target_operation_id=args.target_operation_id,
                expected_revision=args.expected_revision, submission_id=args.submission_id)
        elif args.command == "emergency-status":
            value = client.emergency_status()
        elif args.command == "emergency-stop":
            value = client.emergency_stop(reason=args.reason)
        elif args.command == "emergency-resume":
            value = client.emergency_resume(expected_revision=args.expected_revision)
        elif args.command == "terminal-create":
            value = client.create_terminal_session(cols=args.cols, rows=args.rows,
                command=args.shell_command, target=args.target, directory=args.directory,
                approval_timeout_seconds=args.approval_timeout)
        elif args.command == "terminal-list":
            value = client.list_terminal_sessions()
        elif args.command == "terminal-write":
            value = client.write_terminal(args.session_id, _terminal_payload(args.data),
                                           approval_timeout_seconds=args.approval_timeout)
        elif args.command == "terminal-approval":
            value = client.get_terminal_approval(args.proposal_id)
        elif args.command == "terminal-read":
            value = client.read_terminal(args.session_id, offset=args.offset,
                                         wait_seconds=args.wait_seconds)
        elif args.command == "terminal-resize":
            value = client.resize_terminal(args.session_id, cols=args.cols, rows=args.rows)
        elif args.command == "terminal-interrupt":
            value = client.interrupt_terminal(args.session_id)
        elif args.command == "terminal-close":
            value = client.close_terminal(args.session_id)
        elif args.command == "terminal-attach":
            return _attach(client, args.session_id)
        else:  # pragma: no cover
            raise SystemExit(f"unknown command: {args.command}")
    except ValueError:
        sys.stderr.write(json.dumps({"error": "Invalid command arguments", "code": "invalid_arguments"}) + "\n")
        return 1
    except AstralHTTPError as exc:
        error = {"error": str(exc), "code": exc.code, "status_code": exc.status_code}
        if exc.code == "terminal_confirmation_required" and isinstance(exc.data, dict):
            error["proposal_id"] = exc.data.get("proposal_id")
        sys.stderr.write(json.dumps(error) + "\n")
        return 1
    except AstralError as exc:
        sys.stderr.write(json.dumps({"error": str(exc), "code": None}) + "\n")
        return 1
    finally:
        client.close()
    print(_dump(value))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
