"""Routes the legacy diagnostic ablation benchmarks; none of them produces a qualification report."""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from security_benchmark.config import RunConfig
from security_benchmark.report import compute_stats
from security_benchmark.runner import check_regression, run


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="security_benchmark",
                                description="ASR harness for the Astral trust envelope")
    p.add_argument("--mode", default="synthetic",
                   choices=["synthetic", "in_process", "external", "chained_real"],
                   help="legacy diagnostic ablation mode")
    p.add_argument("--benchmark", action="append", dest="benchmarks", default=[],
                   help="benchmark id (repeatable): agentdojo | asb | injecagent | chained")
    p.add_argument("--model", default="scripted-deterministic",
                   help="model label recorded in the run key (ASR is comparable only within a fixed model)")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--limit", type=int, default=None, help="cap cases per benchmark (CI budget)")
    p.add_argument("--out", default=None, help="artifacts root (gitignored)")
    p.add_argument("--run-id", default=None)
    p.add_argument("--stamp", default="local")
    p.add_argument("--asr-threshold", type=float, default=None,
                   help="diagnostic sample-ASR threshold; not release qualification")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    config = RunConfig(
        mode=args.mode,
        model=args.model,
        benchmarks=args.benchmarks or ["agentdojo"],
        seed=args.seed,
        limit=args.limit,
        run_id=args.run_id,
        asr_threshold=args.asr_threshold,
    )
    if args.out:
        config.artifacts_root = args.out

    records, report_path = run(config, stamp=args.stamp)
    print(f"wrote ASR report → {report_path}")
    for rec in records:
        labels = list(rec.adjudications.keys())
        if not labels:
            continue
        base = compute_stats(rec.adjudications[labels[0]]).asr
        full = compute_stats(rec.adjudications[labels[-1]]).asr
        print(f"  {rec.key.benchmark}: baseline ASR={base:.3f} → full-envelope ASR={full:.3f}")

    if config.asr_threshold is not None:
        offenders = check_regression(records, config.asr_threshold)
        if offenders:
            print("CI REGRESSION GATE TRIPPED:")
            for o in offenders:
                print(f"  {o}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
