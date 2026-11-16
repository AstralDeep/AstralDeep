# Caller ticket `astral.caller/v1`

A caller ticket lets a script, an MCP client or another agent call one personal node with no sign-in. An owner device mints it with a presence signature. The caller holds a separate key and signs every request with it. The ticket is not a secret, because a leaked ticket does nothing without the holder key.

It is a sibling of `astral.ticket/v1`, which stays unchanged. That ticket needs a turn block and a per-call action digest that a script cannot produce. Existing roster v2 stores keep working unchanged. This contract adds no event type, no roster field and no new body field. Devices on the deployed kernel fold a caller revoke as an ordinary `auth.revoke`.

Reference code: `reference/caller_ticket.py`. Generator: `reference/generate_caller_vectors.py`. Vectors: `vectors/caller-v1.json`. The signing helpers, LETS-CJ/1 and the tool definition hash come from the preserved 105 reference.

## 1. Ticket

A JSON object with exactly these fields. Any other field is `caller.malformed`.

| Field | Meaning |
|---|---|
| `v` | The integer 1 |
| `id` | base64url of 16 random bytes |
| `mesh` | The mesh id, `sha256:` and 64 lowercase hex characters |
| `device` | The minting member, a device id |
| `node` | The target member, a device id of kind `node` |
| `holder` | base64url of the caller's raw 32 byte Ed25519 public key |
| `label` | 1 to 64 characters typed by the owner, no control characters |
| `tools` | 1 to 32 entries sorted by `name` and without repeats, each with exactly `name` and `def` |
| `issued_ms` | Mint time in milliseconds |
| `expires_ms` | Expiry in milliseconds |
| `stop` | The mesh stop counter at mint |
| `sig` | The minting device's signature |

A tool entry names either a catalog tool or the reserved `a2a.message`. For a catalog tool `name` matches `[A-Za-z][A-Za-z0-9_-]{0,63}` and `def` is the `tools.md` definition hash taken from the node's current catalog. For `a2a.message`, `def` is `null` and the ticket lets the holder send A2A messages to the node.

`sig` is the minting device's presence key P-256 low-S signature over `astral.caller/v1\n` followed by the LETS-CJ/1 bytes of the object without `sig`. The signature is 64 bytes, `r` then `s`, as in the roster.

### Lifetime

The mint choices are 1 hour, 1 day, 7 days and 30 days. The default is 7 days. A ticket whose `expires_ms` is not after `issued_ms`, or is more than 30 days after it, is `caller.malformed`. A verifier refuses a ticket whose `issued_ms` is more than 5 minutes ahead of its own clock with `caller.not_yet_valid`.

## 2. Holder proof

Each request carries the header `Astral-Caller-Proof`. Its value is base64url of the LETS-CJ/1 bytes of an object with exactly these fields.

| Field | Meaning |
|---|---|
| `v` | The integer 1 |
| `ticket` | The ticket `id` |
| `method` | The HTTP method as sent |
| `target` | The path and query as sent, starting with `/` |
| `body` | `sha256:` and the hash of the exact body bytes, the hash of the empty string for no body |
| `ms` | Proof time in milliseconds |
| `nonce` | base64url of 16 random bytes |
| `sig` | The holder's Ed25519 signature |

`sig` covers `astral.caller-proof/v1\n` followed by the LETS-CJ/1 bytes of the object without `sig`. Any other field is `caller.proof_malformed`.

The proof window is 120 seconds on either side of the verifier's clock. A verifier refuses a nonce it has already accepted inside the window. A verifier also refuses a proof dated before the verifier started, so a restart cannot replay an old request. The verifier keeps nonces in memory only.

## 3. Transport

The ticket travels as `Authorization: AstralCaller <base64url ticket>`, where the base64url text encodes the LETS-CJ/1 bytes of the ticket. Each header is at most 8192 characters. A header over the limit, a missing or different scheme, or text that is not canonical base64url or strict LETS-CJ/1 is malformed.

Two text forms exist for copying and pasting.

- `astral-caller-ticket:v1:<base64url>` carries the ticket bytes.
- `astral-caller-key:v1:<base64url>` carries the 32 byte Ed25519 seed of the holder key.

The key form is a secret. The ticket form is not.

## 4. Verification

The verifier is given its own view of the mesh and does not read anything from the caller beyond the two headers and the request.

| Input | Meaning |
|---|---|
| `mesh` | The mesh the node is bound to |
| `node` | The node's own device id |
| `members` | Current members, each with its `presence` public key and `kind` |
| `removed` | Removed device ids |
| `stop` | The current stop counter |
| `revoked` | Caller ids named by `caller:<id>` revokes |
| `catalog` | The node's current catalog, tool name to definition hash |
| `now_ms`, `started_ms` | The verifier's clock and its start time |
| request | `method`, `target`, `body` and the `tool` the request names |

The first failure wins, in this order.

