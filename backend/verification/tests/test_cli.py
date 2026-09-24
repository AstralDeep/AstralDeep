"""Tests for CLI arg parsing and exit-code mapping (backend/verification/__main__.py):
mode normalization, run-id derivation, and exit-code precedence for near-exposure and
strict-uncertain.
"""

from __future__ import annotations

from verification.__main__ import config_from_args, exit_code_for, parse_args


def test_parse_and_normalize_mode():
    args = parse_args(["--mode", "in-process", "--persona", "everyday",
                       "--persona", "government", "--strict", "--run-id", "__verif__cli"])
    cfg = config_from_args(args)
    assert cfg.mode == "in_process"
    assert cfg.personas == ["everyday", "government"]
    assert cfg.strict is True
    assert cfg.run_id == "__verif__cli"


def test_run_id_from_stamp():
    cfg = config_from_args(parse_args(["--stamp", "20260616T0102Z"]))
    assert cfg.run_id == "__verif__20260616T0102Z"


def test_exit_code_clean():
    record = {"verdicts": [{"outcome": "pass"}, {"outcome": "uncertain"}], "flags": []}
    assert exit_code_for(record, strict=False) == 0


def test_exit_code_fail():
    record = {"verdicts": [{"outcome": "pass"}, {"outcome": "fail"}], "flags": []}
    assert exit_code_for(record, strict=False) == 1


def test_exit_code_near_exposure_takes_precedence():
    record = {"verdicts": [{"outcome": "fail"}], "flags": ["credential_near_exposure"]}
    assert exit_code_for(record, strict=False) == 2


def test_exit_code_strict_uncertain():
    record = {"verdicts": [{"outcome": "pass"}, {"outcome": "uncertain"}], "flags": []}
    assert exit_code_for(record, strict=True) == 2
