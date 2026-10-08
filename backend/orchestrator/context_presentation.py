"""Builds literal, server-authorized evidence views with the pinned primitive vocabulary.
Evidence tools and the conversation loop deliver these views transiently through Projection.
"""

from astralprims import Alert, Badge, Button, KeyValue

_LABELS = {
    "source": "Captured source",
    "preview": "Partial preview",
    "summary": "Generated summary",
    "missing": "Source unavailable",
    "blocked": "Recall blocked",
    "limit": "Context limit",
}


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
        components.append(Button(
            label="Inspect captured source" if state == "preview" else "Next source page",
            action="chat_message", payload={"message": f"/evidence recall {reference} {next_offset}"},
        ).to_dict())
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
