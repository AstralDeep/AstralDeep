# Contracts for spec 106

These documents fix the formats that spec 106 revises or adds. Each one has a Python reference and deterministic vectors. The Kernel, Deep, Vessel and the Apple apps implement against the vectors byte for byte.

## Index

| Document | Format version | Covers | Vectors | Reference |
|---|---|---|---|---|
| [roster-v2.md](roster-v2.md) | `astral.roster/v2` | The accountless roster, device add and removal, stop and resume, creation and recovery export | `vectors/roster-v2.json`, `vectors/crypto-v2.json` | `reference/generate_local_vectors.py` |
| [caller-ticket.md](caller-ticket.md) | `astral.caller/v1` | The owner-minted caller ticket, the holder proof, the holder text forms, the verification order and codes, revocation | `vectors/caller-v1.json` | `reference/caller_ticket.py`, `reference/generate_caller_vectors.py` |

A change to a format raises its version. A document, its vectors and its reference module change together, and the reference tests pass before the change merges.

## Relation to spec 105

The contracts in [spec 105](../../105-decentralized-personal-mesh/contracts/README.md) are history. Where a contract here covers the same ground, this folder wins. The 105 reference helpers for canonical JSON, hashes, signatures and test keys are still imported by the references here, and every other 105 contract applies until a document here replaces it.

Existing roster v2 stores keep working unchanged. Nothing in this folder adds an event type or a body field that the deployed kernel cannot read.

## Running the checks

From the repository root, with the packages in `specs/105-decentralized-personal-mesh/contracts/reference/requirements.txt` installed.

```
python specs/106-sign-in-free-clients/contracts/reference/generate_caller_vectors.py --check
python specs/106-sign-in-free-clients/contracts/reference/generate_local_vectors.py --check
python -m pytest specs/106-sign-in-free-clients/contracts/reference/tests -q
ruff check specs/106-sign-in-free-clients/contracts
```
