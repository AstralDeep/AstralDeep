"""Tests for the supervised mailbox child: argument building, announcement parsing, startup, restart after a crash and orderly stop, using small shell scripts in place of the real binary."""

from __future__ import annotations

import asyncio
import os
import stat

import pytest

from orchestrator import node_mailbox as nm


def script(tmp_path, body):
    path = tmp_path / "fake-mailbox"
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


ANNOUNCE = 'echo "mailbox id box-1"\necho \'address {"id":"box-1"}\'\n'


def test_settings_arguments_and_environment(tmp_path):
    settings = nm.MailboxSettings.from_env(
        {"ASTRAL_NODE_MAILBOX_BIND": "127.0.0.1:7000, ", "ASTRAL_NODE_MAILBOX_MAX_AGE_DAYS": "3", "ASTRAL_NODE_MAILBOX_MAX_MIB": "x"},
        loopback=True,
    )
    args = settings.arguments(tmp_path)
    assert args[:5] == ["serve", "--data-dir", str(tmp_path), "--max-meshes", "1"]
    assert "--no-local-discovery" in args and ["--relay", "none"] == args[args.index("--relay"):args.index("--relay") + 2]
    assert ["--bind", "127.0.0.1:7000"] == args[args.index("--bind"):args.index("--bind") + 2]
    assert ["--max-age-days", "3"] == args[args.index("--max-age-days"):args.index("--max-age-days") + 2]
    assert "--max-mesh-mib" not in args
    capped = nm.MailboxSettings(max_mib=5).arguments(tmp_path)
    assert "--relay" not in capped and ["--max-mesh-mib", "5"] == capped[-2:]


def test_binary_resolution(tmp_path):
    assert nm.MailboxSettings(binary=script(tmp_path, "")).resolve_binary()
    assert nm.MailboxSettings(binary=str(tmp_path / "missing")).resolve_binary() is None


@pytest.mark.parametrize(
    "lines,expected",
    [
        (["mailbox id a", 'address {"x":1}'], {"id": "a", "address": '{"x":1}'}),
        (["mailbox id a"], None),
        (["address {}"], None),
        (["mailbox id a", "address not-json"], None),
        ([], None),
    ],
)
def test_parse_announcement(lines, expected):
    assert nm.parse_announcement(lines) == expected


async def test_missing_binary_is_an_error(tmp_path):
    process = nm.MailboxProcess(nm.MailboxSettings(binary=str(tmp_path / "none")), data_dir=tmp_path / "d")
    with pytest.raises(nm.MailboxError, match="mailbox_binary_missing"):
        await process.start()


async def test_start_reports_the_address_and_stop_ends_the_child(tmp_path):
    binary = script(tmp_path, ANNOUNCE + "exec sleep 60\n")
    process = nm.MailboxProcess(nm.MailboxSettings(binary=binary), data_dir=tmp_path / "d")
    await process.start()
    snapshot = process.snapshot()
    assert snapshot["running"] and snapshot["id"] == "box-1" and process.address == '{"id":"box-1"}'
    assert oct((tmp_path / "d").stat().st_mode & 0o777) == "0o700"
    pid = process._process.pid
    await process.stop()
    assert process.snapshot()["running"] is False and process.address is None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    await process.stop()


async def test_a_crashed_child_is_restarted(tmp_path, monkeypatch):
    monkeypatch.setattr(nm, "RESTART_FIRST_SECONDS", 0.05)
    marker = tmp_path / "ran"
    binary = script(tmp_path, f'{ANNOUNCE}if [ -e {marker} ]; then exec sleep 60; fi\ntouch {marker}\nexit 3\n')
    process = nm.MailboxProcess(nm.MailboxSettings(binary=binary), data_dir=tmp_path / "d")
    await process.start()
    for _ in range(100):
        if process.snapshot()["restarts"] >= 1 and process.snapshot()["running"]:
            break
        await asyncio.sleep(0.05)
    snapshot = process.snapshot()
    assert snapshot["restarts"] >= 1 and snapshot["running"]
    await process.stop()


async def test_a_child_that_never_announces_fails_start(tmp_path, monkeypatch):
    monkeypatch.setattr(nm, "STARTUP_SECONDS", 0.3)
    monkeypatch.setattr(nm, "RESTART_FIRST_SECONDS", 0.05)
    binary = script(tmp_path, "echo nothing useful\nexit 1\n")
    process = nm.MailboxProcess(nm.MailboxSettings(binary=binary), data_dir=tmp_path / "d")
    with pytest.raises(nm.MailboxError, match="mailbox_exited_before_ready"):
        await process.start()
    assert process.snapshot()["running"] is False


async def test_a_stubborn_child_is_killed(tmp_path, monkeypatch):
    monkeypatch.setattr(nm, "STOP_SECONDS", 0.2)
    binary = script(tmp_path, ANNOUNCE + "trap '' INT\nwhile true; do sleep 1; done\n")
    process = nm.MailboxProcess(nm.MailboxSettings(binary=binary), data_dir=tmp_path / "d")
    await process.start()
    pid = process._process.pid
    await process.stop()
    await asyncio.sleep(0.1)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
