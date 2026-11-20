"""The astral.cad-kernel tool script, version 1.1.0. The sandbox runs it as
python tools/cad_run.py with the call arguments as JSON on stdin:
{"script": "<build123d code>"}. Offline; exported files go to $VESSEL_OUTBOX."""
import json
import os
import sys

MAX_SCRIPT_BYTES = 256 * 1024

out = os.environ.get("VESSEL_OUTBOX")
if not out:
    sys.exit("cad: the sandbox gave this call no output folder; files can only be written inside a turn workspace")
if not os.path.isdir(out):
    os.makedirs(out, exist_ok=True)

try:
    args = json.loads(sys.stdin.read() or "{}")
except json.JSONDecodeError as exc:
    sys.exit(f"cad: the call arguments are not JSON: {exc}")
script = args.get("script") if isinstance(args, dict) else None
if not isinstance(script, str) or not script.strip():
    sys.exit("cad: script must be a non-empty string of Python using build123d")
if len(script.encode("utf-8")) > MAX_SCRIPT_BYTES:
    sys.exit(f"cad: the script is over {MAX_SCRIPT_BYTES // 1024} KB")

PRELUDE = '''
import os as _os, re as _re

_OUT = _os.environ.get("VESSEL_OUTBOX")
if not _os.path.isdir(_OUT):
    _os.makedirs(_OUT, exist_ok=True)
import build123d as _bd


def _path(name, extension):
    if not isinstance(name, str) or not _re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,94}", name):
        raise ValueError("the export name must be 1-95 letters, digits, dots, dashes or underscores")
    return _os.path.join(_OUT, f"{name}.{extension}")


def _one(shapes):
    if isinstance(shapes, (list, tuple)):
        return _bd.Compound(children=list(shapes))
    return shapes


def export_step(shapes, name):
    if not _bd.export_step(_one(shapes), _path(name, "step")):
        raise RuntimeError("the STEP export failed")


def export_stl(shapes, name, tolerance=0.1, angular_tolerance=0.2):
    if not _bd.export_stl(_one(shapes), _path(name, "stl"), tolerance=tolerance,
                          angular_tolerance=angular_tolerance):
        raise RuntimeError("the STL export failed")


def export_glb(shapes, name, tolerance=0.1, angular_tolerance=0.2):
    if not _bd.export_gltf(_one(shapes), _path(name, "glb"), binary=True, linear_deflection=tolerance,
                           angular_deflection=angular_tolerance):
        raise RuntimeError("the GLB export failed")
'''

try:
    exec(compile(PRELUDE + "\n" + script, "<cad model script>", "exec"), {"__name__": "__cad_model__"})
except BaseException:
    import traceback

    traceback.print_exc()
    sys.exit(1)
