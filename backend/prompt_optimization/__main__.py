"""Runs the deterministic offline workbench and exports inert review artifacts.
Final exposure receipts use configured AstralPlane; no live-model or credential input is accepted.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

from prompt_optimization import budget as budget_module
from prompt_optimization import model_io as model_io_module
from prompt_optimization import optimizer as optimizer_module
from prompt_optimization import proposals as proposals_module
from prompt_optimization import report as report_module
from prompt_optimization import task as task_module
from prompt_optimization.fixture import FixtureError, build_fixture, load_holdout
from prompt_optimization.holdout import HoldoutReuseError, PlaneHoldoutSeals

DEFAULT_MODEL_LABEL = "scripted-double-v1"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="prompt_optimization",
        description="Bounded scripted offline prompt-optimization pilot; inert review artifacts only",
    )
    parser.add_argument("--out", default=None, help="artifact output directory")
    parser.add_argument("--rounds", type=int, default=4, help="maximum search rounds")
    parser.add_argument("--holdout", default=None, help="explicit new synthetic final-holdout JSON file")
    parser.add_argument("--model-label", default=DEFAULT_MODEL_LABEL, help="scripted double identity for the report")
    return parser.parse_args(argv)


def build_limits(rounds: int) -> budget_module.BudgetLimits:
    return budget_module.BudgetLimits(
        max_reflection_calls=6, max_evaluation_calls=64, max_total_tokens=120000,
        max_retries_per_call=1, max_output_chars=4000, max_candidates_per_round=4,
        max_rounds=max(1, min(int(rounds), 8)),
    )


@contextmanager
def configured_holdout_seals(root: Path):
    from orchestrator.plane_composition import compose_plane_from_environment

    database_url = os.getenv("ASTRAL_PROMPT_OPTIMIZATION_DATABASE_URL", "").strip()
    if not database_url:
        raise ValueError("an explicit qualification database is required")
    state_root = Path(os.getenv("ASTRAL_PROMPT_OPTIMIZATION_STATE_ROOT", "") or
                      root / "backend" / "prompt_optimization" / "_artifacts" / "plane").absolute()
    composition = compose_plane_from_environment(root / "config" / "astral-composition.json", environ={
        **os.environ, "DATABASE_URL": database_url,
        "ATTACHMENT_UPLOAD_ROOT": str(state_root / "blobs"),
        "PERSONAL_AGENT_ARTIFACT_ROOT": str(state_root / "agent-bundles"),
        "ASTRALPLANE_IDENTITY": "prompt-optimization-scripted",
    })
    try:
        yield PlaneHoldoutSeals(composition.runtime)
    finally:
        composition.close()


def run(args: argparse.Namespace) -> int:
    root = repo_root()
    try:
        task_prompt = task_module.load_task_prompt(root)
        fixture = load_holdout(args.holdout) if args.holdout else build_fixture()
        limits = build_limits(args.rounds)
        variants = proposals_module.scripted_variants(task_prompt.rules)
        adapter = model_io_module.ProviderDouble(variants=variants)
        with configured_holdout_seals(root) as seals:
            run = optimizer_module.OptimizationRun(
                fixture=fixture, task_prompt=task_prompt, adapter=adapter, holdout_seals=seals,
                limits=limits, controls=model_io_module.Controls(lambda: False),
                model_identity=f"scripted:{args.model_label}", run_id=str(uuid.uuid4()),
            )
            run.proposal_source = proposals_module.ReflectionProposalSource(
                adapter=adapter, budget=run.budget, controls=run.controls,
                max_candidates=limits.max_candidates_per_round,
                max_output_chars=limits.max_output_chars,
                max_attempts=limits.max_retries_per_call + 1,
            )
            result = run.execute()
        out_dir = Path(args.out) if args.out else root / "backend" / "prompt_optimization" / "_artifacts" / result.run_id
        paths = report_module.export_artifacts(result, out_dir)
    except (FixtureError, HoldoutReuseError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except Exception as error:
        print(f"workbench unavailable: {type(error).__name__}", file=sys.stderr)
        return 2
    print(f"scripted_status={result.status} controls_qualified={result.qualified} "
          f"scripted_metric_recommendation={result.recommendation} actual_model_qualified=False")
    print(f"candidate={paths['candidate']}")
    print(f"report={paths['report']}")
    return 0 if result.status == optimizer_module.STATUS_COMPLETED else 1


def main(argv: Optional[List[str]] = None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
