"""Puts the caller ticket reference and the preserved 105 reference helpers on the import path for the vector tests."""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
MESH_REF = HERE.parents[2] / "105-decentralized-personal-mesh/contracts/reference"
for path in (HERE, MESH_REF):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
