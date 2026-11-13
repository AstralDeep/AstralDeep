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
