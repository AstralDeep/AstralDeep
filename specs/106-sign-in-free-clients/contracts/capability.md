# Capability `astral.capability/v1`

A capability is a signed, versioned package that adds tools (and, for kernel-kind, a pinned
runtime layer) to a personal node's sandboxes without touching any trusted process. An owner
installs it on the node; agents reference it; the executor confines its scripts exactly like
the builtin tools. This document fixes the manifest, the registry index, the verification
order and the codes.

Reference code: `reference/capability.py`. Vectors: `vectors/capability-v1.json`.
The distribution home is the `astral-capabilities` registry repository, whose `index.json`
is the catalog.

## 1. The manifest

A capability package carries one `capability.toml` plus its tool scripts. The manifest is
TOML with exactly these tables; unknown tables or keys are `capability.malformed`.

| Table | Fields |
|---|---|
| `[capability]` | `id` (reverse-DNS, `[a-z0-9][a-z0-9.-]{2,63}`, unique in the registry), `version` (semver `MAJOR.MINOR.PATCH`), `kind` (`tool` or `kernel`), `description` (1 to 200 characters) |
| `[permissions]` | `scopes` (1 to 8 Astral scopes, the colon form `tools:read` … `tools:execute`), `output_files` (0 to 16 file extensions without the dot), `network` (`none` or `egress-proxy`), `phi` (`none` or `reviewed`) |
| `[limits]` | `tool_timeout_seconds` (positive integer; the host clamps it to its own maximum) |
| `[[tools]]` | Repeated 1 to 32 times: `name` (tool name per the tools contract), `script` (package-relative path, `tools/` prefix), `scope` (one of the manifest's `scopes`) |
| `[runtime]` | `kind = "kernel"` only: `python` (major.minor), `wheels` (1 to 64 pinned `name==version` specifiers) |

Rules:

- `kind = "tool"` requires no `[runtime]` table; its scripts run on an interpreter the host
  image already provides. `kind = "kernel"` requires `[runtime]`; its wheels materialize as a
  sandbox image layer attributed to this capability.
- Every `[[tools]].scope` must appear in `[permissions].scopes`, or the manifest is
  `capability.malformed`.
- `network = "egress-proxy"` requires the host's egress posture to allow it; the sandbox
  still has no direct network.
- `phi = "reviewed"` capabilities are refused on PHI-excluded hosts with
  `capability.incompatible`.

## 2. The registry index

The registry repository's `index.json` lists every published version:

| Field | Meaning |
|---|---|
| `format` | `astral.capability-index/v1` |
| `entries` | One per published version: `id`, `version`, `kind`, `tools` (the names), `manifest_sha256` (sha256 of the exact `capability.toml` bytes), `package_sha256` (sha256 of the package tarball), `signer` (the signing identity, or `null` while the signing identity is undecided), `published` (ISO date) |

The index is append-only in practice: a published version is never edited or removed except
by a revocation entry (`revoked: true` on the entry, with a date and reason). Installers
refuse revoked entries with `capability.revoked`.

## 3. Verification order

An installer verifies, in this order, failing at the first failure:

1. `capability.malformed` — the manifest does not parse or violates section 1.
2. `capability.digest` — the fetched bytes' sha256 does not match the index entry.
3. `capability.signer` — the signature's identity is absent from the node's `allow-signers`
   policy, or the signature does not verify. While the signing identity is undecided, an
   installer without an explicit `allow-signers` entry for the registry refuses every install
   with `capability.signer`. Until the signature artifact format lands with that decision,
   the recorded signer is an unverified claim and the policy entry is a conscious,
   node-local trust statement, not cryptography.
4. `capability.incompatible` — the host cannot serve the capability. Compatibility reads the
   fetched manifest, not the index entry: a kernel's `[runtime].python` must equal the
   host interpreter's major.minor, and a `phi = "reviewed"` capability is refused on
   PHI-excluded hosts with `capability.incompatible`.
5. `capability.revoked` — the index marks the entry revoked.

## 4. Resolution

An agent's manifest may reference capabilities: `uses = ["astral.cad-kernel@^1.0"]`. The
constraint is npm-style: `^MAJOR.MINOR` admits the same major at or above that minor;
`@1.2.3` pins exactly. Resolution runs against the host's installed ledger and fails closed:
a reference with no installed satisfying version refuses that agent's sandbox build with
`capability.missing`, and a reference with several satisfying versions resolves to the
highest one. The builtin tools are the virtual capability `astral.core/v1`: always present,
not installable, not removable, never listed in a registry.

## 5. Codes

`capability.malformed`, `capability.digest`, `capability.signer`, `capability.incompatible`,
`capability.revoked`, `capability.missing`. Hosts may add dotted refinements; clients treat
unknown `capability.*` codes as opaque failures showing the reason.

## 6. Versioning

Additions to the manifest or index (new optional fields) are compatible; removals, renames
and semantic changes bump the format suffix. The manifest, its reference and its vectors
change together, and the reference tests pass before the change merges.
