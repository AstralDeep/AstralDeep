"""Checks that the turn timeline driver carries no default sign-in token and still validates its arguments.
It loads scripts/verification/typesafe_turn_timeline.py by path and runs its argument parser."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "verification" / "typesafe_turn_timeline.py"


def load():
    spec = importlib.util.spec_from_file_location("typesafe_turn_timeline_under_test", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_perf_only_without_since_is_a_usage_error_and_no_token_is_defaulted(monkeypatch, capsys):
    monkeypatch.delenv("TEST_UI_TOKEN", raising=False)
    with pytest.raises(SystemExit) as stopped:
        load().main(["--perf-only"])
    assert stopped.value.code == 2
    assert "--perf-only needs --since" in capsys.readouterr().err


def test_the_help_text_names_no_default_token(monkeypatch, capsys):
    monkeypatch.delenv("TEST_UI_TOKEN", raising=False)
    with pytest.raises(SystemExit) as stopped:
        load().main(["--help"])
    assert stopped.value.code == 0
    assert "dev" + "-token" not in capsys.readouterr().out
