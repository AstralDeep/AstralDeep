# The cad tool: argument limits, the outbox contract of its prelude, and a confined end-to-end build
# when the sandbox offers Landlock and the build123d kernel.

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from test_deploy import landlock

from astralvessel import files as outputs
from astralvessel import sandbox, tools
from astralvessel.manifest import load_package

KEY = "c" * 32


def build123d_available() -> bool:
    try:
        import build123d  # noqa: F401
    except ImportError:
        return False
    return True


needs_kernel = pytest.mark.skipif(not build123d_available(), reason="the build123d kernel is not installed")


async def test_cad_refuses_a_missing_or_empty_script():
    out, failed = await tools.run_sandbox_tool("cad", {}, workdir=".", timeout=10, declared={},
                                               package_root=".")
    assert failed and "non-empty string" in out
    out, failed = await tools.run_sandbox_tool("cad", {"script": "   "}, workdir=".", timeout=10,
                                               declared={}, package_root=".")
    assert failed and "non-empty string" in out


async def test_cad_refuses_an_oversized_script():
    script = "x = 1  # " + "p" * (tools.MAX_CAD_SCRIPT_BYTES + 1)
    out, failed = await tools.run_sandbox_tool("cad", {"script": script}, workdir=".", timeout=10,
                                               declared={}, package_root=".")
    assert failed and "over 256 KB" in out


async def test_cad_without_a_turn_workspace_has_no_outbox():
    out, failed = await tools.run_sandbox_tool("cad", {"script": "import build123d"}, workdir=".",
                                               timeout=30, declared={}, package_root=".")
    assert failed and "no output folder" in out


def run_prelude(script: str, outbox: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("VESSEL_")}
    env.update(VESSEL_OUTBOX=outbox, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, "-I", "-"], input=(tools.CAD_PRELUDE + "\n" + script).encode(),
                          env=env, capture_output=True, timeout=180)


@needs_kernel
def test_the_cad_prelude_exports_step_stl_and_glb_into_the_outbox(tmp_path):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    script = (
        "import build123d as bd\n"
        "part = bd.Box(10, 20, 5) - bd.Cylinder(radius=2, height=10)\n"
        "export_step(part, 'bracket')\n"
        "export_stl(part, 'bracket', tolerance=0.05)\n"
        "export_glb(part, 'bracket')\n"
    )
    done = run_prelude(script, str(outbox))
    assert done.returncode == 0, done.stdout.decode() + done.stderr.decode()
    media = {name: outputs.check(name, (outbox / name).read_bytes(), ["step", "stl", "glb"])
             for name in sorted(os.listdir(outbox))}
    assert media == {"bracket.glb": ("model/gltf-binary", ""),
                     "bracket.step": ("application/step", ""),
                     "bracket.stl": ("model/stl", "")}


@needs_kernel
def test_the_cad_prelude_rejects_names_that_leave_the_outbox(tmp_path):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    script = ("import build123d as bd\n"
              "export_stl(bd.Box(1, 1, 1), '../escape')\n")
    done = run_prelude(script, str(outbox))
    assert done.returncode != 0 and "export name" in done.stderr.decode()
    assert os.listdir(outbox) == []


@needs_kernel
@landlock
async def test_a_confined_cad_call_returns_its_model_files(package_dir, tmp_path):
    manifest = package_dir / "vessel.toml"
    manifest.write_text(manifest.read_text() + '\n[output]\nfiles = ["step", "stl", "glb"]\n')
    package = load_package(package_dir)
    scratch = tmp_path / "calls"
    scratch.mkdir()
    runner = sandbox.LocalRunner(package, sandbox.confinement_for(package, str(scratch)))
    out, failed = await runner.run("cad", {"script": (
        "import build123d as bd\n"
        "part = bd.Box(10, 10, 10)\n"
        "export_step(part, 'cube')\n"
        "export_stl(part, 'cube')\n"
        "print('built')\n")}, KEY)
    assert not failed and "built" in out, out
    found = runner.take_files(KEY)
    assert [(f.name, f.media_type) for f in found] == [("cube.step", "application/step"),
                                                       ("cube.stl", "model/stl")]
    await runner.end_turn(KEY)
    assert list(scratch.iterdir()) == []


def test_the_cad_tool_runs_at_execute_scope():
    from astralvessel.manifest import BUILTIN_SCOPES

    assert BUILTIN_SCOPES["cad"] == "tools:execute"
