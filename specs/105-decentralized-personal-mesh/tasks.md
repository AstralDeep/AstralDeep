# Tasks for spec 105

**Date:** 2026-11-13. **Status:** resumed by the owner after the third stop; implementation and qualification remain in progress. No final runtime SHA has been selected and no complete final same-SHA gate set exists. Current work uses Deep `feat/105-integration`, Kernel `k/integration`, Projection `feat/105-mesh-final` and Vessel `feat/105-vessel-final`. The third-stop anchors and subsequent evidence are in [local-integration-handoff.md](local-integration-handoff.md). Prior product branches were pushed except the Kernel; new resume commits are local. No default-branch merge, deployment or release is claimed. Read [spec.md](spec.md) and [plan.md](plan.md) for intent.

## Current qualification queue

This is a local checkpoint, not a completion checklist for an already qualified release. Exact revisions, evidence paths, recorded failures and pending pin updates are in [local-integration-handoff.md](local-integration-handoff.md); criterion counts and the bounded 3.11 assessment are in [qualification.md](qualification.md).

- [x] Qualify persisted compaction-note provenance through the production `LogRunStore`, close/reopen, later model prompt and denied outside dispatch: Kernel test-only `e2db832b`, 17 runs-service tests passed. Closes 3.9 for persisted notes only.
- [x] Retain actual Mac pack proof at Deep `13b96dbb`/Kernel `d02e4ea`: 47 packaging tests, Swift real install/start/health/stop (41.8 s), and an owner-authorized 74-tool WebSocket turn executing the bundled chart tool with credential denials checked.
- [x] Repair stream-test completion synchronization and the reviewed local-wheel voice guards without production-code changes or weaker gates; retain red/green proof and the original failing full attempts.
- [x] Retain Projection `8abce633` Python/NodePack changed-line result 616/628 (98.09%) and 230 focused passes/five skips/one active Apple-provenance deselection. The deselected guard still requires final qualification.
- [x] Verify all 13 actual installed model files (1,419,703,438 bytes) at immutable revision `65619d48c5e4bc1552db24e07bc4a2ff2aa3cf49`; retain offline-package/parity and optimized network-denied diagnostic results separately from live voice.
- [ ] Finish standalone remote MCP through Kernel/FFI/native UI, with durable registry, sealed credentials, bounded SSRF controls, revision-bound authority and denial tests; qualify and adopt exact source/artifact pins together.
- [ ] Regenerate bindings and test the optimized Mac app and connected iPhone 13 Pro: bounded final ASR completion, speech buffering/Stop Voice, the A15 text fallback, real offline voice and interruption measurements. Corrected provisioning does not establish installation or device qualification; MCP bindings currently block installation. Hardware work is requested and active, with no owner deferral.
- [ ] Finish normal owner-authorized mesh push registration/access custody, then demonstrate physical background APNs wake and sync. Warm-handler `7339507` has 96 focused iOS passes; actual registration/delivery remains unqualified.
- [ ] Exercise one stored native surface and an imported A2UI surface on both clients; finish native node catalog/provider custody and regenerate the pack after final MCP adoption.
- [ ] Add the bounded composed-host conformance consumers for existing shared vectors (3.11); no production changes were made by the read-only gap assessment.
- [ ] Freeze one clean final Deep SHA after all code/pins/docs are integrated. Run `tests_2`, `tests_1`, `modules` and `persistent_agents` with the corresponding rebuilt image and correct nonroot scratch posture. Revisit the unexplained full-group work-runtime model-call-count failure; focused passes do not explain it.
- [ ] Merge only passing same-SHA coverage; run strict 90% changed-line gates against both the resume base `ced61ce087ca8b1a1d1e4339e521ef2c8f59718a` and full-feature base `a3eb724aa15fcdcb2c590b859c5f5104c41a4daa`, with backend/tooling/voice-worker reports and no changed-code exclusions.
- [ ] Record the final native gates, remaining skips/risks and live demonstrations; refresh curated vault pages/index/log under the vault's rules before declaring a major checkpoint complete. No new push, Kernel publication, merge, deployment or release is authorized by this queue.

## Owner answers, 2026-11-12

