"""Command line entry for callers that need a confined process, run as python -I -B <dir>/astral_exec/run.py followed by launch or conformance.
Under -I the script's own folder is not on sys.path, so it inserts only its parent folder and then dispatches to launch.py or to the conformance suite.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

USAGE = "usage: run.py {launch,conformance} ..."


def main(args: Sequence[str]) -> int:
    command, rest = (args[0], list(args[1:])) if args else ("", [])
    if command == "launch":
        from astral_exec.launch import main as launch

        return launch(rest)
    if command == "conformance":
        print(
            "run.py conformance: the conformance suite is not yet available",
            file=sys.stderr,
        )
        return 2
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sys.exit(main(sys.argv[1:]))