| Step | Check | Code |
|---|---|---|
| 1 | Ticket shape, lifetime, header size | `caller.malformed` |
| 2 | Proof shape and presence | `caller.proof_malformed` |
| 3 | `mesh` equals the verifier's mesh | `caller.mesh_mismatch` |
| 4 | `node` equals the verifier's node | `caller.wrong_node` |
| 5 | `device` is a current member and not removed | `caller.device_removed` |
| 6 | Presence signature verifies against that member's roster key | `caller.bad_signature` |
| 7 | The current stop counter is not above the ticket's `stop` | `caller.stopped` |
| 8 | `expires_ms` is after the verifier's clock | `caller.expired` |
| 9 | `issued_ms` is at most 5 minutes ahead | `caller.not_yet_valid` |
| 10 | The ticket `id` is not revoked | `caller.revoked` |
| 11 | The proof names this ticket, method, target and body hash | `caller.proof_mismatch` |
| 12 | The proof is inside the window and not before the verifier started | `caller.proof_stale` |
| 13 | The holder signature verifies against `holder` | `caller.proof_bad_signature` |
| 14 | The nonce was not accepted before inside the window | `caller.replayed` |
| 15 | The request's tool is in the ticket | `caller.tool_not_covered` |
| 16 | A catalog tool's `def` equals the node's current definition | `caller.tool_changed` |

A tool the ticket covers that the catalog no longer lists is `caller.tool_changed`. The tool `a2a.message` has no definition, so step 16 does not apply to it. A verifier records the nonce once step 14 passes, even if step 15 or 16 then refuses the request.

Two more codes sit outside the verifier because they depend on the endpoint. `caller.tool_unavailable` means the ticket covers a tool the endpoint cannot dispatch. `caller.no_catalog` means a mint was attempted against a node that has published no catalog.

Deep maps `caller.malformed`, `caller.proof_malformed`, `caller.bad_signature` and `caller.proof_bad_signature` to HTTP 401 and every other code to 403. A successful check returns the caller id, the label, the mesh and the covered tool. The endpoint compares the mesh with its bound owner.

## 5. Revocation

Any member revokes a ticket by writing an `auth.revoke` event whose body has agent `caller:<id>`, tool `*` and scope `*`. The authority fold accepts any agent string, so the deployed kernel folds it as an ordinary revoke that matches no real agent. The 105 grant check still passes for every ordinary action beside it, and the vector `caller_revoke_fold` pins that.

Removing the minting device or raising the stop also refuses its tickets. Deployed apps that list authority entries may show a `caller:<id>` revoke row.

## 6. Minting and node side

These parts belong to the kernel and are listed here so every implementation agrees.

- `mint_caller_ticket(node, holder_key, label, tool_names, a2a, lifetime_s)` runs on an owner device. It resolves each `def` from the node's current catalog fold, so a host never supplies a hash. The kernel draws the presence prompt from the ticket fields and never from model output.
- The minting device keeps an index of its tickets under the meta key `mesh.v2.callers`. `caller_tickets()` lists them with `revoked` and `expired`. `revoke_caller_ticket(id)` is idempotent.
- `verify_caller_request(...)` does every check in section 4, including replay.
- The node's published mesh catalog is the source of truth for tool names and definitions. MCP `tools/list` returns the ticket's tools that the endpoint can dispatch.

## 7. Vectors

`vectors/caller-v1.json` uses the vector format of the roster v2 contract. State is given to the verifier directly and is built from the roster v2 test devices with public test keys. Three case kinds exist.

- `caller_verify` has `state`, `authorization`, `proof` and `request`. The expectation is `{"ok": {"caller", "label", "mesh", "tool"}}` or `{"error": code}`.
- `caller_verify_sequence` has `state` and `steps`. Each step has the same fields as a single case. The expectation is `{"ok": {"results": [...]}}` with one result per step, and nonces carry from step to step.
- `caller_revoke_fold` has `caller`, `grants`, `revokes`, `action` and `effect_kind`. The expectation lists the revoked caller ids and shows that the ordinary grant check passes.

| Case | Outcome |
|---|---|
| `valid_mcp_tool` | accepted |
| `valid_a2a_message` | accepted |
| `lifetime_30_days_accepted` | accepted |
| `other_tool_refused` | `caller.tool_not_covered` |
| `a2a_not_covered_refused` | `caller.tool_not_covered` |
| `changed_definition_refused` | `caller.tool_changed` |
| `missing_catalog_tool_refused` | `caller.tool_changed` |
| `expired` | `caller.expired` |
| `lifetime_31_days` | `caller.malformed` |
| `future_issued_ms` | `caller.not_yet_valid` |
| `bad_presence_signature` | `caller.bad_signature` |
| `wrong_mesh` | `caller.mesh_mismatch` |
| `wrong_node` | `caller.wrong_node` |
| `removed_device` | `caller.device_removed` |
| `raised_stop` | `caller.stopped` |
| `revoked` | `caller.revoked` |
| `proof_method_mismatch`, `proof_target_mismatch`, `proof_body_mismatch`, `proof_ticket_mismatch` | `caller.proof_mismatch` |
| `stale_proof`, `proof_before_verifier_start` | `caller.proof_stale` |
| `bad_holder_signature` | `caller.proof_bad_signature` |
| `replayed_nonce` | accepted, then `caller.replayed` |
| `unknown_ticket_field`, `unsorted_tools`, `header_too_long`, `wrong_scheme` | `caller.malformed` |
| `unknown_proof_field`, `missing_proof` | `caller.proof_malformed` |
| `first_failure_wins` | `caller.mesh_mismatch` |
| `caller_revoke_fold` | revoked id listed, ordinary grant passes |

Regenerate with `python specs/106-sign-in-free-clients/contracts/reference/generate_caller_vectors.py` and verify with `--check`.