| Spec question | Answer |
|---|---|
| Section 17 rule changes (2026-11-13) | The owner explicitly approved the shared Rust kernel and peer model, ROTE in the base, offline-verifiable tool tickets with Keycloak proving identity, and on-device voice. The repository guide now reflects those rules. |
| 1. Kernel in Rust (C2) | Yes. rustup is installed through Homebrew with the Apple targets. |
| 7. Interim relay | The transport's public relays may be used. |
| Live tests | The owner signs in to Keycloak when asked. Live chat turns use the model endpoint and key already in AstralDeep's `.env`, loaded by a local script. The key is never typed into a form, printed, or committed. |
| UX (2026-11-12) | After live testing the owner judged the mesh UI poor in look and behavior and asked for a full fix at the end: a HIG-grade pass over every mesh screen on the Mac and iPhone. |
| Kernel repository (2026-11-12) | The owner asked for the kernel repository to be created: `github.com/AstralDeep/AstralKernel`, public like the other components. It was created empty. Nothing is pushed to it yet. |
| A2UI (2026-11-12) | Use the latest stable protocol. The owner supplied the [A2UI 1.0 announcement](https://developers.googleblog.com/a2ui-10-build-delightful-fast-reliable-agentic-experiences/). Python and Rust adapters are being qualified against the stable 1.0 schemas at upstream `a2ui-project/a2ui@8bf644a37d680a227296b8c0849a7f4b328b8b73`, rather than relying on the earlier release-candidate check. |

| Layering | The base is the kernel, Deep, Plane, LETS and ROTE, shipped as AstralVessel. The UI layer is Projection and Primitives (spec section 16). The kernel stays its own library and Vessel bundles it. Vessel runs Deep's loop and keeps its sandbox and egress as the executor. ROTE moves out of Projection now. |
| Packaging and storage | Two installations: AstralVessel (the base and command-line tools) and the AstralDeep app (everything in Vessel plus the UI). No PostgreSQL on devices or personal installs: Plane gains a SQLite backend so Deep's Python node runs inside the Mac app and in Vessel. The iPhone runs the kernel only. PostgreSQL stays for institution nodes. |
| Voice | The apps stop using the server's hosted voice. LFM3-Audio-2B at 3 bits runs on the device with MLX, downloaded from the Hugging Face repo `Kentucky-Open-Science/LFM3-Audio-2B-MLX-3bit` (the owner uploaded it on 2026-11-12) when someone first taps the mic. No voice until it is installed, and no fallback to system speech. The Metal Toolchain is installed for building MLX. Users never need it because the app ships compiled shaders. |

The other questions in spec section 21 take the values the spec proposes until the owner says
otherwise: a phone runs turns alone (C1), a mesh needs one Keycloak attestation to be created, the
allowance table in section 8.3, a new device reads earlier history by default, speech runs on the
device (C8), the kernel is called `AstralKernel` for now, and multi-owner hosting returns with the
institution profile (C10). Who signs PHI approvals is open, so the roster accepts PHI authorities
but no build ships one.

## Checks against real systems

| Check (plan.md phase 0) | Result, 2026-11-12 |
|---|---|
| Can a device exchange an authorization code with Keycloak itself, so it holds the ID token that carries its bound nonce? | Yes. `astral-desktop` and `astral-mobile` are public clients: a token request with a bogus code returns `invalid_grant`, not `invalid_client`. |
| Does the realm sign ID tokens with an algorithm the kernel can verify offline? | The realm advertises RS256, ES256 and EdDSA among others. |

## Repositories and paths

Resolve active lane checkouts with `git worktree list`; the primary checkout can contain unrelated owner work and need not be the integration checkout. Repository-relative composition pins remain authoritative for adopted sources. The current inspection anchors are:

| Repository | Integration lane / source | Adoption state |
|---|---|---|
| AstralDeep | `feat/105-integration`, named `final` worktree; inspected checkpoint `13b96dbb` | Final integration and same-SHA qualification pending. |
| AstralKernel | `k/integration`; adopted source `components/AstralKernel` at `d02e4ea` | Notes test `e2db832b` is local; final MCP successor/artifacts pending. No Kernel push. |
| AstralProjection | `feat/105-mesh-final`; qualified checkpoint `8abce633` | Deep checkpoint still pins `components/AstralProjection` at `5abd809`; later Apple/MCP revision must be qualified before adoption. |
| AstralPlane | `components/AstralPlane`, `f8f2a637` | SQLite/PostgreSQL component adopted. |
| AstralPrimitives | `components/AstralPrimitives`, `f4314c8` (0.13.0) | Adopted local wheel; separate release train. |
| LETS | `components/LETS`, `13213061` (1.1.2) | Adopted node component; Windows' reviewed client closure is separate. |
| AstralVessel | `feat/105-vessel-final`, `76bb18a8` checkpoint | Final distribution qualification remains separate. |

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
- **Links in generated surfaces.** The strict validator rejects any link, image, autolink, reference
  definition or raw HTML in model-written text, using a markdown parser, not patterns. Renderers also
  show that text with link and image parsing off. A link or image appears only through a handle the
  host resolves. Both layers are required, because a validator alone has always had gaps.

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
| Mesh | `mesh_status()`, `begin_mesh_creation() -> AttestationRequest` (the OIDC nonce to send), `complete_mesh_creation(id_token) -> RecoveryKit`, `create_pairing_offer() -> PairingOffer` (short code and payload, shown on the new device), `preview_pairing(payload) -> PairingPreview` (on an existing device before approval: comparison code, the new device's label and kind, expiry), `approve_pairing(payload)` (on an existing device, with consent), `roster()`, `remove_device(member_id)`, `raise_stop()`, `reattest(id_token)`, `export_backup(path)`, `recover_with_words(words, backup_path?) -> RecoveryResult` (enrolls a fresh device from the recovery words and a backup file or a retained roster), `restore_marker()` and `mailbox_state()` |
| Chats | `list_chats()`, `chat(chat_id) -> ChatView` (messages, branches, surfaces, labels, status), `create_chat(title)`, `send_message(chat_id, text, continues)`, `fork(chat_id, event_id)`, `record_spoken(chat_id, kind, text)` (logs the assistant text a device actually played as a `chat.spoken` event), `act(surface_id, component_id, action)` |
| Turns | `run_turn(chat_id)`, `steer(chat_id, text)`, `interrupt(chat_id)`, `pending_consents()`, `resolve_consent(request_id, approve)` |
| Models | `set_model_provider(config)` (stored as a sealed secret item), `model_providers()` (never returns secrets) |
| Sync and budget | `sync_state()`, `allowance()` |
| Runs and learning | `runs()`, `hand_off_run(run_id, member_id)`, `take_over_run(run_id)`, `learning_proposals()`, `review_proposal(id, adopt)`, `undo_adoption(id)` |

## Lanes

Local integration and the remaining runtime work are tracked in
[runtime-composition-plan.md](runtime-composition-plan.md). A merged lane is not a completed phase:
the facade still has to connect the production log, custody, authorization and engine before a
client can run a mesh conversation.

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
| A1. Apple identity | Projection, branch `feat/105-apple-identity` | `apple-clients/AstralApp/AstralApp/Mesh/`, Info.plist and entitlements | Secure Enclave identity and presence keys, the restore marker, the host signer, the host-drawn consent sheet, enrollment sign-in with a bound nonce, local network permission. |
| A2. Apple on-device | Projection, branch `feat/105-apple-voice` | `apple-clients/AstralApp/AstralApp/OnDevice/` | The Foundation Models adapter, the on-device voice pipeline, barge-in as a clock-injected state machine, the fast path, Mac push-to-talk. |
| N6. Protocols | Deep, branch `feat/105-protocols` | A2A registration, remote MCP approval, publication | Signed agent cards required at registration, hash-pinned MCP tool definitions, selective publication per node. |

### Wave 2, after C and K0

Kernel modules in parallel lanes against the vectors: log, store, fold and sync. Roster, keys and
pairing. Transport. Labels, gates, tickets and LETS. Turn engine, providers, arguments and MCP. UI
adaptation and surface documents. Apple client integration starts at the same time against the FFI
surface: the mesh runtime, pairing and settings, chats from the local log, consent, the on-device
model, then voice.

### Layering lanes, from 2026-11-12

| Lane | Repository | Owns | Delivers |
|---|---|---|---|
| RM. ROTE move | AstralKernel `python/rote/`, Projection, Deep | ROTE's Python package and its imports | ROTE leaves Projection for the kernel repository as a Python package with the same module name, and Deep and Projection depend on it. |
| A3. LFM3 voice | Projection, branch `feat/105-apple-lfm3` | `apple-clients/AstralVoiceModel/`, `OnDevice/` | LFM3-Audio on MLX in Swift with parity tests, the model store and download prompt, the mic flow, and the server voice path retired from the Apple app. |
| PLS. Plane on SQLite | AstralPlane | a SQLite backend | After lane PL lands: a dialect seam, every repository ported, a SQLite migration registry starting at the current revision, one contract suite run against both backends. |
| MP. Mac node pack | Projection, Deep | the Mac app's embedded Python node | After PLS: Deep's node with a bundled Python runtime and Plane on SQLite, run by the Mac app as a supervised background process, trimmed of server-only parts. |
| V. Vessel | AstralVessel | the distribution | Wave 3: `astralvessel up` installs the base as a node, runs Deep's loop, keeps its sandbox and egress as the ticket-checking executor, and runs its conformance suite against the node. |

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
