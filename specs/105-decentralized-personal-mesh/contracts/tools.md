# Tool catalog contract, `astral.tools/v1`

A member advertises the tools it serves so that other members can offer them to their models. This document fixes the event body, the definition hash, the fold that picks each member's current catalog, and how a changed definition is treated.

Reference code: `reference/astral_mesh_ref/tools.py`. Vectors: `tools.json`.

## 1. The event

`tools.catalog` has the family `tools` and the scope `mesh`. A catalog with a chat scope is `event.malformed`. The body has exactly one field.

| Field | Rule |
|---|---|
| `tools` | A list of at most 64 tools. Each catalog replaces the writer's earlier one, so an empty list withdraws every tool |

A tool has exactly these fields.

| Field | Rule |
|---|---|
| `name` | `^[A-Za-z][A-Za-z0-9_-]{0,63}$`. Unique in the catalog |
| `description` | A string of at most 4096 characters |
| `input_schema` | A JSON object |
| `annotations` | A JSON object |
| `effect` | `mesh_read`, `mesh_write`, `outward_read` or `outside_effect` |
| `hash` | The definition hash |

Event bodies carry no floats. A number with a decimal part inside `input_schema` or `annotations` is written as `{"$num": "<decimal text>"}`, and the hash covers that spelling. A reader restores the number before it uses the schema.

Any other shape raises `tools.malformed`. A hash that differs from the recomputed one raises `tools.hash_mismatch`.

## 2. The definition hash

`hash` is the `sha256:` id of the canonical JSON of the object `{"name", "description", "input_schema", "annotations"}`. It is the same hash that pins an MCP tool definition.

## 3. The fold

The input is the `tools.catalog` events and a map from feed to device that lists only the current members of the roster. The fold is a pure function of the set of events.

1. Sort the events by `(clock.ms, clock.n, feed, seq)`.
2. An event from a feed that no current member owns is ignored. This drops the catalogs of removed members.
3. An event whose body is invalid is ignored.
4. For each device the last remaining event is its catalog. Its earlier catalogs are ignored.

The result is `{"catalogs": [...], "ignored": [...]}`. Each catalog is `{device, event, tools}` and the list is sorted by device. `ignored` is the sorted ids of the ignored events.

## 4. Use

A kernel offers the tools of the other members' catalogs to its model. It never offers a tool from its own catalog, since it already has those tools.

A tool is identified by its device, its name and its hash. A tool whose definition changed has a new hash, so it is a new tool. The model sees the plain name. The grant for it names `<name>@<first 8 hex digits of the hash>`, so a grant for the old definition does not cover the new one and the owner must grant it again.

Every call to a peer tool still needs a consented grant, passes the gates of `labels.md` and carries a ticket (`requests.md`). The member that serves the tool derives the effect kind itself and runs its own gates. The `effect` in a catalog only guides the caller's own gates.

## 5. Errors

`tools.malformed`, `tools.hash_mismatch`.

## 6. Local model proposals

The Kernel may attach `x-astral-tool-proposals` to the JSON Schema in its host-local on-device request. Version 1 contains `version: 1` and `tools`, with each descriptor carrying only `name`, `description` and `parameters`. This metadata is not a catalog event, a credential, a tool authorization or a cloud-provider request field. The normal response remains `{text, calls: [{tool, arguments_json}]}`; the Kernel bounds and validates it before normal admission, argument validation, consent, ticketing, dispatch and journaling.

A native host may use a model's typed tool interface only to capture an unexecuted proposal and return that envelope. Its callback must not execute the named tool, return a success result for it, expose preliminary proposal text as an answer, or return a captured proposal after cancellation, malformed arguments or conflicting callbacks. Unsupported structured capability is reported explicitly; the existing announced text-only fallback may then apply. Ordinary model refusals and generation failures are not capability fallback.

The optional `x-astral-on-device-schema` annotation supplies a narrower generation schema. It never replaces the authoritative tool input schema or the runtime surface validator and is included in the ordinary definition hash. The built-in local surface profile derives text and card fields from the pinned Primitives profile, with a bounded list of text children and no controls, links, capabilities or bound values. Cloud generation retains the full tool schema. No new primitive or external protocol is introduced.
