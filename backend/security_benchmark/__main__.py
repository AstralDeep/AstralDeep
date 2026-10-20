"""Routes signed-turn security qualification and legacy diagnostic ablations.
Only the explicit real-dispatch command can produce a qualification report.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import List, Optional

from security_benchmark.config import RunConfig
from security_benchmark.report import compute_stats
from security_benchmark.runner import check_regression, run


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="security_benchmark",
                                description="ASR harness for the Astral trust envelope")
    p.add_argument("--mode", default="synthetic",
                   choices=["synthetic", "in_process", "external", "chained_real"],
                   help="legacy diagnostic ablation mode; use --qualify-real-dispatch "
                        "for the signed-turn security gate")
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
    p.add_argument("--qualify-real-dispatch", action="store_true",
                   help="run the signed-turn fixture gate; requires local PostgreSQL")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.qualify_real_dispatch:
        from security_benchmark.drivers.real_dispatch import run_real_qualification
        from security_benchmark.qualification_report import (
            build_report, candidate_identity, validate_report, write_report,
        )

        root = Path(__file__).resolve().parents[2]
        output = Path(args.out or "backend/security_benchmark/_artifacts")
        if not output.is_absolute():
            output = root / output
        path = output / "real-dispatch-qualification.json"
        candidate = None
        report_written = False
        try:
            candidate = candidate_identity(root)
            evidence, teardown_verified = asyncio.run(run_real_qualification(args.run_id))
            report = build_report(candidate, evidence, teardown_verified=teardown_verified)
            write_report(path, report)
            report_written = True
            validate_report(report, expected_candidate=candidate)
        except Exception as exc:
            if not report_written:
                try:
                    write_report(path, {"schema": "astraldeep.security-qualification/v1",
                        "evidence_class": "real_dispatch_scripted_proposer",
                        "candidate": candidate, "status": "incomplete", "cases": [],
                        "errors": [f"execution_failed:{type(exc).__name__}"],
                        "teardown": {"status": "unverified"}})
                except OSError:
                    pass
            print(f"real-dispatch qualification incomplete: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return 1
        print(f"real-dispatch qualification passed: {path}")
        return 0
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
