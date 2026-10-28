"""CLI entry point for `python -m personalization.semantic_pilot`: runs the read-only
qualification pilot over the frozen synthetic corpus with the selected provider profile,
prints or writes the candidate-bound report, and never changes production retrieval
defaults or durable state.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from .bounds import Bounds
from .report import write_report
from .runner import PilotConfig, PilotError, RunCancelled, run_pilot

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG_ERROR = 2
EXIT_CANCELLED = 3


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="personalization.semantic_pilot",
        description=(
            "Read-only owner-scoped semantic memory retrieval qualification pilot: "
            "compares the actual lexical baseline, bounded semantic candidates, and "
            "optional reranking over frozen synthetic non-PHI cases; exports a "
            "candidate-bound report with an honest adoption/rejection decision"
        ),
    )
    parser.add_argument(
        "--profile",
        choices=("offline-diagnostic", "scripted-double", "tei-http"),
        default="offline-diagnostic",
        help="provider profile to execute (default: offline diagnostic runtime)",
    )
    parser.add_argument(
        "--run-config",
        default=None,
        help=(
            "JSON file with the operator-provided run configuration required by the "
            "tei-http profile (base_url, embed_model/revision, tokenizer_revision, "
            "dimension, license, embedding_runtime_digest, embedding_assets_digest; "
            "reranking requires rerank_base_url, rerank_model, "
            "rerank_revision, rerank_tokenizer_revision, "
            "rerank_runtime_digest, rerank_assets_digest; optional api_key, rerank_api_key, "
            "allow_private_endpoint); never committed, never echoed"
        ),
    )
    parser.add_argument(
        "--attestation",
        default=None,
        help=(
            "JSON file attesting a bounded manual local run of the pinned model/runtime "
            "against this corpus (profile, model_revision, provider_identity_digest, "
            "corpus_digest, performed_at, "
            "operator); without it tei-http stays unqualified"
        ),
    )
    parser.add_argument(
        "--k", type=int, default=None,
        help="force the candidate budget k instead of selecting it on the selection partition",
    )
    parser.add_argument(
        "--report", default=None,
        help="write the report JSON to this path (default: print to stdout)",
    )
    parser.add_argument(
        "--deadline-seconds", type=float, default=None,
        help="optional wall-clock deadline for manual runs; CI runs stay deadline-free",
    )
    return parser.parse_args(argv)


def _load_json(path: Optional[str], *, what: str):
    if path is None:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read {what} from {path}: {exc}") from None


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        run_config = _load_json(args.run_config, what="run configuration")
        attestation = _load_json(args.attestation, what="attestation")
        bounds = Bounds(runtime_deadline_seconds=args.deadline_seconds)
        config = PilotConfig(
            profile=args.profile,
            run_config=run_config,
            bounds=bounds,
            forced_k=args.k,
            attestation=attestation,
        )
        report = run_pilot(config)
    except RunCancelled:
        print("pilot run cancelled; no report written", file=sys.stderr)
        return EXIT_CANCELLED
    except PilotError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except Exception as exc:
        print(f"pilot failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if args.report:
        try:
            write_report(report, args.report, bounds=config.bounds)
        except (ValueError, OSError) as exc:
            print(f"cannot write report: {exc}", file=sys.stderr)
            return EXIT_ERROR
        print(f"report written to {args.report}")
        recommendation = report["recommendation"]["recommendation"]
        print(f"recommendation: {recommendation}")
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())  # pragma: no cover
