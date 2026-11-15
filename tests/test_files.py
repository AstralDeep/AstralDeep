# Files from tools: outbox collection and its checks, and the sandbox hop that carries them to the executor.

from __future__ import annotations

import base64
import os

import httpx
from test_deploy import landlock

from astralvessel import files as outputs
from astralvessel import sandbox
from astralvessel.manifest import load_package

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
KEY = "d" * 32


def allow_files(package_dir, kinds='["png", "csv"]'):
    manifest = package_dir / "vessel.toml"
    manifest.write_text(manifest.read_text() + f"\n[output]\nfiles = {kinds}\n")


def test_the_outbox_keeps_only_allowed_valid_files_and_is_emptied(tmp_path):
    box = tmp_path / "outbox"
    box.mkdir()
    (box / "chart.png").write_bytes(PNG)
    (box / "table.csv").write_text("county,rate\nA,1\n")
    (box / "fake.png").write_text("not a png")
    (box / "drawing.svg").write_text("<svg></svg>")
    (box / "notes.txt").write_text("x")
    (box / ".hidden.csv").write_text("x")
    (box / "big.csv").write_bytes(b"a" * (outputs.MAX_FILE_BYTES + 1))
    (box / "link.csv").symlink_to(tmp_path / "secret")
    (tmp_path / "secret").write_text("secret")
    (box / "folder.csv").mkdir()
    os.mkfifo(box / "pipe.csv")
    found, notes = outputs.collect(str(box), ["png", "csv"])
    assert [(f.name, f.media_type) for f in found] == [("chart.png", "image/png"), ("table.csv", "text/csv")]
    assert found[0].data == PNG and len(notes) == 8 and os.listdir(box) == []
    text = "\n".join(notes)
    assert "fake.png: not a valid .png" in text and "drawing.svg: this agent may not" in text
    assert "big.csv: larger than 4 MB" in text and "link.csv: not a regular" in text and "secret" not in text[-60:]
    assert (tmp_path / "secret").read_text() == "secret"


def test_the_outbox_returns_at_most_four_files_per_call(tmp_path):
    for index in range(6):
        (tmp_path / f"t{index}.csv").write_text("a,b\n")
    found, notes = outputs.collect(str(tmp_path), ["csv"])
    assert len(found) == outputs.MAX_FILES_PER_CALL and len(notes) == 2 and "at most 4" in notes[0]


def test_a_flooded_outbox_gives_the_model_a_short_summary(tmp_path):
    for index in range(50):
        (tmp_path / f"t{index}.txt").write_text("x")
    found, notes = outputs.collect(str(tmp_path), ["csv"])
    assert found == [] and len(notes) == outputs.MAX_NOTES and notes[-1] == "[41 more entries not attached]"
    assert os.listdir(tmp_path) == []


def test_an_outbox_swapped_for_a_link_reads_and_deletes_nothing_outside(tmp_path):
    victim = tmp_path / "victim"
    (victim / "sub").mkdir(parents=True)
    (victim / "secret.csv").write_text("password,hunter2\n")
    (victim / "keep.txt").write_text("x")
    workspace = tmp_path / "turn"
    workspace.mkdir()
    (workspace / "outbox").symlink_to(victim)
    found, notes = outputs.collect(str(workspace / "outbox"), ["csv"])
    assert found == [] and "not a folder" in notes[0] and not (workspace / "outbox").exists()
    assert sorted(os.listdir(victim)) == ["keep.txt", "secret.csv", "sub"]


@landlock
async def test_a_call_that_replaces_its_outbox_does_not_break_the_next_call(package_dir, tmp_path):
    allow_files(package_dir)
    package = load_package(package_dir)
    scratch = tmp_path / "calls"
    scratch.mkdir()
    runner = sandbox.LocalRunner(package, sandbox.confinement_for(package, str(scratch)))
    out, failed = await runner.run("bash", {"command": "rmdir $VESSEL_OUTBOX && echo x > $VESSEL_OUTBOX"}, KEY)
    assert not failed and "not a folder" in out
    await runner.run("bash", {"command": "rmdir $VESSEL_OUTBOX && ln -s /nowhere $VESSEL_OUTBOX"}, KEY)
    out, failed = await runner.run("bash", {"command": "printf 'a\\n' > $VESSEL_OUTBOX/t.csv && echo ok"}, KEY)
    assert not failed and "ok" in out and [f.name for f in runner.take_files(KEY)] == ["t.csv"]
    await runner.end_turn(KEY)


def test_the_executor_checks_files_again_when_they_arrive():
    wire = [outputs.ToolFile("chart.png", "image/png", PNG).wire(),
            {"name": "../../x.png", "data": base64.b64encode(PNG).decode()},
            {"name": "evil.png", "data": base64.b64encode(b"<script>").decode()},
            {"name": "t.csv", "data": "not base64!"},
            {"name": "t.pdf", "data": base64.b64encode(b"%PDF-1.7").decode()}, "junk"]
    assert [f.name for f in outputs.from_wire(wire, ["png", "csv"])] == ["chart.png"]
    assert outputs.from_wire({"not": "a list"}, ["png"]) == []


@landlock
async def test_a_confined_call_returns_files_from_its_outbox(package_dir, tmp_path):
    allow_files(package_dir)
    package = load_package(package_dir)
    scratch = tmp_path / "calls"
    scratch.mkdir()
    runner = sandbox.LocalRunner(package, sandbox.confinement_for(package, str(scratch)))
    out, failed = await runner.run("bash", {"command": "printf 'a,b\\n1,2\\n' > $VESSEL_OUTBOX/t.csv; "
                                                       "echo x > $VESSEL_OUTBOX/n.txt; echo done"}, KEY)
    assert not failed and out.startswith("[not attached n.txt") and "done" in out
    [found] = runner.take_files(KEY)
    assert (found.name, found.data) == ("t.csv", b"a,b\n1,2\n") and runner.take_files(KEY) == []
    out, _ = await runner.run("bash", {"command": "ls $VESSEL_OUTBOX | wc -l"}, KEY)
    assert out.startswith("0")
    await runner.end_turn(KEY)
    assert list(scratch.iterdir()) == []


async def test_the_sandbox_server_sends_collected_files(package_dir, monkeypatch):
    allow_files(package_dir)
    app = sandbox.sandbox_app(str(package_dir), token="t" * 16, confine=False)
    original = sandbox.LocalRunner.take_files

    def take(self, workspace):
        return [outputs.ToolFile("chart.png", "image/png", PNG)] if workspace == KEY else original(self, workspace)

    monkeypatch.setattr(sandbox.LocalRunner, "take_files", take)
    remote = sandbox.RemoteRunner("http://sb", "t" * 16, 5, ["png"])
    remote._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sb",
                                       headers={"Authorization": "Bearer " + "t" * 16})
    out, failed = await remote.run("bash", {"command": "echo hi"}, KEY)
    assert not failed and [f.data for f in remote.take_files(KEY)] == [PNG]
    await remote.run("bash", {"command": "echo hi"}, KEY)
    await remote.end_turn(KEY)
    assert remote.take_files(KEY) == []
    await remote.close()
