"""Builds literal evidence viewers and content-free durable reference cards with approved primitives.
Conversation commits retain metadata while authenticated Projection surfaces expose temporary text.
"""

from copy import deepcopy
import json
import re

from astralprims import Alert, Badge, Button, KeyValue
from shared.protocol import MCPResponse

_LABELS = {
    "source": "Captured source",
    "preview": "Partial preview",
    "summary": "Generated summary",
    "missing": "Source unavailable",
    "blocked": "Recall blocked",
    "limit": "Context limit",
}
_REFERENCE = re.compile(r"obs_[A-Za-z0-9_-]{43}")
_VIEW = re.compile(r"view_[A-Za-z0-9_-]{43}")
_DIGEST = re.compile(r"[a-f0-9]{64}")
_OUTCOMES = frozenset({"success", "succeeded", "error", "denial", "denied", "failure", "failed",
                       "incomplete", "cancelled", "interrupted"})
_COUNTS = ("attempts", "pending", "succeeded", "failed", "cancelled", "retries",
           "recall_pages", "recall_bytes", "unknown_cost")
_USAGE = ("prompt_tokens", "completion_tokens", "cached_tokens", "total_tokens")
_COMPONENT_ID = re.compile(r"cc_[a-f0-9]{24}")
_BYTE_LOCATION = re.compile(r"([0-9]{1,39})–([0-9]{1,39}) of ([0-9]{1,39})")
_REPORTED_USAGE = re.compile(r"([0-9]{1,39}) reported; ([0-9]{1,39}) attempt\(s\) unknown")
_UNKNOWN_COST = re.compile(r"([0-9]{1,39}) attempt\(s\) unknown")


def _inspection(label, params):
    return Button(label=label, action="chrome_open", payload={"surface": "evidence", "params": params}).to_dict()


def _handoff():
    return Alert(message="Inspect this temporary evidence view in this conversation on a phone or desktop.",
                 variant="info").to_dict()


def _count(value):
    return type(value) is int and 0 <= value < 2**127


def _safe_usage(data):
    if (not all(_count(data.get(field)) for field in _COUNTS)
            or not all(type(data.get(field)) is bool for field in ("complete", "verified_cost", "recovery_complete"))
            or type(data.get("usage")) is not dict or set(data["usage"]) != set(_USAGE)
            or type(data.get("known_cost_by_currency")) is not dict or len(data["known_cost_by_currency"]) > 128):
        raise ValueError
    for amounts in data["usage"].values():
        if type(amounts) is not dict or set(amounts) != {"known", "unknown"} or not all(_count(n) for n in amounts.values()):
            raise ValueError
    for currency, amount in data["known_cost_by_currency"].items():
        if (type(currency) is not str or re.fullmatch(r"[A-Z]{3}", currency) is None
                or type(amount) is not str or re.fullmatch(r"[0-9]{1,64}(?:\.[0-9]{1,64})?", amount) is None):
            raise ValueError
    return {field: data[field] for field in (*_COUNTS, "usage", "known_cost_by_currency",
                                             "complete", "verified_cost", "recovery_complete")}


def persistent_reference_components(result: MCPResponse | None, *, kind: str | None = None,
                                    view_id: str | None = None, watch: bool = False) -> list[dict]:
    if kind is not None and type(kind) is not str:
        return evidence_components(state="blocked")
    if kind == "summary":
        components = evidence_components(state="summary")
        if (type(view_id) is not str or _VIEW.fullmatch(view_id) is None
                or result is not None and (type(result) is not MCPResponse or result.error)):
            return components + [Badge(label=_LABELS["missing"], variant="warning").to_dict(),
                Alert(message="Generated text was not saved. The temporary view is unavailable.", variant="warning").to_dict()]
        components.append(KeyValue(title="Temporary view", columns=1,
            items=[{"key": "Summary reference", "value": view_id}]).to_dict())
        components.append(_handoff() if watch else _inspection("Inspect generated summary", {"view_id": view_id}))
        return components
    if kind in {"missing", "blocked", "limit"}:
        return evidence_components(state=kind)
    if type(result) is not MCPResponse or result.error or type(result.result) is not dict:
        return evidence_components(state="blocked")
    data = result.result
    if kind is None:
        kind = ("preview" if data.get("view") == "partial_preview" else
                "source" if "reference" in data else "usage" if "attempts" in data else
                "missing" if data.get("status") in ("deleted", "unavailable") else "blocked")
    if kind in {"missing", "blocked"}:
        return evidence_components(state=kind)
    try:
        if kind == "usage":
            return usage_components(_safe_usage(data))
        reference, digest, outcome = data.get("reference"), data.get("digest"), data.get("outcome")
        if (kind not in {"source", "preview"} or type(reference) is not str or _REFERENCE.fullmatch(reference) is None
                or type(digest) is not str or _DIGEST.fullmatch(digest) is None or outcome not in _OUTCOMES):
            raise ValueError
        if data.get("view") == "partial_preview":
            start, end, total = 0, len(data["preview"].encode("utf-8")), data["total_bytes"]
        else:
            start, end, total = data["start"], data["end"], data["total"]
        if not all(_count(n) for n in (start, end, total)) or not start <= end <= total:
            raise ValueError
        components = evidence_components(state=kind, reference=reference, digest=digest,
            start=start, end=end, total=total, outcome=outcome)
        components.append(_handoff() if watch else _inspection(
            "Inspect captured source" if kind == "source" else "Inspect partial preview",
            {"kind": kind, "reference": reference, "offset": start}))
        return components
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
        return evidence_components(state="blocked")


