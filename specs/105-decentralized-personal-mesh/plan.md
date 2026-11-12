# Plan for spec 105

**Date:** 2026-11-11. **Status:** proposed. Nothing here has started. This is a program plan: phases,
the work in each repository, and how to tell a phase is finished. A task-level plan for each phase is
written when that phase is approved, not before.

Read [spec.md](spec.md) first. Section numbers below refer to it.

## Order

```
R  Reliability now ─────────────────────────────────────────────▶ (independent, starts first)

0  Contracts, vectors, build spike
        │
1  A node of your own            ← the sandbox stops being required here
        │
2  The log and sync              ← chats appear on every device here
        │
3  The pocket runtime            ← a phone works by itself here
        │
   ┌────┼──────────────┐
4  Voice    5  Runs and    6  Generative UI and
            learning          protocol surfaces
   └────┬──────────────┘
7  Shared services, institutions, web
```

- Phase R depends on nothing and helps the product as it is today.
- Phases 4, 5 and 6 can run side by side once phase 3 is done.
- The relay, mailbox and push gateway in phase 7 can be pulled forward the day a host exists.
- Client order inside every phase: macOS, then iOS, then Android and Windows. The watch follows the
  phone. Web is paused.

Each phase lists **exit criteria**. A criterion marked *(live)* can only be shown on real devices or
with a live model. It is demonstrated and recorded, and is not a CI gate. Every other criterion is an
automated test. A phase is finished when its automated criteria pass in CI and its live criteria have
been demonstrated on macOS and iOS. Android and Windows follow inside the phase without gating it, and
keep working as thin clients of a node until their port lands (spec call C9). "Specified",
"implemented", "test-passing" and "live-verified" are reported separately.

## Defects found during research

Research found four defects on `main` that are independent of this spec. They are recorded privately
until they are fixed, and this public document does not describe them.

---

## Phase R: reliability now

**Goal.** Stop losing turns to stalled models and malformed tool arguments, in the existing Python
loop.

**Depends on.** Nothing.

**Work (AstralDeep).**

- Replace the single 60-second timeout (`llm_config/client_factory.py:14,106-111`) with separate
  connect, first-token, idle and attempt deadlines, per model profile (section 10.2).
- Put a deadline on the stream pump's wait (`orchestrator.py:18300`).
- Add a per-turn fallback chain after failure. The router exists (`model_router.py`) and is never used
  for failover. In this phase the chain is set by environment variable. Configuring it in the product
  needs a Plane column and client UI.
- Do not re-run a request once content has streamed. Continue from the partial output.
- Apply one argument pipeline to every tool call on both dispatch paths (`orchestrator.py:19977` and
  `:20833`, which differ today): repair, validate against the tool's schema, regenerate once under a
  strict schema, then a precise tool error (section 10.3).
- Move the identical-call guard out of the goal harness (`goal_progress.py:28`) so it covers every
  turn, and count unparseable calls as repeats.
