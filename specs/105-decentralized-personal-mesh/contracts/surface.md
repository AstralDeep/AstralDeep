# Surface contract, `astral.surface/v1`

A surface is a live piece of generated interface that every device shows from the same events. It is a document with stable component ids, changed by patches, driven by action events, and bound to values that tools returned. This document fixes those formats and the rules a validator applies. The vocabulary of component types is not defined here. It comes from the published primitives schema (`ui_protocol.json`), and the validator is given it as input.

Reference code: `reference/astral_mesh_ref/surface.py`. Vectors: `surface.json`.

## 1. The document

A surface document is a JSON object with exactly these fields.

| Field | Type | Rule |
|---|---|---|
| `v` | integer | `1` |
| `surface` | string | The surface id. `new` in the first `ui.surface_put` of a surface. Its event id is then the surface id |
| `root` | string | Id of the root component |
| `components` | object | Component id to component. At most 512 |
| `capabilities` | object | Capability id to capability |

A component id matches `^[a-z][a-z0-9_.-]{0,63}$`. A **component** is an object with exactly:

| Field | Rule |
|---|---|
| `id` | Equals its key in `components` |
| `type` | A name in the vocabulary |
| `props` | An object of JSON values with no floats |
| `children` | Array of component ids |
| `prov` | `model`, `tool` or `host` |

The tree rules: every child exists, every component has at most one parent, the root has none, no cycles, depth at most 32, and every component is reachable from the root. Otherwise `surface.malformed`.

The model supplies the document. The kernel sets `prov` to say who produced each node: a model, a tool result or the host. A document that a model produced and that claims `host` is refused with `surface.false_provenance`. The host is the only producer of consent prompts, so the vocabulary offered to the model has no consent component. A type outside the vocabulary raises `surface.unknown_type`.

## 2. Properties

- **Handles.** A property named `href` or `src` must be exactly `{"$handle": "<id>"}`. The model supplies handles and the host resolves them. A string there raises `surface.raw_url`. This closes the path where a rendered link carries data out.
- **Bindings.** Any property value may be `{"$bind": {"event": <event id>, "path": <JSON pointer>}}`. The shown value is the one the tool returned, not one the model typed. A binding with other fields raises `surface.malformed`.
- **Completeness.** A component is **actionable** when its type is in the actionable set the validator was given, or when it has an `action` property. An actionable component must have an `action` property of the form `{"capability": <id>}` and non-empty string properties `name` (the accessible name), `spoken` (what a voice client says) and `compact` (the form for a watch). Otherwise `surface.incomplete`. This is what makes every surface work on a watch and by voice.
- **Action reference.** `action.capability` names a capability of the document (`surface.unknown_capability`).

## 3. Capabilities

A capability is an object with exactly:

| Field | Meaning |
|---|---|
| `tool` | The tool the component may cause |
| `args` | Arguments fixed by the surface. Values are JSON with no floats |
| `free` | Sorted names of arguments the person may fill when acting |
| `consent` | `required` or `policy` |
| `max_uses` | Integer at least 1 |

A surface cannot grant itself more than the turn that created it had. The validator is given the turn: `{"tools": [...], "max_uses": int, "tool_consent": {tool: "required"|"policy"}}`. A capability for a tool the turn lacks, with more uses than the turn, or with `consent` `policy` for a tool the turn requires consent for, raises `surface.capability_exceeds_turn`.

## 4. Patches

`ui.surface_put` has body `{"document": <document>, "turn": <id>}`. `turn` is the id of the `chat.turn_start` event of the turn that made the surface. The fold ignores it and section 6 uses it. `ui.patch` has body `{"surface": <id>, "ops": [...]}`. An op is one of:

| Op | Fields | Effect |
|---|---|---|
| `set` | `id`, `prop`, `value` | Sets a property |
| `unset` | `id`, `prop` | Removes a property |
| `add` | `parent`, `index` (int or null for the end), `component` | Inserts a component under a parent |
| `remove` | `id` | Removes a component and its subtree. The root cannot be removed |

