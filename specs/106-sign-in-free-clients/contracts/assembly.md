# Activation manifest

This contract fixes the document an assembly uses to state what it loads: the contracts it implements, one adapter per port, the capabilities it offers, the controls it requires, and what each door gives each principal class. Format version `astral.assembly/v0`.

Reference code: `reference/assembly.py`. Generator: `reference/generate_assembly_vectors.py`. Vectors: `vectors/assembly-v0.json`. The node's own manifest is `config/astral-assembly.json`, and the node's validator is `backend/orchestrator/assembly_manifest.py`.

## 1. What it is for

Composition was spread over submodule pins, deployment files and feature flags. None of them answers "what does this node load, against which contracts, and what does a caller see".

The manifest is that answer in one reviewable file. The node refuses to start when the file is missing, malformed or untrue.

`config/astral-composition.json` keeps pinning component revisions. Deployment files keep per-instance values such as the storage backend. The manifest holds neither.

## 2. The document

A JSON object with exactly these six members. A member that is not listed is refused. A duplicate key is refused.

| Member | Value |
|---|---|
| `format` | `astral.assembly/v0` |
| `assembly` | The assembly's name |
| `ports` | Section 3 |
| `capabilities` | Section 4 |
| `doors` | Section 5 |
| `exceptions` | Section 6 |

A name is 1 to 64 characters of `a-z`, `0-9`, `.`, `/` and `-`, starting with a letter or digit.

## 3. Ports

`ports` has exactly five members: `identity`, `authority`, `persistence`, `tool-provider` and `ui-adaptation`.

Each port has exactly `adapters` and `contracts`.

`adapters` is a list of one or more distinct names. Every port takes exactly one adapter, except `tool-provider`, which may list several. Two adapters on any other port is ambiguous and is refused.

`contracts` is a list, which may be empty. Each entry has exactly `format`, `path` and `sha256`. `path` is a repository-relative POSIX path with no `..` part. `sha256` is 64 lowercase hex digits over the bytes of that file.

The digest is checked wherever the repository is present, which is CI. A packed node carries no contract documents, so boot does not check digests.

## 4. Capabilities

`capabilities` has exactly `offered` and `required_controls`. Both are sorted lists of distinct names.

`offered` equals the capability names the node can put in its descriptor: the fixed list and the ones a joined mesh adds. The descriptor stays the runtime truth and may show fewer.

`required_controls` names controls the assembly must run with. The known controls are `consent-gate`, `egress-allowlist`, `emergency-stop`, `phi-gate` and `ticket-verification`.

## 5. Doors

`doors` is a non-empty object. A key is a door, such as `mesh` or `/mcp`.

Each door has exactly these four members.

| Member | Value |
|---|---|
| `principal` | `owner-device`, `caller-ticket` or `outside-service` |
| `turns` | `true` when the door can start a turn for that principal |
| `notification_types` | The notification types the door sends, a list of distinct names |
| `consent` | `full`, `own-redacted` or `none` |

The consent value follows [caller-events.md](caller-events.md) section 5.

| Principal | `turns` | Allowed `consent` |
|---|---|---|
| `owner-device` | Either | `full`, `none` |
| `caller-ticket` | `true` | `own-redacted`, `none` |
| `caller-ticket` | `false` | `none` |
| `outside-service` | Either | `none` |

A door that gives a caller more than its row allows is refused. This is the rule that keeps a caller from seeing another principal's consent.

## 6. Exceptions

`exceptions` is a list of known gaps the assembly ships with. Each entry has exactly `id`, `date`, `review` and `summary`, all non-empty text. `date` is `YYYY-MM-DD`.

An empty list means the assembly claims no gap.

## 7. Validation order and codes

The document is checked in this order and the first failure wins.

1. The document is an object. `format` is `astral.assembly/v0`.
2. The members are exactly the six of section 2.
3. `assembly` is a name.
4. `ports`, each port in the order of section 3, each contract in list order.
5. `capabilities`.
6. `doors`, in document order.
7. `exceptions`, in list order.

The runtime check comes after the document is valid. It compares `offered` with the node's capability names, then each `caller-ticket` door's `notification_types` with the types the door serves.

| Code | Meaning |
|---|---|
| `assembly.missing` | The file cannot be read |
| `assembly.malformed` | Not JSON, a duplicate key, a value of the wrong type, a bad name, an unsorted or repeated list, or a port or door with a member missing |
| `assembly.unknown_format` | `format` is absent or is another value |
| `assembly.unknown_section` | A top-level member that section 2 does not list |
| `assembly.section_missing` | A member of section 2 is absent |
| `assembly.unknown_port` | A port that section 3 does not list |
| `assembly.port_missing` | A port of section 3 is absent |
| `assembly.unknown_key` | A port, the capabilities or a door has a member that is not listed |
| `assembly.ambiguous` | More than one adapter on a port that takes one |
| `assembly.contract_malformed` | A contract entry of the wrong shape |
| `assembly.unknown_control` | A required control that section 4 does not list |
| `assembly.unknown_principal` | A door's principal is not a class of section 5 |
| `assembly.consent_visibility` | A door's `consent` is not allowed for its principal and `turns` |
| `assembly.exception_malformed` | An exception entry of the wrong shape |
| `assembly.capability_mismatch` | `offered` differs from the node's capability names |
| `assembly.notification_mismatch` | A caller door's `notification_types` differ from what the door serves |
| `assembly.contract_missing` | A pinned contract file is absent |
| `assembly.contract_digest` | A pinned contract file has another digest |

## 8. Who checks it

| Where | Checks |
|---|---|
| Node boot | Sections 2 to 6 and the runtime check. Any code stops the node with exit 78 and `node.assembly_refused` |
| CI, the contract reference tests | The same, plus the contract digests, plus the vectors against both the reference and the node's validator |
| A client | Its required capabilities against the descriptor before it activates. It names the missing capability when it refuses |

## 9. Versioning

`v0` while the port list and the control list are still settling. A new port, a new control or a new door member raises the version. Feature flags move into the manifest only when a flag selects between qualified adapters.
