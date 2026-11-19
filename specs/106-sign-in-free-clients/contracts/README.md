# Contracts for spec 106

These documents fix the formats that spec 106 revises or adds. Each one has a Python reference and deterministic vectors. The Kernel, Deep, Vessel and the Apple apps implement against the vectors byte for byte.

## Index

| Document | Format version | Covers | Vectors | Reference |
|---|---|---|---|---|
| [roster-v2.md](roster-v2.md) | `astral.roster/v2` | The accountless roster, device add and removal, stop and resume, creation and recovery export | `vectors/roster-v2.json`, `vectors/crypto-v2.json` | `reference/generate_local_vectors.py` |
| [caller-ticket.md](caller-ticket.md) | `astral.caller/v1` | The owner-minted caller ticket, the holder proof, the holder text forms, the verification order and codes, revocation | `vectors/caller-v1.json`, `reference/label_classes.json` | `reference/caller_ticket.py`, `reference/ed25519_strict.py`, `reference/generate_caller_vectors.py`, `reference/generate_label_classes.py` |
| [exec-policy.md](exec-policy.md) | `astral.exec-policy/v1` | The execution policy for untrusted node tools, its validation order and codes, the canonical Seatbelt profile, the Linux plan, backend selection | `vectors/exec-policy-v1.json` | `reference/exec_policy.py`, `reference/generate_exec_vectors.py`, `reference/path_fold.json` |
| [connection.md](connection.md) | `astral.connection/v1` | Connections carried on `settings.set`: the key grammar, the record and per-device state, the consent digest, the fold and its codes, unsupported records, server ids and tool names, egress, the MCP over HTTP adapter decisions, the kinds `a2a`, `mcp-stdio`, `http-api` and `model`, the A2A card decisions, and pasted keys | `vectors/connection-v1.json`, `vectors/connection-oauth-v1.json`, `vectors/a2a-card-v1.json` | `reference/connection.py`, `reference/connection_oauth.py`, `reference/a2a_card.py`, `reference/generate_connection_vectors.py`, `reference/generate_a2a_vectors.py` |
| [credential-items.md](credential-items.md) | `astral.credential-items/v1` | The content rule of `key.credential` items: names and families, the size limit, the refusal of sign-in tokens, the shape of connection key items, the secrets a record needs and who may write | `vectors/credential-items-v1.json` | `reference/credential_items.py`, `reference/generate_credential_vectors.py` |
| [phi-detector.md](phi-detector.md) | `astral.phi-detector/v1` | The PHI detector floor: input limits, normalization and the skeleton fold, the pattern dialect, the rule table and its tiers, failure reasons, parity with today's Python rules, and the on-device filter request | `vectors/phi-detector-v1.json` | `reference/phi_detector.py`, `reference/phi_rules.json`, `reference/phi_fold.json`, `reference/html_entities.json`, `reference/phi_filter.json`, `reference/generate_phi_vectors.py`, `reference/generate_phi_fold.py`, `reference/generate_html_entities.py` |
| [phi-destinations.md](phi-destinations.md) | `astral.phi-destination/v1` | The PHI group on the roster's PHI entries, approver keys and approvals, upgrade markers, inclusion, exclusion and the keeper, canonical destinations, the view and its codes, and the chat PHI key | `vectors/phi-destination-v1.json` | `reference/phi_destinations.py`, `reference/generate_phi_destination_vectors.py` |
| [labels-v2.md](labels-v2.md) | `astral.labels/v2` | The 105 label contract with the outside-service flag and the refusal of PHI to an unapproved outside service | `vectors/labels-v2.json` | `reference/labels_v2.py`, `reference/generate_phi_destination_vectors.py` |
| [contacts.md](contacts.md) | `astral.contacts/v1` | Contacts and grants carried on `settings.set`: the key grammar, the invitation, the roster digest and comparison code, the contact record and its fold with roster marks, how a verified contact follows the other mesh's roster and which rejected entries downgrade it, the grant record and its fold, revocation and rotation, the consent digests and every code | `vectors/contacts-v1.json` | `reference/contacts.py`, `reference/generate_contact_vectors.py` |
| [share.md](share.md) | `astral.share/v1` | The share session on ALPN `astral/share/1`: the session kinds and messages, admission, the served types and body fields over a chat including surface-bound tool results, the wraps of content keys, the share envelope and its header key, the receiver steps, the chunk policy, PHI gates, liveness and every code | `vectors/share-v1.json` | `reference/share.py`, `reference/generate_share_vectors.py` |
| [caller-events.md](caller-events.md) | `astral.caller-events/v1` | What a principal can observe about a node: the caller refusal codes and their classes, the request lifecycle with no notifications, and who may see a pending consent | `vectors/caller-events-v1.json` | `reference/caller_events.py`, `reference/generate_caller_event_vectors.py` |
| [descriptor.md](descriptor.md) | \stral.node.descriptor/v2\ | The discovery document a client fetches before it trusts a node: the field table, the capability promises, the mesh extension fields, verification and versioning | \ectors/descriptor-v2.json\ | eference/descriptor.py\ |
| [caller-turn-door.md](caller-turn-door.md) | \stral.caller-turn/v0\ (DRAFT) | The caller turn door: the assembly door declaration, the one-POST lifecycle through the mesh bridge runner, the consent relay per caller-events/v1, label floors, refusals and limits — draft for co-design, no node code | \ectors/caller-turn-door-v0.json\ | eference/caller_turn_door.py\ |
| [assembly.md](assembly.md) | `astral.assembly/v0` | The activation manifest: one adapter per port, pinned contract documents, offered capabilities, required controls, what each door gives each principal class, the validation order and every code | `vectors/assembly-v0.json` | `reference/assembly.py`, `reference/generate_assembly_vectors.py` |

