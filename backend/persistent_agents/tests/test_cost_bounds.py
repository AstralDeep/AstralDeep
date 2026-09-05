"""Currency limits require a current trusted finite bound for every request."""

from datetime import UTC, datetime, timedelta

import pytest
from astralplane.repositories.assignments import AssignmentResourceAmount
from persistent_agents.cost_bounds import quoted_amount
from persistent_agents.runtime_values import digest


def quote(**updates):
    result = {"currency": "USD", "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
              "model_call_micro_units": 100, "model_token_micro_units": 2,
              "tool_call_micro_units": {"reader:read": 20}}
    result.update(updates)
    return {**result, "quote_digest": digest(result)}


def test_unpriced_amount_remains_unknown():
    amount = AssignmentResourceAmount(tool_calls=1, elapsed_ms=1000)
    assert quoted_amount(amount, None, None, "reader:read") == (amount, None, None)


def test_all_reserved_usage_is_priced():
    amount = AssignmentResourceAmount(model_calls=1, tool_calls=2, tokens=10, elapsed_ms=1000)
    bounded, identity, expiry = quoted_amount(amount, "USD", quote(), "reader:read")
    assert bounded.spend_micro_units == 160
    assert bounded.currency == "USD"
    assert len(identity) == 64
    assert expiry > datetime.now(UTC)


@pytest.mark.parametrize("value", [None, {}, quote(currency="EUR"),
    quote(expires_at="2000-01-01T00:00:00+00:00"), quote(model_token_micro_units=-1),
    quote(model_call_micro_units=True), quote(tool_call_micro_units={}),
    {**quote(), "quote_digest": "a" * 64}])
def test_unknown_expired_or_changed_price_refused(value):
    with pytest.raises(PermissionError):
        quoted_amount(AssignmentResourceAmount(model_calls=1, tool_calls=1, tokens=2),
                      "USD", value, "reader:read")
