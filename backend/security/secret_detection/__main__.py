"""CLI entry point for running secret detection benchmark evaluation."""

from __future__ import annotations

import argparse
import os
import sys

from security.secret_detection.evaluator import BenchmarkEvaluator
from security.secret_detection.report import (
    export_report_json,
    export_report_markdown,
    format_report_markdown,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate contextual secret patterns benchmark")
    parser.add_argument(
        "--out",
        dest="out_dir",
        default=os.path.join(os.path.dirname(__file__), "_artifacts"),
        help="Output directory for generated evaluation artifacts",
    )
    args = parser.parse_args()

    evaluator = BenchmarkEvaluator()
    report = evaluator.evaluate()

    json_path = os.path.join(args.out_dir, "secret-detector-evaluation-report.json")
    md_path = os.path.join(args.out_dir, "secret-detector-evaluation-report.md")

    export_report_json(report, json_path)
    export_report_markdown(report, md_path)

    print(format_report_markdown(report))
    print(f"\nArtifacts successfully written to:\n  {json_path}\n  {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
