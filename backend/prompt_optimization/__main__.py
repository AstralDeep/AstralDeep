"""CLI entry point for `python -m prompt_optimization`: runs the deterministic offline
workbench by default, or the opt-in bounded live qualification when explicitly enabled
via environment flag and runtime config file, writes the candidate and report artifacts,
and never applies any candidate to product prompts.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path
from typing import List, Optional

from prompt_optimization import budget as budget_module
from prompt_optimization import model_io as model_io_module
from prompt_optimization import optimizer as optimizer_module
from prompt_optimization import proposals as proposals_module
from prompt_optimization import report as report_module
from prompt_optimization import task as task_module
from prompt_optimization.fixture import build_fixture

LIVE_ENV_FLAG = "ASTRAL_PROMPT_OPTIMIZATION_LIVE"
DEFAULT_MODEL_LABEL = "scripted-double-v1"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="prompt_optimization",
        description=(
            "Bounded offline prompt-optimization pilot: deterministic scripted "
            "proposals and provider doubles by default, optional authorized "
            "live-model qualification, inert review artifacts only"
        ),
    )
    parser.add_argument("--out", default=None, help="artifact output directory")
    parser.add_argument("--rounds", type=int, default=4, help="maximum search rounds")
    parser.add_argument(
        "--live-config",
        default=None,
        help=(
            "runtime config file (base_url/model, api_key optional) for the "
            "opt-in live qualification; requires ASTRAL_PROMPT_OPTIMIZATION_LIVE"
        ),
    )
    parser.add_argument(
        "--model-label",
        default=DEFAULT_MODEL_LABEL,
        help="model identity recorded in the report",
    )
    return parser.parse_args(argv)


def build_limits(rounds: int) -> budget_module.BudgetLimits:
    return budget_module.BudgetLimits(
        max_reflection_calls=6,
        max_evaluation_calls=64,
        max_total_tokens=120000,
        max_retries_per_call=1,
        max_output_chars=4000,
        max_candidates_per_round=4,
        max_rounds=max(1, min(int(rounds), 8)),
    )


def run(args: argparse.Namespace) -> int:
    root = repo_root()
    task_prompt = task_module.load_task_prompt(root)
    fixture = build_fixture()
    limits = build_limits(args.rounds)
    if args.live_config:
        if os.getenv(LIVE_ENV_FLAG, "").strip().lower() not in ("1", "true", "yes", "on"):
            print(
                f"live qualification requires {LIVE_ENV_FLAG} to be explicitly enabled",
                file=sys.stderr,
            )
            return 2
        adapter = model_io_module.LiveCompletion(args.live_config)
        model_identity = f"live:{adapter.model}"
    else:
        variants = proposals_module.scripted_variants(task_prompt.rules)
        adapter = model_io_module.ProviderDouble(variants=variants)
        model_identity = f"scripted:{args.model_label}"

    run = optimizer_module.OptimizationRun(
        fixture=fixture,
        task_prompt=task_prompt,
        adapter=adapter,
        limits=limits,
        controls=model_io_module.Controls(lambda: False),
        model_identity=model_identity,
        run_id=str(uuid.uuid4()),
    )
    optimizer_module.reset_consumed_holdouts()
    run.proposal_source = proposals_module.ReflectionProposalSource(
        adapter=adapter,
        budget=run.budget,
        controls=run.controls,
        max_candidates=limits.max_candidates_per_round,
        max_output_chars=limits.max_output_chars,
        max_attempts=limits.max_retries_per_call + 1,
    )
    result = run.execute()
    out_dir = (
        Path(args.out)
        if args.out
        else root / "backend" / "prompt_optimization" / "_artifacts" / result.run_id
    )
    paths = report_module.export_artifacts(result, out_dir)
    print(
        f"status={result.status} qualified={result.qualified} "
        f"recommendation={result.recommendation}"
    )
    print(f"candidate={paths['candidate']}")
    print(f"report={paths['report']}")
    return 0 if result.status == optimizer_module.STATUS_COMPLETED else 1


def main(argv: Optional[List[str]] = None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
