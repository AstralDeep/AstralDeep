# Label and gate contract, `astral.labels/v1`

Flow labels mark how far content can be trusted and where it may go. Deterministic code enforces rules on them. This document fixes the two axes, how labels combine, the running label of a turn, quarantined reader outputs, the flow rules and the gate order for one effect.

Reference code: `reference/astral_mesh_ref/labels.py`. Vectors: `labels.json`.

## 1. The axes

| Axis | Values, from most to least trusted or from least to most confidential | Assigned by |
|---|---|---|
| `integrity` | `owner`, `tool-trusted`, `untrusted` | The kernel, from the event's source. A tool cannot label its own output |
| `confidentiality` | `public`, `private`, `phi`, `secret` | The source's declared class, raised by content checks |

`owner` is typed or spoken by the owner. `tool-trusted` is a first-party tool's own computation over data the owner wrote. `untrusted` is everything else: web pages, email, remote tool output, files from elsewhere, other agents, audio that is not the owner.

A label is `{"confidentiality": ..., "integrity": ...}`. Any other value raises `labels.invalid`.

## 2. Combining

The **join** of labels takes the least trusted integrity and the most confidential confidentiality. The join of no labels is `{owner, public}`.

An event derived from other events (a memory, a compaction note, a learned item) takes the join of its sources and of its own declared class. Untrusted text cannot be laundered by being remembered.

The **running label** of a turn is the join of the labels of everything loaded into the model's context: earlier messages, memory, notes and skills, and what arrives during the turn. Once untrusted content is in the context, what the model produces afterwards is treated as influenced by it.

## 3. Quarantined readers

A model call with no tools that must answer in a fixed schema may read untrusted content. Its answer is a value `{"integrity": "untrusted", "value": ..., "steers_turn": false}`. The value may fill an argument. It does not join the turn's running label because it is not loaded into the context. A value outside its schema raises `labels.quarantine_schema`. A value that fills an argument of an outside effect is still an untrusted argument (section 4).

## 4. Flow rules

The rules take a turn and an effect.

The turn is `{"context": [labels], "private_access": bool, "secret_in_context": bool}`. `private_access` means the turn can reach private data through its context or its tools.

The effect is `{"kind", "target", "argument_integrity": [integrity of each argument value], "endpoint_phi_approved": bool}`. The kinds are `mesh_read`, `mesh_write`, `outward_read` (a fetch or a search) and `outside_effect` (send, post, buy, delete elsewhere). The targets are `local_tool`, `remote_tool`, `model` and `sync`. Other values raise `labels.invalid`.

Evaluation order, with the first refusal ending it:

| Rule | Outcome |
|---|---|
| Target `model`, and a secret value is in the context or `secret_in_context` is set | Refuse, `flow.secret_in_context` |
| Target `model`, running confidentiality is `phi`, and the endpoint is not PHI-approved | Refuse, `flow.phi_to_model`. An on-device model is always approved |
| Target `sync` | Allow here. The key group decides who receives bodies (`sync.md`) |
| Running integrity is `untrusted` and the kind is `outside_effect` | Consent, reason `untrusted_influence` |
| Running integrity is `untrusted`, `private_access` is set, and the kind is `outside_effect` or `outward_read` | Consent, reason `rule_of_two` |
| The kind is `outside_effect` and any argument is untrusted | Consent, reason `untrusted_argument` |

Reasons are collected in the order shown, so the consent prompt can show the source of the influence. To keep researching without a prompt each time, the engine runs outward work in a sub-turn whose context holds no private data and takes the results back as untrusted content. The rule that an executor may refuse any call with labels it dislikes is the executor's (`ticket.md`).

The result is `{"outcome": "allow" or "consent" or "refuse", "code": string or null, "reasons": [...]}`.

## 5. Gate order

One effect passes these steps in order. The first refusal ends it. The input is the state of each step plus the turn and effect above.

| Step | Gate | Refusal |
|---|---|---|
| 1 | The mesh is not halted (its stop counter is above its resumed counter, `roster.md` section 5) and the proposing device is on the roster | `gate.mesh_stopped`, `gate.device_not_on_roster` |
| 2 | No hard security flag, and the tool definition hash matches the one pinned at approval | `gate.hard_flag`, `gate.tool_def_changed` |
| 3 | Permission: the tool is allowed for this owner, agent and scope | `gate.permission_denied` |
| 4 | Policy rules, merged by narrowing only. Result `allow`, `consent` or `deny` | `gate.policy_denied` for `deny`. `consent` adds the reason `policy` |
| 5 | Flow rules of section 4 | `flow.secret_in_context`, `flow.phi_to_model`. Consent reasons are added |
| 6 | Consent, when any earlier step asked for it, or when the allowance is `solo` (reason `solo_allowance`) | Outcome `consent`, code `consent.required`, with the reasons, unless consent is already given |
| 7 | Allowance: for `mesh_write` and `outside_effect`, a LETS receipt is issued and counted. Reads (`mesh_read`, `outward_read`) have no budget class, so they pass this step with no receipt and the ticket carries a null one (`ticket.md` section 6) | `allowance.exhausted`. This applies even when consent is given |
| 8 | Dispatch with an idempotency key | not a refusal. The allow result reports gate 8 |
| 9 | The result is written as a signed event with its labels | not part of the pure function |

The output is `{"gate": n, "outcome", "code", "reasons"}`. A consent outcome is not an error. It means the host must draw a prompt from the canonical action and collect a presence-key signature. The signature covers the action digest (`ticket.md`), and the digest is checked again at dispatch. Steps 4 and 5 are on by default in the kernel.

The fixed order matters. A halted mesh refuses every new effect, reads included, and outranks a hard flag, a hard flag outranks permission, and so on. A refusal never waits on consent, and consent comes before the allowance is spent.

## 6. Credentials

A secret value is never an event body that every device can read. It is a credential item (`keys.md` section 4.1), a `key.credential` event with a secret label whose body holds one box per recipient device. The value is opened only on a recipient device and only where the kernel injects it at dispatch. It does not enter any model context, log, error or binding return. A body sealed only under a content key with a secret label is refused (`credential.not_sealed`). Sync sends the body of a credential item to every member and withholds the body of any other secret event (`sync.md` section 5).
