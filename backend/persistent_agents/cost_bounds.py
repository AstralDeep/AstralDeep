"""Convert trusted, expiring quote coverage into a conservative reservation.

Coverage comes from the host's quote provider during reviewed activation. No
source, model, UI or external tool response supplies these prices. Quotes cover
all configured model routes and complete downstream tool execution; absence is
an explicit refusal when the owner selects a currency ceiling.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime

from persistent_agents.dispatch_context import DispatchDenied
from persistent_agents.runtime_values import digest, thaw


def quoted_amount(amount, currency, coverage, tool_identity=None):
    if currency is None:
        return amount, None, None
    try:
        if not isinstance(coverage, Mapping) or coverage["currency"] != currency:
            raise ValueError
        quote = thaw(coverage)
        identity = quote.pop("quote_digest")
        if identity != digest(quote):
            raise ValueError
        expiry = datetime.fromisoformat(quote["expires_at"])
        if expiry.tzinfo is None or expiry <= datetime.now(UTC):
            raise ValueError
        call_rate = quote["model_call_micro_units"]
        token_rate = quote["model_token_micro_units"]
        tool_rate = quote["tool_call_micro_units"][tool_identity] if amount.tool_calls else 0
        if any(type(rate) is not int or not 0 <= rate <= 1_000_000_000
               for rate in (call_rate, token_rate, tool_rate)):
            raise ValueError
        spend = amount.model_calls * call_rate + amount.tokens * token_rate + amount.tool_calls * tool_rate
        if spend > 9_000_000_000_000:
            raise ValueError
        return replace(amount, spend_micro_units=spend, currency=currency), identity, expiry
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise DispatchDenied("assignment_cost_bound_unavailable") from exc
