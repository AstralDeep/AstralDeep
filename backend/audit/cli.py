"""
Operator-only audit log CLIs.

Two commands:

* ``verify-chain --user-id <id>`` — walk a single user's hash chain
  forward from genesis and report the first ``event_id`` whose
  recomputed digest does not match the stored one. Exit code 0 when
  the chain is clean; non-zero when tamper is detected.
* ``purge-expired [--horizon-days N]`` — delete rows older than the
  retention horizon (default 6 years per FR-012). The protective
  trigger requires the ``audit.allow_purge`` GUC, which the repository
  sets internally.

Run::

    python -m audit.cli verify-chain --user-id <id>
    python -m audit.cli purge-expired [--horizon-days 2192]

These commands are NOT exposed via REST. They live on the server only
and are explicitly out of the user-facing audit-log read surface.
"""
from __future__ import annotations

import argparse
import atexit
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow ``python -m audit.cli`` from the backend directory
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _make_repo():
    from audit.repository import AuditRepository
    from orchestrator.plane_composition import compose_plane_from_environment

    global _composition
    if _composition is None:
        manifest = BACKEND_DIR.parent / "config" / "astral-composition.json"
        _composition = compose_plane_from_environment(manifest)
    return AuditRepository(
        plane_runtime=_composition.runtime,
        plane_repositories=_composition.repositories,
    )


_composition = None


def _close_plane_runtime() -> None:
    global _composition
    if _composition is not None:
        _composition.close()
        _composition = None


atexit.register(_close_plane_runtime)


def cmd_verify_chain(args: argparse.Namespace) -> int:
    repo = _make_repo()
    bad = repo.verify_chain(args.user_id)
    if bad is None:
        print(f"OK: chain verified for user {args.user_id}")
        return 0
    print(f"TAMPER: chain broken at event_id {bad} for user {args.user_id}", file=sys.stderr)
    return 2


def cmd_purge_expired(args: argparse.Namespace) -> int:
    repo = _make_repo()
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.horizon_days)
    deleted = repo.purge_older_than(args.user_id, cutoff)
    print(f"purged {deleted} audit row(s) older than {cutoff.isoformat()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(prog="audit.cli", description="Audit log operator commands")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_verify = sub.add_parser("verify-chain", help="Verify a user's audit chain")
    p_verify.add_argument("--user-id", required=True, help="actor_user_id whose chain to verify")
    p_verify.set_defaults(func=cmd_verify_chain)

    p_purge = sub.add_parser("purge-expired", help="Delete rows past the retention horizon")
    p_purge.add_argument(
        "--user-id",
        required=True,
        help="actor_user_id whose authenticated chain prefix may be pruned",
    )
    p_purge.add_argument(
        "--horizon-days",
        type=int,
        default=int(os.getenv("AUDIT_RETENTION_DAYS", "2192")),  # ~6 years
        help="Retention horizon in days (default 2192 = 6 years)",
    )
    p_purge.set_defaults(func=cmd_purge_expired)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
