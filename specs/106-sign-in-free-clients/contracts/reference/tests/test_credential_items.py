"""Runs every credential item vector through the reference and pins the contracted outcome of the groups that credential-items.md names.
A failure means credential-items.md, the committed vectors and credential_items.py disagree.
"""

import json
import re
from pathlib import Path

import credential_items
import generate_credential_vectors
import pytest

CONTRACTS = Path(__file__).resolve().parents[2]
VECTORS = CONTRACTS / "vectors"
DOC = json.loads((VECTORS / "credential-items-v1.json").read_text(encoding="utf-8"))
CASES = {case["name"]: case for case in DOC["cases"]}
TOKEN_FIELDS = ("access_token", "refresh_token", "id_token")


def result(name: str) -> dict:
    return CASES[name]["expect"]


def code(name: str) -> str | None:
    return result(name).get("error")


@pytest.mark.parametrize("name", sorted(CASES))
def test_case_reproduces_its_committed_expectation(name: str) -> None:
    case = CASES[name]
    assert credential_items.run(case["kind"], case["input"]) == case["expect"]


def test_the_document_names_the_contract() -> None:
    assert DOC["format"] == "astral.vectors/v1"
    assert DOC["name"] == "credential-items-v1"
    assert DOC["spec"] == "106/contracts/credential-items.md"
    assert len(CASES) == len(DOC["cases"])


def test_sign_in_token_names_are_the_three_fields() -> None:
    for name in TOKEN_FIELDS:
        assert credential_items.has_token_field({name: "x"})
        assert credential_items.has_token_field({"a": [{"b": {name.upper(): "x"}}]})
    for field in ("token", "refresh", "id", "access", "access_tokens"):
        assert not credential_items.has_token_field({field: "x"})


@pytest.mark.parametrize(
    "name",
    [n for n in CASES if n.startswith("write_token_") and not n.endswith("allowed")],
)
def test_every_token_shaped_value_is_refused_by_every_family(name: str) -> None:
    assert code(name) == "credential.sign_in_token"


@pytest.mark.parametrize("name", [n for n in CASES if n.endswith("_allowed")])
def test_near_misses_are_allowed(name: str) -> None:
    assert "ok" in result(name)


def test_a_provider_item_keeps_the_shape_it_has_today() -> None:
    assert result("write_provider_valid") == {"ok": {"family": "provider"}}
    assert result("write_legacy_server_valid") == {"ok": {"family": "legacy_server"}}


def test_the_size_limit_is_65536_bytes_of_lets_cj() -> None:
    assert "ok" in result("write_size_65536_valid")
    assert code("write_size_65537") == "credential.too_large"
    assert code("write_size_before_token") == "credential.too_large"


def test_a_key_item_has_at_most_32_secrets_of_at_most_4096_code_points() -> None:
    assert "ok" in result("write_key_32_secrets_valid")
    assert code("write_key_33_secrets") == "credential.malformed"
    assert "ok" in result("write_key_secret_4096_valid")
    assert code("write_key_secret_4097") == "credential.malformed"
    assert code("write_key_secret_zero_width_joiner") == "credential.malformed"


def test_only_the_secure_field_may_write() -> None:
    sources = [n for n in CASES if n.startswith("write_source_")]
    assert len(sources) >= 6
    assert all(code(n) == "credential.source_refused" for n in sources)
    assert code("write_source_checked_before_name") == "credential.source_refused"


def test_a_record_needs_exactly_its_secret_names() -> None:
    assert result("binding_stdio_env_names")["ok"]["secrets"] == ["A_TOKEN", "B_TOKEN"]
    assert code("binding_stdio_missing_env_name") == "credential.key_mismatch"
    assert code("binding_http_api_two_secrets") == "credential.key_mismatch"
    for name in (
        "binding_oauth_record_takes_no_key",
        "binding_model_uses_a_provider_item",
    ):
        assert code(name) == "credential.malformed"
    assert code("binding_token_field_refused_first") == "credential.sign_in_token"


def test_every_code_is_exercised() -> None:
    seen = {c["expect"].get("error") for c in DOC["cases"]}
    assert set(credential_items.CODES) <= seen


def test_the_document_code_table_matches_the_module() -> None:
    text = (CONTRACTS / "credential-items.md").read_text(encoding="utf-8")
    section = text.split("## 6. Codes", 1)[1].split("\n## ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("| `")]
    listed = {re.search(r"`([a-z_.]+)`", row).group(1) for row in rows}
    assert listed == set(credential_items.CODES)


def test_the_connection_document_points_at_this_contract() -> None:
    text = (CONTRACTS / "connection.md").read_text(encoding="utf-8")
    assert "credential-items.md" in text
    keys = CONTRACTS.parent.parent / "105-decentralized-personal-mesh/contracts/keys.md"
    assert "credential-items.md" in keys.read_text(encoding="utf-8")


def test_generator_reproduces_the_committed_file() -> None:
    assert generate_credential_vectors.render()["credential-items-v1"] == (
        VECTORS / "credential-items-v1.json"
    ).read_text(encoding="utf-8")


def test_check_mode_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("check mode wrote a file")

    monkeypatch.setattr(Path, "write_text", refuse)
    generate_credential_vectors.main(["--check"])
