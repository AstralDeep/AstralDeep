# Tasks for spec 105

**Date:** 2026-11-12. **Status:** in progress on branch `feat/105-personal-mesh` in AstralDeep,
AstralProjection, AstralPlane, AstralPrimitives and LETS, and on `main` in the new local repository
`AstralKernel`. Nothing is pushed. Read [spec.md](spec.md) and [plan.md](plan.md) first. This file
fixes the choices every lane shares, so lanes can work in parallel.

## Owner answers, 2026-11-12

| Spec question | Answer |
|---|---|
| 1. Kernel in Rust (C2) | Yes. rustup is installed through Homebrew with the Apple targets. |
| 7. Interim relay | The transport's public relays may be used. |
| Live tests | The owner signs in to Keycloak when asked. Live chat turns use the model endpoint and key already in AstralDeep's `.env`, loaded by a local script. The key is never typed into a form, printed, or committed. |

The other questions in spec section 21 take the values the spec proposes until the owner says
otherwise: a phone runs turns alone (C1), a mesh needs one Keycloak attestation to be created, the
allowance table in section 8.3, a new device reads earlier history by default, speech runs on the
device (C8), the kernel is called `AstralKernel` for now, and multi-owner hosting returns with the
institution profile (C10). Who signs PHI approvals is open, so the roster accepts PHI authorities
but no build ships one.

## Repositories and paths

| Repository | Path | Branch |
|---|---|---|
| AstralDeep | `/Users/sam/Desktop/Work/AstralDeep` | `feat/105-personal-mesh` |
| AstralKernel (new) | `/Users/sam/Desktop/Work/AstralKernel` | `main`, lanes on their own branches |
| AstralProjection | `/Users/sam/Desktop/Work/AstralProjection` (not the stale submodule worktree) | `feat/105-personal-mesh` |
| AstralPlane | `/Users/sam/Desktop/Work/AstralPlane` | `feat/105-personal-mesh` |
| AstralPrimitives | `/Users/sam/Desktop/Work/AstralPrimitives` | `feat/105-personal-mesh` |
| LETS | `/Users/sam/Desktop/Work/LETS` | `feat/105-personal-mesh` |

Rust: put `/opt/homebrew/opt/rustup/bin` first on `PATH`. Homebrew's own `rustc` has no iOS
standard library.

## Wire formats

These are fixed. A lane that finds one unworkable stops and reports, and does not change it alone.

- **Canonical JSON** is LETS-CJ/1: sorted keys, no whitespace, UTF-8, no floats, integers within
  signed 64 bits, bytes as unpadded base64url, duplicate keys refused.
- **Hashes** are SHA-256, written `sha256:<lowercase hex>`.
- **Domain separation.** Every signature covers `<context>\n` followed by the canonical JSON of the
  object without its `sig` field. The contexts are `astral.event/v1`, `astral.roster/v1`,
  `astral.checkpoint/v1`, `astral.consent/v1`, `astral.policy/v1`, `astral.attestation/v1` and
  `astral.pairing/v1`.
- **Keys.** Identity and presence keys are P-256 ECDSA with SHA-256, signatures as raw 64-byte `r||s`,
  public keys as 65-byte uncompressed points. Operations keys are Ed25519. Each device also has an
  X25519 wrap key. The identity key certifies the operations and wrap keys in the device's roster
  entry.
- **Ids.** A feed id is the device's operations public key. An event id is the hash of its signed
  header. The mesh id is the hash of the first roster entry. Chat ids are the event id of the chat's
  creating event.
- **Clock.** A hybrid logical clock `{ms, n}`. A peer refuses an event whose `ms` is more than five
  minutes ahead of its own clock.
- **Body encryption.** XChaCha20-Poly1305, a random 24-byte nonce, and as associated data the canonical
  JSON of `{feed, seq, scope, key}`. One content key per scope (a chat, or `mesh`).
- **Key wrapping** (`astral.wrap/v1`): an ephemeral X25519 key, ECDH with the recipient's wrap key,
  HKDF-SHA256 with info `astral.wrap/v1`, then XChaCha20-Poly1305.
- **On the wire** an event travels as `{feed, seq, header_ct, body_ct}`. The header is sealed under
  the personal group's current epoch key, so a relay or mailbox sees only feed, sequence and sizes.
