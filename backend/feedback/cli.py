"""Server-only CLI for the feedback subsystem.

Subcommands:

* ``compute-quality`` — runs one quality-signal computation cycle now.
* ``generate-proposals`` — runs the proposal generator once now.
* ``pre-pass-once`` — runs the synthesizer's loop pre-pass once now.

Invoked from the orchestrator container, e.g.::

    docker exec astraldeep bash -c \
        "cd /app/backend && python -m feedback.cli compute-quality"
"""
from __future__ import annotations

import argparse
import atexit
import asyncio
import logging
from pathlib import Path
import sys
from typing import Optional


_COMPOSITION = None


def _close_plane_runtime() -> None:
    global _COMPOSITION
    if _COMPOSITION is not None:
        _COMPOSITION.close()
        _COMPOSITION = None


atexit.register(_close_plane_runtime)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _build_repo():
    # Lazy imports — avoids pulling FastAPI / orchestrator deps when unused.
    global _COMPOSITION
    from orchestrator.plane_composition import compose_plane_from_environment
    from .repository import FeedbackRepository

    if _COMPOSITION is None:
        manifest = Path(__file__).resolve().parents[2] / "config" / "astral-composition.json"
        _COMPOSITION = compose_plane_from_environment(manifest)
    return FeedbackRepository(
        plane_runtime=_COMPOSITION.runtime,
        plane_repositories=_COMPOSITION.repositories,
    )


async def _cmd_compute_quality(args: argparse.Namespace) -> int:
    from .quality import compute_for_window
    repo = _build_repo()
    snaps = await compute_for_window(repo)
    print(f"computed {len(snaps)} quality snapshots")
    for s in snaps:
        print(f"  {s.agent_id}/{s.tool_name}: status={s.status} "
              f"dispatches={s.dispatch_count} fail_rate={s.failure_rate:.2%} "
              f"neg_fb_rate={s.negative_feedback_rate:.2%}")
    return 0


async def _cmd_generate_proposals(args: argparse.Namespace) -> int:
    from .proposals import generate_for_underperforming
    repo = _build_repo()
    proposals = await generate_for_underperforming(repo)
    print(f"generated {len(proposals)} proposals")
    for p in proposals:
        print(f"  proposal {p.id} for {p.agent_id}/{p.tool_name} "
              f"-> {p.artifact_path}")
    return 0


async def _cmd_pre_pass_once(args: argparse.Namespace) -> int:
    # The pre-pass runs inside the synthesizer's loop. Calling it here is an
    # ops escape hatch to force a screening pass over recent feedback now.
    try:
        from orchestrator.knowledge_synthesis import run_safety_pre_pass_once
    except ImportError:
        print("synthesizer pre-pass entrypoint not available", file=sys.stderr)
        return 2
    repo = _build_repo()
    flagged = await run_safety_pre_pass_once(repo)
    print(f"loop pre-pass flagged {flagged} record(s)")
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m feedback.cli")
    parser.add_argument("--verbose", "-v", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("compute-quality", help="Compute one quality-signal cycle now")
    sub.add_parser("generate-proposals", help="Generate proposals for currently-flagged tools")
    sub.add_parser("pre-pass-once", help="Run the synthesizer loop pre-pass once")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    if args.cmd == "compute-quality":
        return asyncio.run(_cmd_compute_quality(args))
    if args.cmd == "generate-proposals":
        return asyncio.run(_cmd_generate_proposals(args))
    if args.cmd == "pre-pass-once":
        return asyncio.run(_cmd_pre_pass_once(args))
    return 1


if __name__ == "__main__":
    sys.exit(main())
