# Runtime composition and local qualification

**Date:** 2026-11-12. **Status:** stopped at the owner's request; local branches only. Resume from
[local-integration-handoff.md](local-integration-handoff.md). This plan connects
the separately implemented lanes to the phase exit criteria in [plan.md](plan.md). It does not mark
any phase live-verified or authorize product pushes, releases, model uploads or shared-stack changes.

## Current evidence

- Deep locally merged reliability, node and protocol lanes. Subsequent repairs cover token audiences,
  authorization for global trust writes, live MCP definition-pin changes and canonical owner fixtures.
  The 52 changed backend modules passed 1,366 tests; changed executable Python coverage was 96.17%.
  The current complete backend run and exact baseline-failure inventory belong in the local handoff.
- Kernel locally merged core, log, mesh, authorization, engine, UI, ROTE and transport. The integrated
  workspace passed 657 tests after branch-context, canonical-binding, scoped-log query and durable
  engine-sink repairs. Formatting and strict clippy passed. All five Apple library targets and the
  Linux Python wheel built; the Swift
  facade still exposes the build spike rather than the complete runtime.
- Primitives uses stable A2UI 1.0 schemas pinned to upstream
  `8bf644a37d680a227296b8c0849a7f4b328b8b73`, frozen with their license for deterministic tests.
  The complete Primitives suite passed 2,477 tests. The package is unreleased.
- Projection locally integrated identity, on-device voice and ROTE lanes. Voice requires an explicit
  `VoiceKernelLink`; there is no production link yet. Plane's final shared qualification is recorded
  in the handoff; Deep's primary composition pin has not changed. The tested Apple custody branch is
  also separate and unmerged.
- Exact signing-input consent binding passed 29 focused tests on each of macOS and iOS. The shared
  canonical-JSON wrapper passed six tests on each platform. Required unavailable biometrics refuse
  before policy evaluation; display-only prompts cannot authorize signing. Roster prompt factories
  currently support add/remove only and refuse other kinds.

These are local implementation and test observations, not protected CI or live acceptance evidence.
The existing changed component pointers in the Deep checkout must be preserved until the exact
replacement revisions are qualified deliberately.

## 1. Durable signed chat, before model execution

1. Extend the host custody boundary with device-only Ed25519 operations and X25519 wrap keys. Reuse
   the host's existing P-256 identity and presence keys. Do not place clear private keys in log metadata
   or generate replacement identities during ordinary reopen.
2. Compose a verified roster and epoch/content-key provider with `astral_log::Log`. Persist public
   roster material and sealed key material, rebuild verified state on reopen, and notify the log when
   keys change. Coordinate the existing host restore-pending marker with successful new enrollment
   and feed activation; do not clear it merely because the app launches.
3. Add a log-owned authenticated, decrypted scope query in causal order. A facade adapter must use
   that public API rather than query the SQLite driver or decode unverified bodies itself.
   `Log::scope_events` is now available with explicit body states; the production engine adapter is
   being implemented separately.
4. Compose create-chat, owner-message, chat-read and reopen operations through the facade and FFI.
   Use the contracted `new` scope for chat creation, then bind the resulting event id as the chat id.
5. Connect transport to the enrolled operations identity and real log. The spike's separately persisted
   transport secret is not the production identity contract.

**Verification:** deterministic two-runtime tests cover encrypted append, reopen with the same keys,
sync and derived chat views, missing/wrong keys, removed members, stale/future events, forks and
restored-install refusal. Then exercise the same operations through Swift on macOS and iOS.

## 2. One authorized, bounded turn

1. Compose the new `LogEventSink` with the runtime, preserving verified labels and branch ancestry.
   Its real-store tests cover body availability, structural builder checks and blocking-pool failures;
   exhaustive journal grammar and proof/replay policy still belong in the engine. The branch-aware context builder must keep
   sibling branches and queued messages out of the selected turn.
2. Bridge `Gatekeeper`, consent and `ToolHost` with a durable authorization record bound to the exact
   action, arguments and current tool definition. Consent and receipts must survive replay with their
   proofs, not only opaque ids. Recheck stop, membership and policy immediately before each dispatch.
3. Resolve the contracted uncapped-read/receipt seam before dispatch is enabled: gate dimensions
   currently omit reads, while `verify_ticket` requires an action-bound LETS receipt. Any amendment
   must land in the contract, Python reference, LETS implementation, Rust verifier and vectors together.
   Never substitute an unrelated receipt, a fabricated charge or a silent null receipt.
4. Account for model-token allowance across attempts, fallbacks, regeneration and reader calls.
   Re-evaluate the actual provider/model immediately before every attempt, including after backoff.
   Derive PHI approval from verified authority, and route remote requests through deterministic egress
   and redirect controls.
5. Implement peer/node ticket delivery and receiver verification before its own gate stack. Keep
   explicitly approved third-party MCP OAuth separate. Preserve the confidentiality of inputs in
   result labels and recheck the approved tool-definition pin.

**Verification:** real composition tests with deterministic fake adapters cover success, denial at
every gate, mutation after approval, revocation during waits, exhaustion, retries without duplicate
charges, unknown effect outcomes, process reopen and receiver-side denial. Conformance vectors must
run against both the Python and Rust loops.

## 3. Apple chat and voice

1. Connect server-independent chat state and turn events to the native app using the production FFI.
   Host-drawn consent must bind the exact canonical action and unsigned signing input before any
   biometric prompt. A display-only prompt cannot authorize signing, and a biometric requirement
   must refuse unavailable biometrics.
2. Supply a real `VoiceKernelLink`. Preserve complete model output for audit while deriving future
   conversational context from text actually delivered. Completed sentences alone cannot prove an
   interrupted word prefix was played; add delivery/alignment evidence before making that claim.
3. Separate interruption of generation and cancel-safe tools from unsafe effects. Voice interruption
   must not cancel or replay an effect whose outcome is unknown.
4. Use the model store's size/hash verification and explicit download state. Release qualification
   requires the owner-published immutable model revision and matching manifest digests. Do not
   upload weights or sign in on the owner's behalf.

**Verification:** combined unsigned macOS/iOS builds and focused native tests, then live microphone,
playback, interruption, offline chat, consent/denial and two-device sync. Keep baseline native-test
failures demonstrated separately. Android/Windows retain the current thin-client posture until
their scoped runtime port lands.

## 4. Plane, packaging and phase acceptance

- Finish the SQLite port against every repository contract and representative existing data on
  Python 3.11. Keep genuine backend-specific catalog/locking checks explicit and replace them with
  meaningful SQLite equivalents. Verify PostgreSQL regressions, schema identity, recovery and
  changed-line coverage before changing Deep's composition pin.
- Deep currently calls `create_postgres_runtime` and binds PostgreSQL's registry digest. Add an
  authorized backend selection and an exact SQLite registry expectation through Plane's public
  facade before enabling a SQLite node. Updating the Plane library pin alone does not enable that
  host composition.
- Integrate the ROTE composition changes only with exact qualified Kernel/Projection/Plane revisions.
  Build the Linux Python binding from that Kernel revision, and exercise it in the node.
- Build the supervised Mac node pack and retire Vessel's independent loop through its own ticket
  conformance and sandbox/egress acceptance tests. Neither is implemented merely by merging crates.
- Qualify runs/learning, institution/PHI authority and shared services against their own phase criteria.
  Record automated and live results separately; protected CI, merge, deployment and release remain
  separate milestones.

The genuine owner/external inputs are interactive identity-provider sign-in, immutable published voice
model artifacts and institution attestation authority. Internal adapter work can continue without
those inputs and must not weaken the corresponding fail-closed boundaries.