- **Recovery key**: 32 random bytes shown once as 24 words from a fixed word list. HKDF-SHA256 derives
  an Ed25519 signing key (info `astral.recovery.sign/v1`) and an X25519 key (info
  `astral.recovery.wrap/v1`).
- **Tickets.** The capability part is a Biscuit token rooted at the device's operations key, with one
  attenuation block per hop (device, turn, agent, call). The last block pins the tool name and the
  argument digest. The receipt is a LETS receipt. Consent is a presence-key signature over the
  action digest. Labels ride in the capability as facts.
- **Transport** is iroh with ALPN `astral/sync/1`, local-network discovery, and the default public
  relays. A pairing payload carries the inviting device's iroh address.

## Kernel layout

One Cargo workspace in `AstralKernel`, one crate per area so each lane owns one crate. Every crate
depends on `astral-core` and on nothing else in the workspace except where the table says so. A
crate that needs another area defines a small trait, and the facade wires the real one in.

| Crate | Holds |
|---|---|
| `astral-core` | Canonical JSON, hashing, key and signature types, the event header, ids, the hybrid clock. No I/O. |
| `astral-log` | The SQLite store, feeds and chain checks, the fold (chats, branches, surfaces, settings), and the sync protocol state machine, independent of transport. |
| `astral-mesh` | Roster entries and merge, identity attestation checks, key groups and epochs, wrapping, the recovery key and backup file, pairing. |
| `astral-net` | iroh transport carrying the sync protocol, local discovery, relays, mailbox client. Depends on `astral-log` for sync messages. |
| `astral-authz` | Labels and flow rules, the gate stack, tickets (biscuit-auth), consent checks, the LETS holder and verifier, period budgets and allowances. |
| `astral-engine` | The turn state machine and step journal, the provider layer (clocks, retries, fallback chain), the argument pipeline, the quarantined reader, the MCP client, runs and learning. |
| `astral-ui` | Device adaptation of surfaces, surface documents and patches, action capabilities, value bindings, the decoding grammar from the strict schema, A2UI import and export. |
| `astral-kernel` | The facade that composes the crates into one `Kernel`. |
| `astral-kernel-ffi` | The UniFFI interface over the facade. Swift and Python bindings are generated from it. |
| `astral-mailbox` | A small server binary: blind relay helper, mailbox and the lost-device removal notice. |
| `astral-kernel-cli` | A command-line peer used by tests, by the node installer, and to load a model credential from a file. |

Build outputs: `scripts/build-apple.sh` makes `AstralKernelFFI.xcframework` (macOS arm64 and x86_64,
iOS arm64, iOS simulator arm64) and the generated Swift file. `scripts/build-python.sh` builds a
Linux wheel inside Docker for the node.

**The FFI surface** stays small and is owned by the facade lane. Swift lanes code against these
shapes before the facade exists. Names are UniFFI names. Blocking calls run off the main thread.

Callback interfaces the host implements:

| Interface | Methods |
|---|---|
| `HostSigner` | `identity_public_key() -> bytes` (65-byte P-256 point), `identity_sign(message) -> bytes` (64-byte `r||s` over SHA-256 of message), `presence_public_key() -> bytes`, `presence_sign(prompt: ConsentPrompt, message) -> bytes` (the host draws the prompt from `ConsentPrompt`, asks for biometrics, then signs), `hardware_backed() -> bool` |
| `OnDeviceModel` | `available() -> bool`, `complete(request: OnDeviceRequest) -> OnDeviceResponse` (messages, an optional JSON schema for constrained output, max tokens) |
| `LocalTools` | `list() -> [LocalToolSpec]`, `call(name, arguments_json, ticket) -> string` |
| `KernelListener` | `on_change(change: KernelChange)`: chat changed, sync state changed, consent requested, roster changed, turn progress, allowance changed |

The `Kernel` object:

