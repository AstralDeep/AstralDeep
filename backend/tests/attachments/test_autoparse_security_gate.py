"""Tests that orchestrator/code_security.py's gate blocks auto-created parser code from
installing dependencies (subprocess/os.system) while passing stdlib-only parsers, and
that the codegen prompt enforces stdlib-only.
"""

from __future__ import annotations

from orchestrator.code_security import CodeSecurityAnalyzer, Severity


def _analyze(code: str):
    return CodeSecurityAnalyzer().analyze(code, filename="parser/mcp_tools.py")


def test_subprocess_install_is_blocked():
    code = (
        "import subprocess\n"
        "def parse_x(attachment_id, **kwargs):\n"
        "    subprocess.run(['pip', 'install', 'pyarrow'])\n"
        "    return {}\n"
    )
    report = _analyze(code)
    assert not report.passed
    assert report.max_severity == Severity.CRITICAL


def test_os_system_is_blocked():
    code = (
        "import os\n"
        "def parse_x(attachment_id, **kwargs):\n"
        "    os.system('pip install pyarrow')\n"
        "    return {}\n"
    )
    report = _analyze(code)
    assert not report.passed


def test_stdlib_only_parser_passes_gate():
    code = (
        "import zipfile, io\n"
        "def parse_zip(attachment_id, **kwargs):\n"
        "    return {'note': 'best-effort zip listing', 'entries': []}\n"
    )
    report = _analyze(code)
    assert report.passed


def test_codegen_prompt_carries_stdlib_only_constraint():
    import inspect

    from orchestrator import agent_generator
    src = " ".join(inspect.getsource(agent_generator).split())
    assert "packages already installed in this image" in src
    assert "best-effort structural extraction" in src
    assert "Do NOT assume any" in src
