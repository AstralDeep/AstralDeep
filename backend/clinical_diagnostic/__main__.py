"""Command line entry point for the bounded clinical answer diagnostic: frozen synthetic
fixtures by default, or an explicitly opted-in licensed-asset public run comparing the bare
model with Astral's dispatcher; never reads provider keys from the environment.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from typing import List, Optional

from clinical_diagnostic import SUITE_VERSION
from clinical_diagnostic.budget import ExecutionLimits
from clinical_diagnostic.healthbench import (
    AssetIdentityError,
    REVIEWED_ASSET_SHA256,
    SchemaError,
)
from clinical_diagnostic.models import Partition
from clinical_diagnostic.provider import OpenAICompletionProvider, load_completion_config
from clinical_diagnostic.runner import (
    EXIT_CONFIG,
    EXIT_INFRASTRUCTURE,
    EXIT_OK,
    PUBLIC_RUN_ENV,
    DiagnosticConfig,
    build_public_grader,
    build_public_producers,
    run,
)

PARTITION_NAMES = {
    "development": Partition.DEVELOPMENT,
    "held_out": Partition.HELD_OUT,
}


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="clinical_diagnostic",
        description=(
            "Bounded clinical answer diagnostic: calibrated rubric scoring over "
            "HealthBench Professional-shaped cases with infrastructure failures kept separate"
        ),
    )
    parser.add_argument("--mode", default="frozen", choices=["frozen", "public"])
    parser.add_argument(
        "--partition", action="append", dest="partitions", default=None,
        choices=sorted(PARTITION_NAMES), help="repeatable: development or held_out",
    )
    parser.add_argument("--limit", type=int, default=None, help="cap selected cases")
    parser.add_argument("--out", default=None, help="artifacts root (gitignored)")
    parser.add_argument("--run-id", default="local")
    parser.add_argument("--max-calls", type=int, default=400)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--max-elapsed", type=float, default=900.0)
    parser.add_argument("--max-cost", type=float, default=5.0)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument(
        "--no-gate", action="store_true",
        help="record the frozen fixture regression gate without failing the process",
    )
    parser.add_argument("--asset", default=None, help="public mode: licensed assets.zip path")
    parser.add_argument("--asset-member", default="", help="public mode: .json/.jsonl member")
    parser.add_argument(
        "--asset-sha256", default=REVIEWED_ASSET_SHA256,
        help="expected asset bundle SHA-256 (defaults to the reviewed public identity)",
    )
    parser.add_argument("--review-file", default=None,
                        help="JSON overlay recording clinician review status/labels")
    parser.add_argument(
        "--acknowledge-unreviewed", action="store_true",
        help="public mode: run without clinician review and label the report accordingly",
    )
    parser.add_argument("--bare-config", default=None, help="public mode: bare model JSON config")
    parser.add_argument("--grader-config", default=None, help="public mode: grader JSON config")
    parser.add_argument("--ws-uri", default=None, help="public mode: Astral WebSocket URL")
    parser.add_argument("--http-uri", default=None, help="public mode: Astral HTTP base URL")
    parser.add_argument(
        "--dispatcher-token-env", default="ASTRAL_CLINICAL_DISPATCHER_TOKEN",
        help="environment variable holding the bounded dispatcher bearer token",
    )
    return parser.parse_args(argv)


def arm_cancellation(event: threading.Event) -> object:
    def handler(_signum, _frame) -> None:
        event.set()

    return signal.signal(signal.SIGINT, handler)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        limits = ExecutionLimits(
            max_cases=args.limit if args.limit is not None else 50,
            max_calls=args.max_calls,
            max_retries=args.max_retries,
            max_elapsed_seconds=args.max_elapsed,
            max_cost=args.max_cost,
            concurrency=args.concurrency,
        )
    except ValueError as exc:
        print(f"clinical diagnostic configuration refused: {exc}")
        return EXIT_CONFIG
    partitions = tuple(
        PARTITION_NAMES[name] for name in (args.partitions or ["development", "held_out"])
    )
    config = DiagnosticConfig(
        mode=args.mode,
        partitions=partitions,
        limits=limits,
        run_id=args.run_id,
        acknowledge_unreviewed_public=args.acknowledge_unreviewed,
        enforce_frozen_gate=not args.no_gate,
        asset_path=args.asset,
        asset_member=args.asset_member,
        asset_expected_sha256=args.asset_sha256,
        review_file=args.review_file,
        public_opt_in=os.environ.get(PUBLIC_RUN_ENV) == "1",
    )
    if args.out:
        config.artifacts_root = args.out
    if args.mode == "public":
        if os.environ.get(PUBLIC_RUN_ENV) != "1":
            print(f"refusing public mode: set {PUBLIC_RUN_ENV}=1 for an explicitly opted-in run")
            return EXIT_CONFIG
        try:
            if not (args.bare_config and args.grader_config and args.ws_uri and args.http_uri):
                raise ValueError(
                    "public mode requires --bare-config, --grader-config, --ws-uri, and --http-uri"
                )
            token_env = args.dispatcher_token_env
            token = os.environ.get(token_env, "")
            if not token:
                raise ValueError(
                    f"public mode requires a dispatcher bearer token in {token_env}"
                )
            from clinical_diagnostic.dispatcher import WebSocketDispatcher

            bare = OpenAICompletionProvider(load_completion_config(args.bare_config))
            grader = build_public_grader(
                OpenAICompletionProvider(load_completion_config(args.grader_config))
            )
            dispatcher = WebSocketDispatcher(
                token=token,
                ws_uri=args.ws_uri,
                http_uri=args.http_uri,
                timeout_seconds=min(600.0, args.max_elapsed),
            )
            config.producers = build_public_producers(bare, dispatcher)
            config.grader = grader
        except (ValueError, OSError) as exc:
            print(f"clinical public run refused: {exc}")
            return EXIT_CONFIG
    cancellation = threading.Event()
    config.cancellation = cancellation
    try:
        previous = arm_cancellation(cancellation)
    except ValueError:
        previous = None
    try:
        outcome = run(config)
    except (SchemaError, AssetIdentityError, ValueError, OSError) as exc:
        print(f"clinical diagnostic run failed: {exc}")
        return EXIT_CONFIG
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)
    print(f"suite {SUITE_VERSION}: wrote {outcome.record_path}")
    print(f"report: {outcome.report_path}")
    summary = outcome.record.summary
    print(
        f"scored {summary.get('scored', 0)} · infrastructure failures "
        f"{summary.get('infrastructure_failures', 0)} · clinician-reviewed cases "
        f"{summary.get('clinician_reviewed', 0)}"
    )
    for offender in outcome.offenders:
        print(f"  gate: {offender}")
    if outcome.exit_code == EXIT_OK:
        return EXIT_OK
    if outcome.exit_code == EXIT_INFRASTRUCTURE:
        print("infrastructure failures are reported separately and never scored")
    return outcome.exit_code


if __name__ == "__main__":
    sys.exit(main())