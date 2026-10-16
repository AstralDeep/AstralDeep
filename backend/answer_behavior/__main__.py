"""CLI entry point for `python -m answer_behavior`: runs the frozen (CI) suite by
default, or the opt-in bounded non-gating live diagnostic, writes artifacts, and exits 1
when the frozen candidate gate trips.
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from answer_behavior import SUITE_VERSION
from answer_behavior.drivers import LIVE_ENV_FLAG
from answer_behavior.models import Verdict
from answer_behavior.runner import LIVE_DEFAULT_FLOOR_NOTE, RunConfig, run

DIMENSION_NAMES = ("instruction_precedence", "abstention", "truthful_completion")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="answer_behavior",
        description=(
            "Bounded synthetic answer-behavior diagnostics: instruction precedence, "
            "abstention, and truthful action/resource reporting under Astral-owned rubrics"
        ),
    )
    parser.add_argument("--mode", default="frozen", choices=["frozen", "live"])
    parser.add_argument(
        "--actor", action="append", dest="actors", default=None,
        help="frozen actors to score (repeatable): baseline | candidate",
    )
    parser.add_argument("--model", default=None, help="model label recorded in the run key")
    parser.add_argument("--corpus", default=None, help="override the frozen corpus path")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--limit", type=int, default=None, help="cap the number of cases")
    parser.add_argument("--out", default=None, help="artifacts root (gitignored)")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--stamp", default="local")
    parser.add_argument(
        "--floor", type=float, default=1.0,
        help="frozen-mode candidate success floor per dimension (default 1.0)",
    )
    parser.add_argument(
        "--live-config", default=None,
        help="runtime JSON file with base_url/model (api_key optional, keyless local endpoints)",
    )
    parser.add_argument("--live-max-cases", type=int, default=8)
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.mode == "live":
        import os

        if os.environ.get(LIVE_ENV_FLAG) != "1":
            print(
                f"refusing to run live mode: set {LIVE_ENV_FLAG}=1 "
                f"({LIVE_DEFAULT_FLOOR_NOTE})"
            )
            return 2
    actors = args.actors if args.actors else ["baseline", "candidate"]
    config = RunConfig(
        mode=args.mode,
        actors=actors,
        model=args.model or ("live-model" if args.mode == "live" else "frozen-corpus"),
        corpus_path=args.corpus,
        seed=args.seed,
        limit=args.limit,
        run_id=args.run_id,
        success_floor=args.floor,
        live_config_path=args.live_config,
        live_max_cases=args.live_max_cases,
    )
    if args.out:
        config.artifacts_root = args.out

    try:
        record, report_path, offenders = run(config, stamp=args.stamp)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"answer-behavior run failed: {exc}")
        return 2

    print(f"suite {SUITE_VERSION}: wrote report → {report_path}")
    for score in record.scores:
        marker = "OK" if score.result.verdict.is_success else (
            "UNKNOWN" if score.result.verdict is Verdict.UNSCOREABLE else "VIOLATION"
        )
        print(
            f"  {marker:>8} {score.actor:>9} {score.case_id} [{score.dimension}] "
            f"→ {score.result.verdict.value}"
        )
    if config.mode == "live":
        print(LIVE_DEFAULT_FLOOR_NOTE + "; no gate applied")
        return 0
    if offenders:
        print("CANDIDATE GATE TRIPPED:")
        for offender in offenders:
            print(f"  {offender}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