A change to a format raises its version. A document, its vectors and its reference module change together, and the reference tests pass before the change merges.

## Relation to spec 105

The contracts in [spec 105](../../105-decentralized-personal-mesh/contracts/README.md) are history. Where a contract here covers the same ground, this folder wins. The 105 reference helpers for canonical JSON, hashes, signatures and test keys are still imported by the references here, and every other 105 contract applies until a document here replaces it.

Existing roster v2 stores keep working unchanged. Nothing in this folder adds an event type or a body field that the deployed kernel cannot read. The connection contract adds a `consent` field to some `settings.set` bodies, which deployed kernels accept and fold as an unread setting. The PHI destination contract changes no roster format. It writes `phi_authority`, `phi_approval`, `epoch` and `wrap` entries that deployed kernels already fold the same way, and one `settings.set` key, `phi.capable/<device id>`, that they fold as an unread setting. The contacts contract adds four families of `settings.set` keys, `contact/`, `contact.roster/`, `share/` and `share.revoked/`, and the consent kinds `contact` and `share`, which deployed kernels fold as unread settings and never read. The share contract adds no event type or body field. It adds the ALPN `astral/share/1`, which a deployed kernel refuses with `net.unknown_alpn`, and member sync is unchanged.

## Running the checks

From the repository root, with the packages in `specs/105-decentralized-personal-mesh/contracts/reference/requirements.txt` installed.

```
python specs/106-sign-in-free-clients/contracts/reference/generate_caller_vectors.py --check
python specs/106-sign-in-free-clients/contracts/reference/generate_local_vectors.py --check
python specs/106-sign-in-free-clients/contracts/reference/generate_exec_vectors.py --check
python specs/106-sign-in-free-clients/contracts/reference/generate_connection_vectors.py --check
python specs/106-sign-in-free-clients/contracts/reference/generate_a2a_vectors.py --check
python specs/106-sign-in-free-clients/contracts/reference/generate_credential_vectors.py --check
python specs/106-sign-in-free-clients/contracts/reference/generate_phi_vectors.py --check
python specs/106-sign-in-free-clients/contracts/reference/generate_phi_destination_vectors.py --check
python specs/106-sign-in-free-clients/contracts/reference/generate_caller_event_vectors.py --check
python specs/106-sign-in-free-clients/contracts/reference/generate_assembly_vectors.py --check
python specs/106-sign-in-free-clients/contracts/reference/generate_contact_vectors.py --check
python specs/106-sign-in-free-clients/contracts/reference/generate_share_vectors.py --check
python -m pytest specs/106-sign-in-free-clients/contracts/reference/tests -q
ruff check specs/106-sign-in-free-clients/contracts
python3.14 specs/106-sign-in-free-clients/contracts/reference/generate_label_classes.py --check
python3.14 specs/106-sign-in-free-clients/contracts/reference/generate_path_fold.py --check
python3.14 specs/106-sign-in-free-clients/contracts/reference/generate_phi_fold.py --check
python3.14 specs/106-sign-in-free-clients/contracts/reference/generate_html_entities.py --check
```

The last four commands need Python 3.14. They use only the standard library. The first three refuse to run unless `unicodedata.unidata_version` is 16.0.0, and the last refuses any Python other than 3.14. The caller ticket label table, the exec policy path fold tables, the PHI skeleton fold and the HTML reference table come from them, and every implementation reads those tables instead of its runtime's Unicode or HTML data. The exec policy live test runs only on macOS.
