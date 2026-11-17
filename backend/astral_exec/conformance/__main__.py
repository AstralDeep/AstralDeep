"""Runs the conformance suite as python -m astral_exec.conformance, with the same options as run.py conformance."""

import sys

from . import main

sys.exit(main(sys.argv[1:]))