An op that names a missing component, adds an id that exists, or removes the root is skipped. This is how concurrent edits stay harmless. An unknown op raises `surface.patch_invalid`.

## 5. The fold

The input is the `ui.surface_put` and `ui.patch` events of a surface. Each is given as `{type, feed, seq, clock, body}`.

1. Sort the events by `(clock.ms, clock.n, feed, seq)`. This agrees with causal order because clocks follow `event.md` section 4.
2. A put replaces the whole document if the new document validates. Otherwise it is rejected and the old document stays.
3. A patch applies to the current document. A patch before any put is ignored. The patched document is validated in full. If it fails, the whole patch is rejected and the document is unchanged.

Concurrent patches to one property end with the later clock, then the greater feed id. The result lists the final `document` and `rejected` (feed, seq and error for each rejected event).

## 6. Action events

`ui.action` has body `{"surface", "component", "capability", "args"}`. A peer that executes it checks, against the document as folded up to that point:

1. The component exists and has an `action` (`surface.unknown_component`).
2. `capability` equals the component's (`surface.unknown_capability`).
3. Every fixed argument is present with the fixed value, and no argument outside `args` and `free` appears (`surface.action_args`).
4. The number of earlier actions with this capability is below `max_uses` (`surface.capability_exhausted`).

The result is the tool, the consent mode and the arguments. The executing peer then runs the gates of `labels.md` and verifies or asks for a ticket as for any call. Pressing a control writes a signed action event. It never executes anything by itself.

### Acting on a surface

A kernel that is asked to press a control, given the surface id, the component and the arguments, does these steps in order.

1. It finds the surface. The log must hold a `ui.surface_put` whose event id is the surface id (`surface.unknown_surface`). The surface's events are that put, later puts whose document names the surface, and the patches and actions that name it. They fold under the turn rules of section 3, and the four checks above run against the folded document. The capability is the one the component declares in the document. An action whose own `capability` field is present and differs from it is `surface.unknown_capability`, and an absent field takes the declared one. `used` is the number of earlier actions with the declared capability that passed, counted in clock order. If the fold holds no accepted document, the refusal is the code of the first rejected put, so a surface that was wider than its turn refuses with `surface.capability_exceeds_turn`. Vectors: `surface_act`.
2. The first put names the originating turn in `turn`. It must be a turn of the same chat (`surface.no_origin_turn`).
3. It writes a signed `ui.action` event. A use of the capability is spent when this event is written, whatever the gates then decide.
4. It runs the capability's tool as a new turn that continues the originating turn. The start event is `{"continues": <originating turn>, "message": null, "action": {"event": <ui.action id>, "consent": <capability consent>}}`. The turn has one step, the call, which passes the gates of `labels.md` like any call. Consent is asked when policy requires it and also when the capability says `required`. The turn starts from the labels of the turn it continues, so it carries no less caution than that turn.

The turn rules of section 3 are taken from the executing device: `tools` is what it has granted to the agent, `max_uses` is its limit on rounds in one turn, and `tool_consent` is `required` for a tool that a narrowing rule sends to consent.

## 7. Resolving a binding

The event named must be a `chat.tool_result` that the peer holds (`surface.bind_unknown_event`, `surface.bind_not_tool_result`). The path is a JSON pointer into the event's body, with `~1` for a slash and `~0` for a tilde. The empty path is the whole body. A path that does not resolve raises `surface.bind_path`. The value must be a string, boolean, integer or null (`surface.bind_not_scalar`). Numbers are shown exactly as returned.

## 8. Errors

`surface.malformed`, `surface.unknown_type`, `surface.false_provenance`, `surface.raw_url`, `surface.incomplete`, `surface.unknown_capability`, `surface.capability_exceeds_turn`, `surface.patch_invalid`, `surface.unknown_component`, `surface.action_args`, `surface.capability_exhausted`, `surface.bind_unknown_event`, `surface.bind_not_tool_result`, `surface.bind_path`, `surface.bind_not_scalar`, `surface.unknown_surface`, `surface.no_origin_turn`.
