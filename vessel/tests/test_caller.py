# The caller client against the contract's vectors (fixtures/caller-v1-cases.json, copied from AstralDeep
# specs/106-sign-in-free-clients/contracts/vectors/caller-v1.json at commit 567144de2228e122eb18a6a4a0905d6c9e5ebbca),
# plus the 0600 holder key file, the ticket file and the refusal of every removed way to pass a credential.

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from astralvessel import caller as callers
from astralvessel import cli

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "caller-v1-cases.json").read_text())
SEED = bytes.fromhex(VECTORS["test_keys"]["holder_seed_hex"])
KEY = callers.KEY_TEXT + callers.b64(SEED)
CASES = VECTORS["cases"]
TICKET = callers.TICKET_TEXT + callers.b64(json.dumps({"id": "ticket-1"}).encode())


def verify_cases():
    return [c for c in CASES if c["kind"] == "caller_verify" and "ok" in c["expect"]]


def test_the_proof_bytes_for_the_contracts_accepted_cases_equal_the_contracts():
    matched = 0
    for case in verify_cases():
        given = case["input"]
        proof = json.loads(callers.unb64(given["proof"]))
        request = given["request"]
        body = request["body"].encode()
        ticket = callers.TICKET_TEXT + given["authorization"].removeprefix(callers.SCHEME)
        caller = callers.Caller(ticket, KEY)
        assert caller.ticket_id == proof["ticket"], case["name"]
        assert proof["body"] == "sha256:" + hashlib.sha256(body).hexdigest(), case["name"]
        made = caller.headers(request["method"], request["target"], body, ms=proof["ms"],
                              nonce=callers.unb64(proof["nonce"]))
        assert made[callers.PROOF_HEADER] == given["proof"], case["name"]
        assert made["Authorization"] == given["authorization"], case["name"]
        matched += 1
    assert matched >= 10


def test_the_key_and_ticket_text_forms_match_the_contract():
    derived = next(c for c in CASES if c["name"] == "key_form_derives_holder")
    key = ed25519.Ed25519PrivateKey.from_private_bytes(callers.unb64(derived["input"]["text"][len(callers.KEY_TEXT):]))
    assert callers.holder_text(key) == derived["expect"]["ok"]["holder_text"]
    seen = 0
    for case in CASES:
        if case["kind"] == "caller_ticket_text" and "ok" in case["expect"]:
            caller = callers.Caller(case["input"]["text"], KEY)
            assert caller.ticket_id == case["expect"]["ok"]["id"]
            assert caller.headers("POST", "/a2a")["Authorization"] == case["expect"]["ok"]["authorization"]
            seen += 1
    assert seen >= 1


@pytest.mark.parametrize("ticket,key", [("nope", KEY), (TICKET, "nope"), (callers.TICKET_TEXT + "!!", KEY),
                                        (callers.TICKET_TEXT + callers.b64(b"[]"), KEY),
                                        (TICKET, callers.KEY_TEXT + callers.b64(b"short"))])
def test_a_malformed_ticket_or_key_is_refused(ticket, key):
    with pytest.raises(callers.CallerError, match="caller ticket must be"):
        callers.Caller(ticket, key)


def test_a_proof_is_signed_by_the_holder_and_fresh_each_time():
    caller = callers.Caller(TICKET, KEY)
    first = caller.headers("POST", "/a2a", b"{}")[callers.PROOF_HEADER]
    second = caller.headers("POST", "/a2a", b"{}")[callers.PROOF_HEADER]
    proof = json.loads(callers.unb64(first))
    unsigned = {k: v for k, v in proof.items() if k != "sig"}
    ed25519.Ed25519PrivateKey.from_private_bytes(SEED).public_key().verify(
        callers.unb64(proof["sig"]), callers.PROOF_CONTEXT + callers.canonical(unsigned))
    assert first != second


def test_the_key_command_makes_a_private_file_and_prints_only_the_public_holder_form(tmp_path, capsys):
    holder = callers.create_key(tmp_path)
    path = tmp_path / callers.KEY_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text().strip()
    assert text.startswith(callers.KEY_TEXT) and text not in holder and holder.startswith(callers.HOLDER_TEXT)
    key = ed25519.Ed25519PrivateKey.from_private_bytes(callers.unb64(text[len(callers.KEY_TEXT):]))
    assert callers.holder_text(key) == holder
    with pytest.raises(callers.CallerError, match="already exists"):
        callers.create_key(tmp_path)
    assert path.read_text().strip() == text
    other = tmp_path / "other"
    other.mkdir()
    cli_args = type("A", (), {"deployment": str(other / "deployment.toml")})()
    from astralvessel import operate
    operate.caller_key(cli_args)
    out = capsys.readouterr().out
    assert "holder  astral-caller-holder:v1:" in out and callers.KEY_TEXT not in out


