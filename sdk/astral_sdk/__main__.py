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
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", default=None, help="Deep base URL (or $ASTRAL_BASE_URL)")
    parser.add_argument("--token", default=None, help="Framework credential bearer (or $ASTRAL_TOKEN)")
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

    return parser


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
        else:  # pragma: no cover
            raise SystemExit(f"unknown command: {args.command}")
    except AstralHTTPError as exc:
        error = {"error": str(exc), "code": exc.code, "status_code": exc.status_code}
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