def _reference_group(group):
    if type(group) is not list or not 2 <= len(group) <= 5 or any(type(item) is not dict for item in group):
        raise ValueError
    remaining, count = [(group, 0)], 0
    while remaining:
        value, depth = remaining.pop()
        count += 1
        if count > 1024 or depth > 8:
            raise ValueError
        if type(value) is dict:
            if len(value) > 12 or any(type(key) is not str or len(key) > 64 for key in value):
                raise ValueError
            remaining.extend((item, depth + 1) for item in value.values())
        elif type(value) is list:
            if len(value) > 160:
                raise ValueError
            remaining.extend((item, depth + 1) for item in value)
        elif type(value) is str:
            if len(value) > 512 or len(value.encode("utf-8")) > 2048:
                raise ValueError
        elif value is not None and type(value) not in (bool, int) or type(value) is int and not _count(value):
            raise ValueError
    clean = []
    for component in group:
        identity = component.get("component_id")
        if "component_id" in component and (type(identity) is not str or _COMPONENT_ID.fullmatch(identity) is None):
            raise ValueError
        clean.append({key: value for key, value in component.items() if key != "component_id"})
    return clean


def _usage_group(group):
    rows = group[1]["items"]
    if not 13 <= len(rows) <= 141:
        raise ValueError
    totals = {}
    for position, field in enumerate(_COUNTS[:-1]):
        label = "Provider responses received" if field == "succeeded" else field.replace("_", " ").capitalize()
        row = rows[position]
        if set(row) != {"key", "value"} or row["key"] != label or re.fullmatch(r"[0-9]{1,39}", row["value"]) is None:
            raise ValueError
        totals[field] = int(row["value"])
    totals["usage"] = {}
    for position, field in enumerate(_USAGE, start=8):
        row = rows[position]
        amounts = _REPORTED_USAGE.fullmatch(row["value"])
        if set(row) != {"key", "value"} or row["key"] != field.replace("_", " ").capitalize() or amounts is None:
            raise ValueError
        totals["usage"][field] = dict(zip(("known", "unknown"), map(int, amounts.groups()), strict=True))
    prices = {}
    for row in rows[12:-1]:
        currency = re.fullmatch(r"Reported cost \(([A-Z]{3})\)", row["key"])
        if set(row) != {"key", "value"} or currency is None or currency[1] in prices:
            raise ValueError
        prices[currency[1]] = row["value"]
    unknown = _UNKNOWN_COST.fullmatch(rows[-1]["value"])
    if set(rows[-1]) != {"key", "value"} or rows[-1]["key"] != "Cost uncertainty" or unknown is None:
        raise ValueError
    totals.update(unknown_cost=int(unknown[1]), known_cost_by_currency=prices,
        complete=group[0].get("label") == "Complete accounting",
        verified_cost=group[0].get("label") == "Complete accounting", recovery_complete=len(group) == 3)
    return persistent_reference_components(MCPResponse(result=totals), kind="usage")