def test_a_key_file_open_to_group_or_others_is_refused(tmp_path):
    callers.create_key(tmp_path)
    path = tmp_path / callers.KEY_FILE
    for mode in (0o640, 0o604, 0o644, 0o660):
        path.chmod(mode)
        with pytest.raises(callers.CallerError, match="readable by group or others.*chmod 600"):
            callers.read_key(tmp_path)
    path.chmod(0o400)
    assert callers.read_key(tmp_path).startswith(callers.KEY_TEXT)


def test_a_missing_or_linked_key_and_a_missing_ticket_give_pointed_errors(tmp_path, monkeypatch):
    with pytest.raises(callers.CallerError, match="run astralvessel caller key"):
        callers.read_key(tmp_path)
    real = tmp_path / "real"
    real.write_text(KEY)
    real.chmod(0o600)
    (tmp_path / callers.KEY_FILE).symlink_to(real)
    with pytest.raises(callers.CallerError, match="regular file"):
        callers.read_key(tmp_path)
    monkeypatch.delenv(callers.TICKET_FILE_VARIABLE, raising=False)
    with pytest.raises(callers.CallerError, match="--ticket-file or set ASTRAL_CALLER_TICKET_FILE"):
        callers.read_ticket(None)
    with pytest.raises(callers.CallerError, match="does not exist"):
        callers.read_ticket(str(tmp_path / "missing"))
    with pytest.raises(callers.CallerError, match="cannot read the ticket file"):
        callers.read_ticket(str(tmp_path))


def test_the_ticket_comes_from_the_option_or_the_file_the_variable_names(tmp_path, monkeypatch):
    (tmp_path / callers.KEY_FILE).write_text(KEY)
    (tmp_path / callers.KEY_FILE).chmod(0o600)
    ticket = tmp_path / "ticket"
    ticket.write_text(TICKET + "\n")
    monkeypatch.setenv(callers.TICKET_FILE_VARIABLE, str(ticket))
    assert callers.load_caller(tmp_path, None).ticket_id == "ticket-1"
    monkeypatch.setenv(callers.TICKET_FILE_VARIABLE, str(tmp_path / "gone"))
    assert callers.load_caller(tmp_path, str(ticket)).ticket_id == "ticket-1"


@pytest.mark.parametrize("name", ["ASTRAL_OWNER_TOKEN", "ASTRAL_CALLER_KEY", "ASTRAL_CALLER_TICKET"])
def test_a_removed_environment_variable_is_refused_with_a_reason(tmp_path, monkeypatch, name):
    (tmp_path / callers.KEY_FILE).write_text(KEY)
    (tmp_path / callers.KEY_FILE).chmod(0o600)
    (tmp_path / "ticket").write_text(TICKET)
    monkeypatch.setenv(name, "anything")
    with pytest.raises(callers.CallerError, match=f"{name} is no longer used"):
        callers.load_caller(tmp_path, str(tmp_path / "ticket"))


@pytest.mark.parametrize("argv,words", [
    (("ask", "q", "--token-file", "x"), "owner tokens are gone"), (("chat", "--token-file"), "owner tokens are gone"),
    (("pair", "--token-file", "x"), "owner tokens are gone"), (("conformance", "--token-file", "x"), "ask the owner"),
    (("ask", "q", "--key-file", "x"), "caller-key file"), (("conformance", "--key-file", "x"), "caller key"),
])
def test_the_removed_options_say_why(monkeypatch, capsys, argv, words):
    monkeypatch.setattr("sys.argv", ["astralvessel", *argv])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 2 and words in capsys.readouterr().err


def test_the_proof_bytes_match_the_kernels_canonical_json():
    kernel = pytest.importorskip("astral_kernel")
    fields = {"v": 1, "ticket": "t", "method": "POST", "target": "/a2a?x=\u00e9", "body": "sha256:" + "a" * 64,
              "ms": 1_700_000_000_000, "nonce": callers.b64(os.urandom(16))}
    assert callers.canonical(fields) == bytes(kernel.canonical_json(json.dumps(fields).encode()))
