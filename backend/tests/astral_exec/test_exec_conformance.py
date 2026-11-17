"""Tests the conformance suite's own machinery without a sandbox: result lines and exit codes, backend choice, refusal handling, the fixtures and the probe tool's mode table.
The cases that need a real backend run in the Seatbelt and Linux harness tests.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from astral_exec import conformance, run
from astral_exec.conformance import cases as cases_module
from astral_exec.conformance import context as context_module
from astral_exec.conformance import fixtures as fixtures_module
from astral_exec.conformance.fixtures import PUBLIC, Fixtures, Upstream
from astral_exec.errors import Refusal
from astral_exec.facts import Facts

PLATFORM = "darwin" if sys.platform == "darwin" else "linux"
needs_platform = pytest.mark.skipif(
    sys.platform not in ("darwin", "linux"), reason="the suite runs on macOS and Linux"
)


def healthy():
    if PLATFORM == "darwin":
        return Facts(
            platform="darwin",
            landlock_abi=0,
            bwrap=False,
            userns=False,
            euid=os.geteuid(),
            sandbox_exec=True,
            app_sandboxed=False,
        )
    facts = Facts(
        platform="linux",
        landlock_abi=6,
        bwrap=True,
        userns=True,
        euid=1000,
        sandbox_exec=False,
        app_sandboxed=False,
    )
    facts.bwrap_path = "/usr/bin/bwrap"
    facts.proc_children = True
    return facts


def broken():
    return Facts(
        platform=PLATFORM,
        landlock_abi=0,
        bwrap=False,
        userns=False,
        euid=1000,
        sandbox_exec=False,
        app_sandboxed=False,
    )


def test_a_result_line_is_text_or_json():
    result = conformance.Result("fs.x", conformance.FAIL, "why")
    assert result.line(False) == "FAIL fs.x: why"
    assert conformance.Result("fs.x", conformance.PASS).line(False) == "PASS fs.x"
    assert json.loads(result.line(True)) == {
        "case": "fs.x",
        "result": "FAIL",
        "detail": "why",
    }


@pytest.mark.parametrize(
    "results, wanted",
    [
        ([("a", "PASS")], 0),
        ([("a", "PASS"), ("b", "FAIL")], 1),
        ([("a", "REFUSED"), ("b", "FAIL")], 1),
        ([("a", "REFUSED"), ("b", "PASS")], 3),
        ([], 3),
    ],
)
def test_the_exit_status_fails_closed(results, wanted):
    assert conformance.status([conformance.Result(*row) for row in results]) == wanted


def test_choose_refuses_an_unknown_platform_and_a_foreign_backend(tmp_path):
    unknown = conformance.choose("auto", {"platform": "plan9"}, str(tmp_path))
    assert isinstance(unknown, Refusal) and "plan9" in unknown.message
    foreign = "linux" if PLATFORM == "darwin" else "seatbelt"
    refusal = conformance.choose(foreign, {"platform": PLATFORM}, str(tmp_path))
    assert isinstance(refusal, Refusal) and foreign in refusal.message


@needs_platform
def test_choose_names_the_backend_a_healthy_machine_offers(tmp_path):
    root = str(tmp_path.resolve())
    os.makedirs(f"{root}/home")
    name = "seatbelt" if PLATFORM == "darwin" else "linux"
    assert conformance.choose("auto", healthy(), root) == name
    assert conformance.choose(name, healthy(), root) == name


@needs_platform
def test_a_machine_with_no_backend_refuses_every_case_that_needs_one(tmp_path):
    results = conformance.run_suite(
        "auto",
        workdir=str(tmp_path.resolve()),
        only=["fs.read_outside", "refusal.no_backend"],
        facts=broken(),
    )
    by_case = {r.case: r for r in results}
    assert by_case["fs.read_outside_policy_denied"].result == conformance.REFUSED
    assert by_case["refusal.no_backend"].result == conformance.PASS
    assert conformance.status(results) == conformance.EXIT_REFUSED


@needs_platform
def test_the_refusal_cases_pass_without_starting_any_tool(tmp_path):
    results = conformance.run_suite(
        "auto", workdir=str(tmp_path.resolve()), only=["refusal."], facts=healthy()
    )
    report = [(r.case, r.result, r.detail) for r in results]
    assert results and {r.result for r in results} == {conformance.PASS}, report
    expected = {"refusal.no_backend", "refusal.shadow_mode"}
    expected |= (
        {"refusal.app_sandboxed"}
        if PLATFORM == "darwin"
        else {"refusal.root_node", "refusal.proc_children_missing"}
    )
    assert {r.case for r in results} == expected


def test_a_case_that_raises_anything_fails(monkeypatch):
    def boom(ctx):
        raise ZeroDivisionError("nope")

    def refuse(ctx):
        raise context_module.Failed("said so")

    broken_case = cases_module.Case("x.boom", boom, ("darwin", "linux"), False)
    failed_case = cases_module.Case("x.failed", refuse, ("darwin", "linux"), True)
    assert conformance.execute(broken_case, None, None) == conformance.Result(
        "x.boom", conformance.FAIL, "ZeroDivisionError: nope"
    )
    assert conformance.execute(failed_case, None, None).detail == "said so"
    refusal = Refusal("exec.backend_unavailable", "no backend here")
    assert conformance.execute(failed_case, None, refusal) == conformance.Result(
        "x.failed", conformance.REFUSED, "no backend here"
    )


def test_main_prints_one_line_per_case_and_returns_the_status(monkeypatch, capsys):
    rows = [conformance.Result("a", "PASS"), conformance.Result("b", "FAIL", "bad")]
    seen = {}

    def fake(backend, workdir=None, only=(), facts=None):
        seen.update(backend=backend, only=list(only))
        return rows

    monkeypatch.setattr(conformance, "run_suite", fake)
    assert conformance.main(["--backend", "linux", "--case", "fs.", "--json"]) == 1
    out = capsys.readouterr().out.splitlines()
    assert [json.loads(line)["result"] for line in out] == ["PASS", "FAIL"]
    assert seen == {"backend": "linux", "only": ["fs."]}
    monkeypatch.setattr(conformance, "run_suite", lambda *a, **k: [rows[0]])
    assert conformance.main([]) == 0
    assert capsys.readouterr().out == "PASS a\n"


def test_main_refuses_unknown_options():
    assert conformance.main(["--backend", "container"]) == conformance.EXIT_USAGE
    assert conformance.main(["--help"]) == 0


def test_run_py_dispatches_the_conformance_command():
    assert run.main(["conformance", "--backend", "container"]) == conformance.EXIT_USAGE


def test_the_module_entry_runs_the_same_command():
    done = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "astral_exec.conformance",
            "--backend",
            "container",
        ],
        env={"PYTHONPATH": str(Path(conformance.__file__).resolve().parents[2])},
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == conformance.EXIT_USAGE


def test_every_case_is_named_once_for_a_known_platform():
    names = [case.name for case in cases_module.CASES]
    assert len(names) == len(set(names))
    assert all(
        set(case.platforms) <= {"darwin", "linux"} for case in cases_module.CASES
    )


def test_the_probe_tool_serves_every_mode_the_cases_ask_for():
    source = (
        Path(context_module.__file__)
        .with_name("probe_tool.py")
        .read_text(encoding="utf-8")
    )
    tree = ast.parse(source)
    modes = next(
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign) and node.targets[0].id == "MODES"
    )
    served = {key.value for key in modes.keys}
    asked = set(
        re.findall(
            r'ctx\.(?:run|control)\(\s*"(\w+)"',
            Path(cases_module.__file__).read_text(encoding="utf-8"),
        )
    )
    assert asked and asked <= served


def test_parse_keeps_checks_and_sets_aside_other_lines():
    checks, extra = context_module.parse(
        b'{"check": "a", "outcome": 1}\nEXEC-RAN\n{"x": 1}\n'
    )
    assert checks == {"a": 1} and extra == ["EXEC-RAN", '{"x": 1}']


def test_a_run_that_did_not_complete_cannot_be_read():
    unfinished = context_module.Run(
        context_module.Outcome("exec.time_limit", None, b"", b"late")
    )
    with pytest.raises(context_module.Failed, match="did not complete"):
        unfinished.get("anything")
    finished = context_module.Run(context_module.Outcome(None, 0), {"a": 1})
    assert finished.get("a") == 1
    with pytest.raises(context_module.Failed, match="no b"):
        finished.get("b")


@needs_platform
def test_the_process_table_finds_a_process_by_a_token_in_its_command():
    token = "7391" + str(os.getpid())
    child = subprocess.Popen(["/bin/sleep", token])
    try:
        for _ in range(500):
            if child.pid in context_module.processes_matching(token):
                break
            time.sleep(0.01)
        assert child.pid in context_module.processes_matching(token)
    finally:
        child.kill()
        child.wait()
    assert child.pid not in context_module.processes_matching(token)


def connect(family, address):
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.connect(address)
        return sock.recv(64)


def test_every_fixture_counts_what_reaches_it_and_the_upstream_maps_ports(tmp_path):
    fixtures = Fixtures(tmp_path, tmp_path)
    try:
        before = fixtures.snapshot()
        assert before == {"tcp": 0, "unix": 0, "udp": 0}
        connect(socket.AF_INET, ("127.0.0.1", fixtures.tcp_port))
        connect(socket.AF_UNIX, fixtures.unix_path)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.settimeout(5)
            query = b"\x12\x34\x01\x00\x00\x01"
            udp.sendto(query, ("127.0.0.1", fixtures.udp_port))
            assert udp.recvfrom(512)[0][:2] == query[:2]
        for _ in range(500):
            if fixtures.snapshot() == {"tcp": 1, "unix": 1, "udp": 1}:
                break
            time.sleep(0.01)
        assert fixtures.snapshot() == {"tcp": 1, "unix": 1, "udp": 1}

        upstream = Upstream(fixtures, {"fixture.example.com": [PUBLIC]})
        assert upstream.resolve("fixture.example.com", 80) == [PUBLIC]
        with pytest.raises(OSError):
            upstream.resolve("other.example.com", 80)
        echo = upstream.connect(PUBLIC, 80, 5)
        echo.sendall(b"ping")
        assert echo.recv(64) == b"PONG:ping"
        echo.close()
        tls = upstream.connect(PUBLIC, 443, 5)
        tls.close()
        assert upstream.dials == [(PUBLIC, 80), (PUBLIC, 443)]
        assert upstream.lookups == [
            ("fixture.example.com", 80),
            ("other.example.com", 80),
        ]
        assert set(upstream.options) == {"resolver", "connector"}
    finally:
        fixtures.stop()


def test_a_missing_openssl_refuses_the_tls_case_with_its_reason(monkeypatch, tmp_path):
    monkeypatch.setattr(fixtures_module.shutil, "which", lambda name: None)
    fixtures = Fixtures(tmp_path, tmp_path)
    try:
        assert fixtures.cert is None
        tls_case = next(
            c for c in cases_module.CASES if c.name == "proxy.https_through_proxy"
        )
        ctx = context_module.Context.__new__(context_module.Context)
        ctx.fixtures = fixtures
        result = conformance.execute(tls_case, ctx, None)
        assert result.result == conformance.REFUSED
        assert result.detail.startswith("tls_fixture_unavailable")
    finally:
        fixtures.stop()


@pytest.mark.skipif(
    shutil.which("openssl") is None,
    reason="this test needs the openssl CLI to make the certificate",
)
def test_with_openssl_the_tls_fixture_has_a_certificate(tmp_path):
    fixtures = Fixtures(tmp_path, tmp_path)
    try:
        assert (
            fixtures.cert is not None
            and fixtures.cert.exists()
            and fixtures.cert_error is None
        )
    finally:
        fixtures.stop()
