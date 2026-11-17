# Contracts for spec 106

These documents fix the formats that spec 106 revises or adds. Each one has a Python reference and deterministic vectors. The Kernel, Deep, Vessel and the Apple apps implement against the vectors byte for byte.

## Index

| Document | Format version | Covers | Vectors | Reference |
|---|---|---|---|---|
| [roster-v2.md](roster-v2.md) | `astral.roster/v2` | The accountless roster, device add and removal, stop and resume, creation and recovery export | `vectors/roster-v2.json`, `vectors/crypto-v2.json` | `reference/generate_local_vectors.py` |
| [caller-ticket.md](caller-ticket.md) | `astral.caller/v1` | The owner-minted caller ticket, the holder proof, the holder text forms, the verification order and codes, revocation | `vectors/caller-v1.json`, `reference/label_classes.json` | `reference/caller_ticket.py`, `reference/ed25519_strict.py`, `reference/generate_caller_vectors.py`, `reference/generate_label_classes.py` |
| [exec-policy.md](exec-policy.md) | `astral.exec-policy/v1` | The execution policy for untrusted node tools, its validation order and codes, the canonical Seatbelt profile, the Linux plan, backend selection | `vectors/exec-policy-v1.json` | `reference/exec_policy.py`, `reference/generate_exec_vectors.py`, `reference/path_fold.json` |
| [connection.md](connection.md) | `astral.connection/v1` | Connections carried on `settings.set`: the key grammar, the record and per-device state, the consent digest, the fold and its codes, unsupported records, server ids and tool names, egress, and the MCP over HTTP adapter decisions | `vectors/connection-v1.json`, `vectors/connection-oauth-v1.json` | `reference/connection.py`, `reference/connection_oauth.py`, `reference/generate_connection_vectors.py` |
| [phi-detector.md](phi-detector.md) | `astral.phi-detector/v1` | The PHI detector floor: input limits, normalization and the skeleton fold, the pattern dialect, the rule table and its tiers, failure reasons, parity with today's Python rules, and the on-device filter request | `vectors/phi-detector-v1.json` | `reference/phi_detector.py`, `reference/phi_rules.json`, `reference/phi_fold.json`, `reference/html_entities.json`, `reference/phi_filter.json`, `reference/generate_phi_vectors.py`, `reference/generate_phi_fold.py`, `reference/generate_html_entities.py` |
| [phi-destinations.md](phi-destinations.md) | `astral.phi-destination/v1` | The PHI group on the roster's PHI entries, approver keys and approvals, upgrade markers, inclusion, exclusion and the keeper, canonical destinations, the view and its codes, and the chat PHI key | `vectors/phi-destination-v1.json` | `reference/phi_destinations.py`, `reference/generate_phi_destination_vectors.py` |
| [labels-v2.md](labels-v2.md) | `astral.labels/v2` | The 105 label contract with the outside-service flag and the refusal of PHI to an unapproved outside service | `vectors/labels-v2.json` | `reference/labels_v2.py`, `reference/generate_phi_destination_vectors.py` |

A change to a format raises its version. A document, its vectors and its reference module change together, and the reference tests pass before the change merges.

## Relation to spec 105

The contracts in [spec 105](../../105-decentralized-personal-mesh/contracts/README.md) are history. Where a contract here covers the same ground, this folder wins. The 105 reference helpers for canonical JSON, hashes, signatures and test keys are still imported by the references here, and every other 105 contract applies until a document here replaces it.

Existing roster v2 stores keep working unchanged. Nothing in this folder adds an event type or a body field that the deployed kernel cannot read. The connection contract adds a `consent` field to some `settings.set` bodies, which deployed kernels accept and fold as an unread setting. The PHI destination contract changes no roster format. It writes `phi_authority`, `phi_approval`, `epoch` and `wrap` entries that deployed kernels already fold the same way, and one `settings.set` key, `phi.capable/<device id>`, that they fold as an unread setting.

## Running the checks

From the repository root, with the packages in `specs/105-decentralized-personal-mesh/contracts/reference/requirements.txt` installed.

```
python specs/106-sign-in-free-clients/contracts/reference/generate_caller_vectors.py --check
python specs/106-sign-in-free-clients/contracts/reference/generate_local_vectors.py --check
python specs/106-sign-in-free-clients/contracts/reference/generate_exec_vectors.py --check
python specs/106-sign-in-free-clients/contracts/reference/generate_connection_vectors.py --check
python specs/106-sign-in-free-clients/contracts/reference/generate_phi_vectors.py --check
python specs/106-sign-in-free-clients/contracts/reference/generate_phi_destination_vectors.py --check
python -m pytest specs/106-sign-in-free-clients/contracts/reference/tests -q
ruff check specs/106-sign-in-free-clients/contracts
python3.14 specs/106-sign-in-free-clients/contracts/reference/generate_label_classes.py --check
python3.14 specs/106-sign-in-free-clients/contracts/reference/generate_path_fold.py --check
python3.14 specs/106-sign-in-free-clients/contracts/reference/generate_phi_fold.py --check
python3.14 specs/106-sign-in-free-clients/contracts/reference/generate_html_entities.py --check
```

The last four commands need Python 3.14. They use only the standard library. The first three refuse to run unless `unicodedata.unidata_version` is 16.0.0, and the last refuses any Python other than 3.14. The caller ticket label table, the exec policy path fold tables, the PHI skeleton fold and the HTML reference table come from them, and every implementation reads those tables instead of its runtime's Unicode or HTML data. The exec policy live test runs only on macOS.
