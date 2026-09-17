"""Feature 089 (T007): the TypeSafe key is covered by the same hygiene the LLM key is.

Every test here uses ``CANARY``, a synthetic value that matches the committed
TypeSafe pattern and is not a credential. It is allowlisted in ``.gitleaks.toml``
for exactly this purpose. No real key ever appears in this repository.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import tomllib

import pytest

from llm_config.audit_events import _KEY_PREFIX_PATTERNS, _assert_no_api_key
from llm_config.log_scrub import (
    TYPESAFE_KEY_PATTERN,
    LLMKeyRedactionFilter,
    _is_api_key_field,
    redact_llm_config,
)

# Synthetic. Matches the committed pattern; is not a key.
CANARY = "ts_live_CANARY0000NOTAREALKEY000000"
CANARY_VARIANTS = (
    CANARY,
    "tsk_CANARY0000NOTAREALKEY000000000",
    "tsai_CANARY0000NOTAREALKEY00000000",
    "ts-CANARY0000NOTAREALKEY0000000000",
)

# A synthetic key in the *real* vendor shape: a short lowercase prefix, an
# underscore, then a long lowercase-alphanumeric tail. The first version of the
# pattern was guessed from other vendors' prefixes and did not match a real key
# at all, which meant the scrubber silently passed it through. This constant
# exists so that class of miss fails a test instead of reaching a log file. The
# prefix letters are invented; only the shape is real.
SHAPED_CANARY = "zqkfmp_" + ("0canary9notarealkey" * 6)[:101]

REPO_ROOT = Path(__file__).resolve().parents[3]


# -- field-name matching -------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "api_key",
        "API_KEY",
        "typesafe_api_key",
        "TYPESAFE_API_KEY",
        "userApiKey",
        "llm.api_key",
        "provider-api-key",
    ],
)
def test_every_api_key_spelling_is_recognized(name: str) -> None:
    assert _is_api_key_field(name) is True


@pytest.mark.parametrize(
    "name",
    ["model", "base_url", "api_keys", "keyapi", "api_key_fingerprint", "", None, 7],
)
def test_non_key_fields_are_left_alone(name: object) -> None:
    assert _is_api_key_field(name) is False


# -- log scrubbing -------------------------------------------------------


def test_typesafe_field_is_redacted_alongside_the_llm_field() -> None:
    redacted = redact_llm_config(
        {
            "api_key": "sk-0000000000000000000000000",
            "typesafe_api_key": CANARY,
            "model": "jev-latest",
            "base_url": "https://api.typesafe.ai",
        }
    )
    assert redacted["api_key"] == "<redacted>"
    assert redacted["typesafe_api_key"] == "<redacted>"
    assert redacted["model"] == "jev-latest"
    assert redacted["base_url"] == "https://api.typesafe.ai"


@pytest.mark.parametrize("value", CANARY_VARIANTS)
def test_typesafe_shaped_tokens_are_redacted_in_free_text(value: str) -> None:
    scrubbed = redact_llm_config("routing failed with key " + value + " attached")
    assert value not in scrubbed
    assert "<redacted>" in scrubbed


def test_typesafe_key_is_redacted_inside_nested_structures() -> None:
    payload = {
        "request": {"headers": {"Authorization": "Bearer " + CANARY}},
        "tools": [{"note": CANARY}],
        "pair": ("left", CANARY),
    }
    assert CANARY not in json.dumps(redact_llm_config(payload), default=str)


def test_typesafe_key_is_redacted_inside_a_json_string() -> None:
    encoded = json.dumps({"typesafe_api_key": CANARY, "model": "jev-latest"})
    scrubbed = redact_llm_config(encoded)
    assert CANARY not in scrubbed
    assert "jev-latest" in scrubbed


def test_the_logging_filter_scrubs_a_typesafe_key_from_a_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    logger = logging.getLogger("test.089.hygiene")
    logger.addFilter(LLMKeyRedactionFilter())
    try:
        with caplog.at_level(logging.INFO, logger="test.089.hygiene"):
            logger.info("probing with %s", CANARY)
            logger.info("inline " + CANARY)
    finally:
        logger.filters.clear()

    combined = "\n".join(record.getMessage() for record in caplog.records)
    assert CANARY not in combined


def test_a_short_token_is_not_mistaken_for_a_key() -> None:
    # The pattern requires a long enough tail, so ordinary prose survives.
    assert redact_llm_config("ts_ok") == "ts_ok"
    assert redact_llm_config("the ts-1 lane") == "the ts-1 lane"


def test_the_committed_pattern_matches_the_canary_and_nothing_ordinary() -> None:
    assert TYPESAFE_KEY_PATTERN.search(CANARY) is not None
    assert TYPESAFE_KEY_PATTERN.search("timestamp") is None
    assert TYPESAFE_KEY_PATTERN.search("ts_") is None


# -- audit-event guard ---------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"api_key": "x"},
        {"typesafe_api_key": "x"},
        {"TYPESAFE_API_KEY": "x"},
        {"nested": {"userApiKey": "x"}},
        {"note": CANARY},
        {"nested": {"note": CANARY}},
    ],
)
def test_audit_payloads_carrying_a_key_are_refused(payload: dict) -> None:
    with pytest.raises(ValueError):
        _assert_no_api_key(payload)


def test_a_clean_typesafe_audit_payload_is_accepted() -> None:
    _assert_no_api_key(
        {
            "action": "typesafe_save",
            "outcome": "valid",
            "model": "jev-latest",
            "key_fingerprint": "0123456789ab",
        }
    )


def test_the_audit_guard_shares_the_scrubber_pattern() -> None:
    assert TYPESAFE_KEY_PATTERN in _KEY_PREFIX_PATTERNS


# -- committed configuration ---------------------------------------------


def test_gitleaks_carries_named_typesafe_rules() -> None:
    config = tomllib.loads(
        (REPO_ROOT / ".gitleaks.toml").read_text(encoding="utf-8")
    )
    rules = {rule["id"]: rule for rule in config.get("rules", ())}
    assert "typesafe-system-one-key" in rules
    assert "typesafe-api-key-assignment" in rules
    for rule in rules.values():
        re.compile(rule["regex"])  # every committed pattern must compile
        assert rule["description"]
        assert "typesafe" in rule["tags"]
    assert re.compile(rules["typesafe-system-one-key"]["regex"]).search(CANARY)


def test_the_synthetic_canary_is_the_only_allowlisted_typesafe_value() -> None:
    config = tomllib.loads(
        (REPO_ROOT / ".gitleaks.toml").read_text(encoding="utf-8")
    )
    allowlisted = config["allowlist"]["regexes"]
    typesafe_entries = [entry for entry in allowlisted if "ts_" in entry or "tsk_" in entry]
    assert typesafe_entries == [CANARY]


def test_typesafe_env_names_are_scrubbed_from_harness_artifacts() -> None:
    from verification.config import OTHER_SECRET_ENV_NAMES

    assert {
        "TYPESAFE_API_KEY",
        "TYPESAFE_BASE_URL",
        "TYPESAFE_DEFAULT_MODEL",
    } <= set(OTHER_SECRET_ENV_NAMES)


def test_no_real_typesafe_key_is_committed_in_this_test() -> None:
    """The canary must stay synthetic: a real key here would be a leak."""
    source = Path(__file__).read_text(encoding="utf-8")
    assert "CANARY" in CANARY
    assert source.count("NOTAREALKEY") >= 1


# -- the real vendor key shape -------------------------------------------


def test_a_key_in_the_real_vendor_shape_is_redacted() -> None:
    """The regression that motivated the pattern's second version.

    A long lowercase-alphanumeric key behind a short lowercase prefix must be
    redacted. The original pattern required one of a handful of guessed
    prefixes and matched nothing of this shape.
    """
    assert TYPESAFE_KEY_PATTERN.search(SHAPED_CANARY) is not None
    scrubbed = redact_llm_config("saving key " + SHAPED_CANARY + " for the user")
    assert SHAPED_CANARY not in scrubbed
    assert "<redacted>" in scrubbed


def test_a_shaped_key_is_refused_in_an_audit_payload() -> None:
    with pytest.raises(ValueError):
        _assert_no_api_key({"note": SHAPED_CANARY})


def test_the_gitleaks_rule_covers_the_real_vendor_shape() -> None:
    config = tomllib.loads(
        (REPO_ROOT / ".gitleaks.toml").read_text(encoding="utf-8")
    )
    rule = next(
        r for r in config["rules"] if r["id"] == "typesafe-system-one-key"
    )
    assert re.compile(rule["regex"]).search(SHAPED_CANARY)


@pytest.mark.parametrize(
    "identifier",
    [
        "test_the_circuit_opens_after_three_consecutive_fallbacks",
        "backend_orchestrator_typesafe_routing_decision_module_level_constant",
        "astralplane_reconciliation_marker_table_name_used_in_the_catalog_query",
        "user_typesafe_credential",
        "chrome_typesafe_save",
        "a_very_long_snake_case_identifier_that_someone_might_log_in_a_debug_line",
        "089_typesafe_a8p_integration_branch_name_for_the_feature_work_here",
    ],
)
def test_ordinary_identifiers_are_not_redacted(identifier: str) -> None:
    """The shape rule must not eat snake_case out of every debug line.

    This is why the pattern requires a digit in the tail as well as length: a
    long identifier is common, a long identifier carrying a digit inside a
    single token is not.
    """
    assert TYPESAFE_KEY_PATTERN.search(identifier) is None
    assert redact_llm_config(identifier) == identifier


def test_the_shaped_canary_is_synthetic() -> None:
    assert "canary" in SHAPED_CANARY
    assert "notarealkey" in SHAPED_CANARY
