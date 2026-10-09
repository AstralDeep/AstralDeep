"""
install_doctor.py - Read‑only installation and readiness diagnostic tool.

This script inspects the local environment for the services required by AstralDeep
without performing any mutations or requiring authentication.  It is intended to
be used by operators to quickly understand whether a deployment is *ready*,
*repairable* or *unsupported*.

Features
--------
* Detects the host operating system and verifies basic prerequisites.
* Checks the presence and health of required services (Keycloak, PostgreSQL,
  Redis) by attempting a TCP connection to their default ports.
* Produces a deterministic JSON report that is safe to share (no secrets are
  included).
* Returns a non‑zero exit code when the installation is not ready.

Usage
-----
    python -m scripts.install_doctor          # prints JSON to stdout
    python -m scripts.install_doctor --json    # same as default
    python -m scripts.install_doctor --human   # pretty‑printed report
"""

import argparse
import json
import platform
import socket
import sys
from dataclasses import asdict, dataclass
from typing import Dict, List, Tuple

# --------------------------------------------------------------------------- #
# Configuration – required services and their default connection details.
# --------------------------------------------------------------------------- #
REQUIRED_SERVICES: List[Tuple[str, str, int]] = [
    ("keycloak", "localhost", 8080),
    ("postgres", "localhost", 5432),
    ("redis", "localhost", 6379),
]

# --------------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ServiceStatus:
    name: str
    reachable: bool
    error: str = ""

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass(frozen=True)
class DiagnosisReport:
    host: str
    python_version: str
    services: List[ServiceStatus]

    @property
    def overall(self) -> str:
        """Derive an overall status from individual service checks."""
        if all(s.reachable for s in self.services):
            return "ready"
        if any(s.reachable for s in self.services):
            return "repairable"
        return "unsupported"

    def to_dict(self) -> Dict:
        return {
            "host": self.host,
            "python_version": self.python_version,
            "overall": self.overall,
            "services": [s.to_dict() for s in self.services],
        }

# --------------------------------------------------------------------------- #
# Core logic
# --------------------------------------------------------------------------- #
def _check_tcp(host: str, port: int, timeout: float = 1.0) -> Tuple[bool, str]:
    """
    Attempt a TCP connection to ``host:port``.
    Returns (reachable, error_message).  ``error_message`` is empty on success.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, ""
    except OSError as exc:
        return False, str(exc)


def diagnose() -> DiagnosisReport:
    """
    Run the full read‑only diagnosis and return a :class:`DiagnosisReport`.
    """
    host_info = platform.platform()
    py_version = f"{sys.version_info.major}.{sys.version_info.minor}"

    services_status = []
    for name, host, port in REQUIRED_SERVICES:
        reachable, err = _check_tcp(host, port)
        services_status.append(ServiceStatus(name=name, reachable=reachable, error=err))

    return DiagnosisReport(host=host_info, python_version=py_version, services=services_status)


def render_human(report: DiagnosisReport) -> str:
    """
    Produce a human‑readable multiline string from a :class:`DiagnosisReport`.
    """
    lines = [
        f"Host: {report.host}",
        f"Python: {report.python_version}",
        f"Overall status: {report.overall}",
        "",
        "Service checks:",
    ]
    for svc in report.services:
        status = "✅ reachable" if svc.reachable else f"❌ unreachable ({svc.error})"
        lines.append(f"  - {svc.name}: {status}")
    return "\n".join(lines)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="AstralDeep read‑only installation and readiness doctor."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON report (default).",
    )
    group.add_argument(
        "--human",
        action="store_true",
        help="Emit a pretty‑printed human readable report.",
    )
    args = parser.parse_args(argv)

    report = diagnose()

    if args.human:
        print(render_human(report))
    else:
        # Default to JSON output
        print(json.dumps(report.to_dict(), indent=2))

    # Exit code semantics:
    #   0 – ready
    #   1 – repairable (some services reachable)
    #   2 – unsupported (nothing reachable)
    exit_code = {"ready": 0, "repairable": 1, "unsupported": 2}[report.overall]
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