def canonical_reference_components(group) -> list[dict] | None:
    try:
        clean = _reference_group(group)
        label = clean[0].get("label")
        if label in ("Complete accounting", "Partial accounting"):
            expected = _usage_group(clean)
        else:
            state = next((key for key, value in _LABELS.items() if value == label), None)
            if state in {"source", "preview"}:
                rows = clean[1]["items"]
                if len(rows) != 4:
                    raise ValueError
                location = _BYTE_LOCATION.fullmatch(rows[1]["value"])
                if location is None:
                    raise ValueError
                start, end, total = map(int, location.groups())
                expected = persistent_reference_components(MCPResponse(result={
                    "reference": rows[0]["value"], "start": start, "end": end, "total": total,
                    "digest": rows[2]["value"], "outcome": rows[3]["value"],
                }), kind=state, watch=clean[-1].get("type") == "alert")
            elif state == "summary":
                view_id = clean[2]["items"][0]["value"] if clean[2].get("type") == "keyvalue" else None
                expected = persistent_reference_components(None, kind="summary", view_id=view_id,
                    watch=clean[-1].get("type") == "alert")
            elif state in {"missing", "blocked", "limit"}:
                expected = persistent_reference_components(None, kind=state)
            else:
                return None
        if json.dumps(clean, sort_keys=True, allow_nan=False) != json.dumps(expected, sort_keys=True, allow_nan=False):
            return None
        return deepcopy(group)
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, IndexError):
        return None


def evidence_components(
    *, state: str, text: str = "", reference: str | None = None,
    digest: str | None = None, start: int = 0, end: int = 0,
    total: int = 0, next_offset: int | None = None,
    outcome: str | None = None,
) -> list[dict]:
    label = _LABELS[state]
    components = [Badge(label=label, variant="warning" if state != "source" else "info").to_dict()]
    if text:
        components.append(KeyValue(
            title=label, columns=1, items=[{"key": "Permitted text", "value": text}],
        ).to_dict())
    if reference:
        items = [{"key": "Observation", "value": reference},
                 {"key": "UTF-8 byte location", "value": f"{start}–{end} of {total}"}]
        if digest:
            items.append({"key": "Source SHA-256", "value": digest})
        if outcome:
            items.append({"key": "Operation outcome", "value": outcome})
        components.append(KeyValue(title="Source identity", columns=1, items=items).to_dict())
    if state in {"preview", "summary"}:
        components.append(Alert(
            message="This is a shortened working view. Omitted text is not evidence of absence or success.",
            variant="info",
        ).to_dict())
    if state in {"missing", "blocked", "limit"}:
        components.append(Alert(
            message=("History is retained. Reduce the next request or start an authorized continuation."
                     if state == "limit" else
                     "Use your authorized source tool to request a new snapshot. A stale reference cannot restore access."),
            variant="warning",
        ).to_dict())
    if reference and next_offset is not None:
        components.append(_inspection(
            "Inspect captured source" if state == "preview" else "Next source page",
            {"kind": "source", "reference": reference, "offset": next_offset}))
        components.append(Alert(
            message="Source inspection uses the same permissions. If this device cannot run the action, continue this conversation on a phone or desktop.",
            variant="info",
        ).to_dict())
    return components


def usage_components(totals: dict) -> list[dict]:
    items = []
    for field in ("attempts", "pending", "succeeded", "failed", "cancelled", "retries",
                  "recall_pages", "recall_bytes"):
        label = "Provider responses received" if field == "succeeded" else field.replace("_", " ").capitalize()
        items.append({"key": label, "value": str(totals.get(field, 0))})
    for field, amounts in totals.get("usage", {}).items():
        items.append({"key": field.replace("_", " ").capitalize(),
                      "value": f"{amounts['known']} reported; {amounts['unknown']} attempt(s) unknown"})
    for currency, cost in totals.get("known_cost_by_currency", {}).items():
        items.append({"key": f"Reported cost ({currency})", "value": str(cost)})
    items.append({"key": "Cost uncertainty", "value": f"{totals.get('unknown_cost', 0)} attempt(s) unknown"})
    complete = totals.get("complete") is True and totals.get("verified_cost") is True
    return [
        Badge(label="Complete accounting" if complete else "Partial accounting", variant="info").to_dict(),
        KeyValue(title="Whole conversation usage", items=items, columns=1).to_dict(),
        Alert(message="Unknown usage or prices do not count as zero or establish savings.", variant="info").to_dict(),
        *([Alert(message="Earlier or unlinked calls may be missing. These totals cover recorded attempts.",
                 variant="warning").to_dict()] if totals.get("recovery_complete") is False else []),
    ]