- Use a strict schema when regenerating arguments. Strict schemas on the first tool request wait for
  the native adapters (Deep #331, #326).
- Unparseable arguments are already kept out of replayable history (`_history_assistant_message`).
  Keep its test passing.

The task-level plan is [phase-r-plan.md](phase-r-plan.md).

**Tests.** A fake model provider that can stall before the first token, stall mid-stream, drop the
connection, return `429` with `retry-after`, and emit each known kind of malformed arguments. It is
deterministic and runs inside the CI time limit.

**Exit criteria.**

- A provider that never sends a first token surfaces to the user within the first-token deadline, not
  after three minutes.
- A stream that stalls mid-answer ends within the idle deadline and keeps the partial text.
- Each malformed-argument case from the incident history (non-JSON, raw control characters, a number
  as a string) is repaired or corrected in at most one extra model call.
- No identical failing call is dispatched more than twice in one turn.
- A primary model outage completes the turn on the fallback, with a visible notice.

---

## Phase 0: contracts, vectors and a build spike

**Goal.** Fix the formats everything else depends on, and find out whether one shared kernel is
buildable (spec call C2) before anything relies on it.

**Depends on.** Owner answers to spec section 21, questions 1 and 10.

**Work.**

| Repository | Work |
|---|---|
| AstralDeep | Write the language-neutral contracts under `specs/105-decentralized-personal-mesh/contracts/`: event header and body, feed and sync messages, roster entries, outside attestations (identity, PHI approval), key groups and key epochs, the recovery key, the budget policy, the ticket, the label set and flow rules, the gate order, UI surface documents. Each has test vectors. Amend spec 100's availability contract and its conformance test in the same change. Amend `AGENTS.md` (spec section 17). |
| LETS | Freeze the receipt and lease formats and publish signature and claim vectors (LETS #157). Write the design for period budgets derived from a signed policy and the roster, and for the device profile. |
| AstralPrimitives | Commit the generated wire schema as a versioned file. Add a strict profile for generation with no open-ended properties. |
| New kernel repository | A spike, thrown away afterwards: sign and verify one event, verify one LETS receipt, check every contract's vectors, open a SQLite store, open a transport endpoint, and call them from Swift (macOS, iOS), Kotlin (Android) and Python (Windows, Linux). Measure build time against the 30-minute CI limit. |

**Checks to run against real systems in this phase.**

- The deployed Keycloak version returns the request nonce in the ID token, and what a silent
  re-authentication needs (spec section 7.3). A refresh-token response may not carry the nonce.
- The transport's Kotlin binding builds and runs on Android. One report says the published artifact
  targets desktop only.
- Which parts of the transport the language bindings expose. The research found endpoints and streams
  only, so attachment transfer and local discovery are the kernel's to build.

**Exit criteria.**

- Every contract has vectors produced by a reference script in Python and verified by the spike in a
  second language.
- The spike builds for macOS, iOS, Android, Windows and Linux inside the CI limit, and each binding
  round-trips an event. If it does not, call C2 falls back to native implementations against the same
  vectors. The formats stay, but the transport and capability-token libraries (calls C5 and C6) have no
  Swift or Kotlin form outside a shared core and must be chosen again, and every later phase grows.
- LETS vectors verify in the Python reference and in the spike.
- The owner has signed off the rule changes.

---

## Phase 1: a node of your own

**Goal.** Any owner can run the orchestrator themselves, and no client needs
`sandbox.ai.uky.edu`. Clients are still thin in this phase. The node can be the owner's own Mac, a
home server or a VM.

**Depends on.** Phase 0 contracts for the roster and pairing.

**Work.**

| Repository | Work |
|---|---|
| AstralDeep | A single-owner node profile with its own startup posture (`session_store.py:81-143` assumes operator secrets). Enforce token audience and issuer (`auth.py:303-310`). Key owners on `(issuer, subject)`. Mint delegation locally with an asymmetric key in place of the per-dispatch Keycloak exchange (`orchestrator.py:22320`). The MCP channel already mints locally (`:22511-22558`). A node descriptor and a pairing endpoint, building on `mesh_enrollment.py`. The low-level node installer that AstralVessel calls (re-file of Deep #246, #293). Raise and persist the stop counter (Deep #245, #289). Takes ROTE from Projection through the kernel's Python package. |
| AstralVessel | Becomes the base's distribution (spec section 16). `astralvessel up` installs a personal node that runs Deep's turn loop and gates, with Plane and LETS. Vessel's own loop is retired. Its sandbox and egress become the node's executor. Its conformance suite runs against the node. |
| AstralKernel | Takes ROTE's Python package out of Projection, unchanged in behavior, so Deep and Projection both depend on the base for it. |
| AstralProjection | ROTE leaves `backend/rote/` for the kernel's Python package. Pairing by QR or code in place of the compiled-in server (`Configuration.swift:13-14`, `app/build.gradle.kts:298,331-332`, `deployment/release-profile.json`). A list of the owner's nodes in settings. Device identity and presence keys in the Secure Enclave, StrongBox or TPM in place of software seeds (`MeshEnrollment.swift:149`). The sign-in nonce bound to the device key. |
| AstralPlane | The `(issuer, subject)` owner key. No migration of old data is needed. |

**Exit criteria.**

- With `sandbox.ai.uky.edu` blocked, a Mac and an iPhone paired to a node on the owner's network
  complete a chat with a tool call.
- With Keycloak blocked after sign-in, the session keeps working and tool calls still dispatch.
- A token issued for one node is refused by another.
- A second issuer's token carrying a first issuer's subject is refused.
- Raising the stop counter halts new effects on the node.

**After this phase** the owner can retire the sandbox whenever they choose. Nothing later depends on
it.

---

## Phase 2: the log and sync

**Goal.** Every device holds the owner's history and stays current with the others.

**Depends on.** Phase 0 (decision on C2), phase 1.

**Work.**

| Repository | Work |
|---|---|
| Kernel | Event log and SQLite store. Feeds and sync. Transport with local-network discovery and relay fallback. Roster, key groups, key epochs, the recovery key and pairing. A new feed after a restore. Confidentiality labels on events and the sync rule for them. Encrypted attachment chunks. Device-side adaptation of UI, ported from `AstralProjection/backend/rote/adapter.py` and tested against its existing cases. Checkpoint countersigning. |
| AstralPlane | Event tables with hash ids. Existing chat tables derived from events. A `SCHEMA_REVISION` bump with the usual guarded migration. |
| AstralDeep | The node writes all chat activity as events and syncs through the kernel's Python binding. Notifications become events. The server-held HMAC audit chain is replaced by the node's signed feed. History written by a phase 1 node is converted to events once. The node keeps serving thin clients that have not been ported. |
| AstralProjection | Each client embeds the kernel, renders chats from the local log, shows history offline, queues input offline, and shows honest sync state. The "send it again" path on disconnect (`AppModel.swift:2863-2899`) goes away. Local notifications when a synced event needs the owner. |

**Exit criteria.**

- A chat started on the iPhone appears on the Mac, and the reverse, with the internet off and both on
  one network.
- A phone in airplane mode shows synced history and accepts input, which is delivered when it
  reconnects.
- Two devices that each continued the same chat while apart show two branches.
- After a device is removed, it cannot decrypt events written afterwards, and its new events are
  refused by the others.
- An altered event, a gap, or a feed shorter than a checkpoint a peer has countersigned is detected by
  another peer.
- A device restored from a backup starts a new feed and is not mistaken for its old self.
- The same generated surface renders correctly on Mac and iPhone from one stored event, with no server
  adapting it.
- The body of a PHI-labeled event is not sent to a peer without PHI approval. That peer receives the
  header only.

---

## Phase 3: the pocket runtime

**Goal.** A phone with no other device reachable runs a turn, within a bound.

**Depends on.** Phase 2. LETS device-profile design from phase 0.

**Work.**

| Repository | Work |
|---|---|
| Kernel | The turn engine with a step journal (section 10.1). The provider layer with the clocks, retries and fallback chain of section 10.2 and the argument pipeline of 10.3. The gate stack of section 9.4, tested against vectors derived from `_run_project_bound_gate_stack`. Tickets (8.2). Flow labels and the quarantined reader (9.2). The LETS holder and verifier. An MCP client for remote servers. An adapter interface for on-device models. |
| LETS | Period budgets derived from a signed policy and the roster. The device profile. |
| AstralProjection | Host-drawn consent signed by the presence key (Projection #138). On-device model adapters: Apple's Foundation Models, Android's on-device model where present, a local runtime on Windows. Credentials in the device keystore, synced as sealed items. Phone-safe local tools. |
| AstralDeep | The node verifies tickets from peers and runs its own gates. The Python loop is run against the kernel's conformance vectors. Receipt verification for Mac host effects (Deep #560). |

**Exit criteria.**

- *(live)* An iPhone in airplane mode completes a chat using the on-device model, with a notice that
  it did.
- An iPhone with internet and no reachable peer completes a turn that calls a remote MCP tool.
- When a device's share for the period is spent, the next effect is refused with a clear reason. A
  transfer from another device restores it.
- Two devices that derive a period's budget apart arrive at the same budget and disjoint shares.
- A phone that has heard from no other member for a day drops to its solo share: it keeps reading and
  chatting, and performs an effect outside the mesh only with consent for that action.
- A consent prompt for an effect is drawn by the client, signed by the presence key, and a changed
  action after approval is refused.
- A ticket from a removed device, or from before a stop, is refused by a peer and by a node.
- Untrusted content that tries to trigger an outward effect produces a consent prompt naming its
  source, across a fixed corpus of recorded attacks replayed with the fake provider.
- Untrusted content remembered in one turn still marks a later turn that loads it.
- *(live)* The same defenses are measured against adaptive attacks with a live model and with a small
  on-device planner. The attack success rate is reported. Its pass threshold is set in phase 0.
- The Python loop and the kernel loop pass the same conformance vectors.

---

## Phase 4: voice on the device

**Goal.** Conversational voice with interruption, with no voice server.

**Depends on.** Phase 3's provider layer and on-device model adapters.

**Work.**

| Repository | Work |
|---|---|
| AstralProjection | One duplex audio graph with echo cancellation per platform. Voice activity and end-of-turn detection. Streaming transcription (replacing the final-only path in `AppleLocalSpeech.swift` and `LocalSpeechEngine.kt`). Streaming speech. The interruption logic of section 12.3. The watch as an audio satellite of the phone. Push-to-talk and wake phrase on the Mac (Projection #142). Model downloads with size shown first. The voice model is LFM3-Audio-2B at 3 bits on MLX for recognition, speech and short spoken replies. It is downloaded from Hugging Face the first time someone taps the mic, and there is no voice until it is installed (owner, 2026-11-12). The server's hosted voice models are no longer used. |
| Kernel | The fast-path and full-answer split. Speculative generation on partial transcripts. Recording only what was spoken. The rule that a risky action cannot be approved by voice alone. |
| AstralDeep | Retire `backend/voice_agent/` and the LiveKit dependency once clients no longer use them. |

**Exit criteria.**

- *(live)* Speaking over the assistant lowers its audio within about 100 ms and silences it within
  500 ms of the person starting to speak, measured on device.
- A cough or a short acknowledgement does not stop it.
- After an interruption, the chat history contains only the assistant words that were played.
- *(live)* A voice session works in airplane mode on hardware with an on-device model.
- A risky action proposed in a voice session cannot be approved by saying yes.
- No voice path contacts a voice server.

---

## Phase 5: durable runs and learning

**Goal.** Work that survives a restart and a change of device, and a learning loop that is gated.

**Depends on.** Phase 3.

**Work.**

| Repository | Work |
|---|---|
| Kernel | Executor leases and handoff. The goal judge with round, stall and wait limits. Schedules owned by the executing peer. Compaction with a verbatim tail and a notes document. The learning loop of section 11.3: staging, static gate, replay evaluation, trial, ledger. |
| AstralProjection | Run status and handoff controls. Operating-system continued-work support on phones. The review, undo and forget surfaces (Deep #553, Projection #133). A desktop agent pack: the first-party Python agents run as a supervised local process inside the Mac and Windows apps, so a desktop with no node can use them. |
| AstralPlane | The learning ledger and goal runs as event-derived tables (Plane #119, #117). |
| AstralDeep | The node adopts the kernel's turn engine through the Python binding. The server-side scheduler and persistent-agent loops become users of executor leases. `prompt_optimization/` supplies the evaluation harness for replay (Deep #349). |

**Exit criteria.**

- Killing the app mid-turn and reopening it resumes the turn without repeating an effect.
- A step dispatched with no recorded result is shown as outcome unknown and is not retried
  automatically.
- A goal started on the phone is handed to the Mac and finishes there.
- A proposed skill is adopted only after its replay checks pass (deterministic checks on recorded tool
  calls and outcomes), never widens a permission, and can be undone.
- A proposal whose evidence includes untrusted content is never adopted automatically, across a fixed
  corpus of injection cases.
- A Mac with no node runs a bundled first-party agent.

---

## Phase 6: generative UI and protocol surfaces

**Goal.** The changes in spec section 13, and standard protocols between peers.

**Depends on.** Phase 3.

**Work.**

| Repository | Work |
|---|---|
| AstralPrimitives | Value bindings, action capabilities, required spoken and compact forms, node provenance, handle-only links and images. An A2UI catalog for the vocabulary. |
| Kernel | The decoding grammar generated from the schema. Surface documents and patches. Action events checked against capabilities. A2UI import and export. MCP between peers with the ticket in request metadata. MCP Tasks for long calls. |
| AstralProjection | Renderers for the new fields on each client. Removal of the Python adaptation layer once the kernel's passes its tests. |
| AstralDeep | Signed A2A agent cards required at registration. Selective publication per node (Deep #244, #288). Hash-pinned MCP tool definitions. |

**Exit criteria.**

- Every surface in the test corpus generated under the grammar parses and validates. The validator,
  not the grammar, rejects a value binding or action capability that does not resolve.
- *(live)* The rate of valid surfaces from live models, including the small on-device model, is
  measured and reported.
- A value shown in a bound component equals the tool result it came from.
- A button on a surface cannot trigger an action beyond the capability of the turn that made it.
- A surface with no spoken form for an actionable component is rejected.
- A live surface updated on the Mac updates on the phone, and catches up after the phone was offline.
- An A2UI surface from an outside agent renders through primitives and is labeled untrusted.
- A changed MCP tool definition is treated as a new tool.

---

## Phase 7: shared services, institutions and the web

**Goal.** The pieces that need a host or an institution.

**Depends on.** A host (the UofL VM). Phase 3 for institutions.

**Work.**

- A blind relay and mailbox, and a push gateway that sends content-free wakes. Small, stateless apart
  from stored ciphertext, and installable by anyone.
- The lost-device removal notice held at the mailbox.
- The institution node profile: several owners partitioned by owner, PHI approval declared in its
  descriptor, fresh identity attestation required, its own gates run on every ticket.
- The Keycloak move: a runbook for adding the new issuer to rosters and re-attesting devices.
- The web client, either as a satellite of a node or as a peer running the kernel in the browser. That
  choice is made here, not now.

**Exit criteria.**

- Two devices that are never online together converge through the mailbox, which holds only ciphertext.
- *(live)* A backgrounded phone is woken by a push that carries no content, then syncs.
- A removal notice reaches a device that comes online after every other device is off.
- An institution node refuses a ticket with a stale identity attestation and refuses PHI-labeled work
  from an unapproved peer.
- Devices keep working through an issuer change.

---

## How this is tested

- Required CI stays deterministic: in-process fake peers, a fake relay and mailbox, a fake identity
  issuer, and the fault-injecting fake model provider from phase R. No live third-party service.
- Conformance vectors from phase 0 run against the kernel and the Python node in every phase.
- Each suite finishes inside the existing time limits.
- Timing criteria (deadlines, the interruption bounds) are tested in CI against a controlled
  clock and measured live on devices. They are not wall-clock gates on shared runners.
- Security changes are verified through the real dispatch path, including denials.
- Live checks on real devices are listed separately in each phase report and are never implied by a
  green test run.

## Issues to open when a phase is approved

These replace issues that [issue-triage.md](issue-triage.md) recommends closing and re-filing.

| Phase | New issue | Replaces |
|---|---|---|
| R | One argument pipeline and repeat guard for every tool call | Nothing. New |
| R | Separate model-call deadlines and a per-turn fallback chain | Nothing. New |
| 0 | One turn engine with shared conformance vectors | Deep #239 |
| 1 | Bootstrap a personal node | Deep #246, #293 |
| 2 | Notification policy evaluated on the device | Deep #237 |
| 2 | Device-local store for worktree bindings | Plane #120 |
| 3 | Receipt enforcement through kernel dispatch and node ingress | Deep #220 |
| 3 | Connector credentials in device keystores | Plane #122 |
| 3 | Outcome-unknown for calls to another peer | Projection #125 |
| 4 | On-device voice pipeline, iOS and Android | Projection #28, #29 |
| 5 | Schedules owned by the executing peer | Deep #550 |
| 6 | MCP Tasks for completion subscriptions | Deep #267 |
| 7 | Content-free push gateway | Deep #270 |
| later | Cresco as an optional transport and compute adapter | Deep #242 |

## The first three things to do

1. Start phase R. It is independent and its tests are the fake provider every later phase reuses.
2. Answer spec section 21, then run the phase 0 build spike.
3. Fix the four defects found during research. They are recorded privately.