| Area | Methods |
|---|---|
| Lifecycle | `open(config, signer, model, tools, listener)`, `start_network()`, `stop_network()`, `version()` |
| Mesh | `mesh_status()`, `begin_mesh_creation() -> AttestationRequest` (the OIDC nonce to send), `complete_mesh_creation(id_token) -> RecoveryKit`, `create_pairing_offer() -> PairingOffer` (short code and payload, shown on the new device), `approve_pairing(payload)` (on an existing device, with consent), `roster()`, `remove_device(member_id)`, `raise_stop()`, `reattest(id_token)`, `export_backup(path)` |
| Chats | `list_chats()`, `chat(chat_id) -> ChatView` (messages, branches, surfaces, labels, status), `create_chat(title)`, `send_message(chat_id, text, continues)`, `fork(chat_id, event_id)`, `act(surface_id, component_id, action)` |
| Turns | `run_turn(chat_id)`, `steer(chat_id, text)`, `interrupt(chat_id)`, `pending_consents()`, `resolve_consent(request_id, approve)` |
| Models | `set_model_provider(config)` (stored as a sealed secret item), `model_providers()` (never returns secrets) |
| Sync and budget | `sync_state()`, `allowance()` |
| Runs and learning | `runs()`, `hand_off_run(run_id, member_id)`, `take_over_run(run_id)`, `learning_proposals()`, `review_proposal(id, adopt)`, `undo_adoption(id)` |

## Lanes

Each lane names the files it owns. Two lanes never edit the same file. A lane commits to its own
branch and reports what is implemented, what tests pass, and what is not done.

### Wave 1, started 2026-11-12

| Lane | Repository | Owns | Delivers |
|---|---|---|---|
| R. Reliability | Deep | the files named in [phase-r-plan.md](phase-r-plan.md) | Phase R, all nine tasks. |
| C. Contracts | Deep | `specs/105-decentralized-personal-mesh/contracts/` | Phase 0 contract documents, a Python reference in `contracts/reference/`, and vectors in `contracts/vectors/` for every format above. Spec 100's availability contract amendment. |
| K0. Kernel spike and FFI | AstralKernel | workspace, `astral-kernel-ffi`, build scripts, the Swift wrapper package in Projection `apple-clients/AstralKernel/` | The phase 0 build spike, kept rather than thrown away: one event signed and verified, SQLite opened, an iroh endpoint opened, called from Swift on macOS and the iOS simulator and from Python on Linux. Build times recorded. |
| L. LETS | LETS | new modules and docs | Frozen receipt and lease formats with vectors, period budgets derived from a signed policy and the roster, the device profile. |
| P. Primitives | AstralPrimitives | the package | The committed versioned wire schema and strict generation profile, then the phase 6 fields: value bindings, action capabilities, spoken and compact forms, node provenance, handle-only links and images, the A2UI catalog. |
| N1. Node | Deep | `backend/orchestrator/auth.py`, `delegation.py`, `mesh_*.py`, `session_store.py`, `shared/a2a_security.py`, the delegation and token-validation regions of `orchestrator.py`, new `node_*.py` | Phase 1 backend: single-owner node profile, issuer and audience checks on all three verification paths, `(issuer, subject)` owner ids, Ed25519 local delegation, node descriptor and pairing endpoint, stop wiring. |
| PL. Plane | AstralPlane | new schema and repository modules | Event tables with hash ids, chat views derived from events, learning ledger and goal runs, with the guarded migration and revision bump. |

### Wave 2, after C and K0

Kernel modules in parallel lanes against the vectors: log, store, fold and sync. Roster, keys and
pairing. Transport. Labels, gates, tickets and LETS. Turn engine, providers, arguments and MCP. UI
adaptation and surface documents. Apple client integration starts at the same time against the FFI
surface: the mesh runtime, pairing and settings, chats from the local log, consent, the on-device
model, then voice.

### Wave 3

The node joins its mesh through the Python binding, stores events in Plane, serves as relay and
mailbox, verifies tickets and runs its own gates. Runs and learning. Signed agent cards and
hash-pinned tool definitions. The institution profile. The Keycloak move runbook. Then live tests on
macOS and the iOS simulator.

## Rules for every lane

- Follow `AGENTS.md`. Each source file opens with a header of at most three sentences. No other
  comments except a one-line reason where it is essential. No spec or task numbers in source.
- Never run `make up`, `docker compose`, or anything that recreates the shared `astraldeep` container
  or touches the `astraldeep-postgres` database. Run backend tests in a disposable container: a
  throwaway `postgres:17-alpine` on its own network, then `docker run --rm` of `astraldeep:latest` with
  the lane's worktree mounted at `/app` and no `.env`.
- Never print, log or commit a secret. Never type a credential into a form.
- Do not push, open pull requests, or change GitHub.
- Report implemented, test-passing and live-verified separately.
