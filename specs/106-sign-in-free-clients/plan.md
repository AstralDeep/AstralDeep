# Plan for spec 106

**Date:** 2026-11-13. **Status:** plan only, nothing started. It begins after spec 105 merges. This is a
program plan in the shape of 105's: phases, the work in each repository, and how to tell a phase is
finished. The tasks for every phase are in [tasks.md](tasks.md).
**Companions:** [spec.md](spec.md), [tasks.md](tasks.md), [research.md](research.md).

Read [spec.md](spec.md) first. Section numbers refer to it unless a line says "105". Decision ids are
the spec's. A statement marked **today** comes from [research.md](research.md). Everything else is
planned and nothing is built.

## Preconditions

1. **Spec 105 is merged.** Every phase starts after it. This plan edits 105's contracts.
2. **Plane on SQLite is enabled in Deep.** Phase 4 needs it. **Today** Deep still calls
   `create_postgres_runtime` and expects PostgreSQL's registry (research C86 to C88, C199).
3. **Three questions only the owner can answer** (spec section 16).

| Question | Who answers | Needed by |
|---|---|---|
| 1. Mac sandbox default if Apple removes `sandbox-exec` (C29) | The owner, once phase 0 brings a recommendation | End of phase 0, since phase 4 builds the Mac backend |
| 2. Where a pasted key goes by default, every device or only those picked at entry (O11) | The owner | Before phase 3, which builds key connections |
| 3. What the watch does after the cut, and what the paused Android and Windows clients show | The owner | Before phase 7, which deletes their sign-in code |

The spec gives no deadline for any of them. The "needed by" column is this plan's proposal.

## Order

| Phase | Needs | Runs beside |
|---|---|---|
| 0. Spikes | Spec 105 merged | Nothing |
| 1. Identity cut | 0 | Nothing |
| 2. Connections with MCP over HTTP | 1 | 4 |
| 3. A2A, stdio, key connections | 2 | 4, 5 |
| 4. Docker-free node | Phase 1, the phase 0 sandbox and bundle spikes, Plane on SQLite | 2, 3 |
| 5. PHI on the device | 2 | 3, 4 |
| 6. Sharing | 1 and 5 | 4 |
| 7. Keycloak removal | 1 to 6 finished on macOS and iOS | Nothing |

- Phase 1 blocks everything after it, phase 4 included. No spike result feeds phases 1, 5, 6 and 7, so
  the spec gives no fallback for them.
- A phase is finished when its automated criteria pass in CI and its live criteria, marked *(live)*,
  have been shown on macOS and iOS. Live criteria are recorded demonstrations and not CI gates.
  "Specified", "implemented", "test-passing", "live-verified", "merged" and "deployed" are reported
  separately.
- Client order is macOS, then iOS, then the Vessel command line. Android and Windows are paused (O9).
- **Contract rule.** 105's contracts README says a format change raises its version and that the
  document, its vectors and its reference module change together. Every contract task here closes only
  when all three are in one change and the reference tests pass.
- **Open item.** The spec does not say whether revised and new contract documents live in 105's
  `contracts/` folder or under spec 106. Task T1.1 decides and every later phase follows it.

## Phase 0: spikes

**Goal.** Get a written result for each risky call before anything relies on it.
**Depends on.** Spec 105 merged. **Repositories.** AstralProjection (Seatbelt files, device tests),
AstralVessel (Landlock), AstralDeep (bundle, MCP and A2A spikes), `astraldeep.github.io`.
**Contracts.** None.

**Work.**
1. Seatbelt `enforce` runs a bundled Python tool whose only route is a loopback proxy, building on
   `ComputerSandboxProfile.swift` and `MacComputerEgressProxy.swift`. **Today** the default is shadow
   mode, which allows everything. Test the Virtualization.framework fallback, set the minimum Landlock
   version and read MXC as a policy layer.
2. Build the Python bundle, measure it, sign and notarize it inside the Mac app, and test whether the
   App Sandboxed build can host it.
3. Run the MCP adapter against a fixture OAuth server, test the HTTPS callback on macOS and iOS and
   check the A2A reader against a card with each scheme.
4. Publish the metadata document and app-link files on `astraldeep.github.io` once the owner approves
   (O16).

**Tests.** The Seatbelt spike also refuses a read outside its policy. The fixture server covers a `401`
with resource metadata, no `S256` and no registration endpoint.

**Exit criteria.**
- A written result for each item with the call it supports or reverses.
- The Seatbelt spike refuses a write outside the workspace and any connection except to the proxy.
- The HTTPS callback returns to the app on a real Mac and iPhone *(live)*.

**Live.** The HTTPS callback, and the notarized bundle launching on a clean Mac account.
**If a spike fails.**

| Spike | Fallback the spec gives |
|---|---|
| Seatbelt `enforce` | A Virtualization.framework Linux VM behind the same interface (C29). Linux binaries only and heavier |
| Virtualization.framework | None. The owner answers question 1 with the evidence |
| Landlock minimum, bundle and signing, App Sandboxed hosting, HTTPS callback, MCP adapter, A2A reader | None. Untrusted code is refused below the Landlock minimum. A failure elsewhere is a finding for the owner |

## Phase 1: identity cut

**Goal.** A person is their mesh keys. Creating a mesh, pairing a device and authenticating a caller
need no identity server and no network.
**Depends on.** Phase 0. It blocks every later phase.
**Repositories.** AstralKernel, AstralDeep, AstralPlane, AstralVessel, AstralProjection.
**Contracts.** `roster.md` at a new version (drop `attest`, `issuer`, `phi_authority`, `phi_approval`,
the `issuers` field of genesis, the `attestation` field of add, and the state fields in 6.3). `event.md` drops the context
`astral.attestation/v1`. The contracts README drops the test issuer keys and `attestation.json`.
`ticket.md` gains the owner-minted ticket for callers that are not mesh devices (O13).

**Work.**
1. Contract changes, vectors and reference code. No compatibility reader, since 105 is not deployed (C2).
2. Kernel: mesh creation as one local call. `begin_mesh_creation`, `complete_mesh_creation(id_token)`,
   `reattest` and `attestation.rs` go. A seeded test mesh with software keys, accepted only when
   `ASTRAL_ENV=development`.
3. Deep and Plane: the owner id becomes the mesh id (C1), replacing `backend/shared/node_identity.py`
   and Vessel's derivation in `astralvessel/deployment.py`.
4. Deep: the ticket verifier is the only door. A bearer JWT is refused and mock auth leaves the tree.
   An owner-minted ticket serves callers that are not mesh devices.
5. Vessel: pairing stops sending an owner token (`astralvessel/node.py`) and the issuer prompt and
   `KEYCLOAK_*` exports go. Projection: create a mesh and pair a device with no sign-in.
6. Edit `AGENTS.md` with the section 13 wording (O19).

**Tests.** Golden path with the network blocked. Edge cases: two meshes on one machine, recovery-key
restore, a removed device. Denials: a bearer-only request, an expired or revoked ticket, a ticket for
tool A used on tool B. Failures: a placeholder secret in production, a seeded test mesh with
`ASTRAL_ENV` unset. All through the real dispatch path.

**Exit criteria.**
- A Mac and an iPhone each create a mesh and pair with the network blocked *(live)*.
- A request with only a bearer JWT is refused.
- A production boot with a placeholder secret exits 78 and `ASTRAL_ENV` unset means production.
- No test uses mock auth.
- A script holding an owner-minted ticket calls a named tool and is refused for any other.

**Live.** Create and pair on a real Mac and iPhone with the network off.
**Open items.** The ticket's fields and lifetime are for this phase to settle (6.4). The spec does not
say whether the owner id change needs a Plane `SCHEMA_REVISION` bump. `AGENTS.md` requires one for any
schema change, so task T1.9 checks the schema first.

## Phase 2: connections with MCP over HTTP

**Goal.** An owner connects an OAuth-protected MCP server from any device and signs in only to that
server, as in the 7.8 walkthrough.
**Depends on.** Phase 1. **Repositories.** AstralKernel, AstralDeep, AstralProjection, AstralVessel,
AstralPlane (only if needed), `astraldeep.github.io`.
**Contracts.** A new connection contract: the record (C8), the per-device state events and the approval
signature. Event names and field spelling are chosen here (7.2). It reuses the definition hash in
`tools.md` section 2. `event.md` gains the family.

**Work.**
1. Connection contract, vectors and reference code. Keystore on Apple platforms (Keychain) and on
   headless Linux (Secret Service or a kernel-wrapped file), keyed by connection id and issuer (C9),
   with the per-device state events.
2. Adapter 1 on the kernel and the node, following 7.4.1, on the stateless revision. **Today**
   `remote_mcp_connectors.py` speaks `2025-11-25` and keeps tokens in the connector record.
   Discovery goes through the approved egress path (C14), `backend/shared/external_http.py` on a node.
3. First-connect card drawn by the host and signed with the presence key (C13). Sign-in in the system
   browser. Headless Vessel uses the device grant or a pasted redirect (C11).
4. Step-up, refresh and every failure row in 7.7. Pins move into the connection record.
5. Fixture OAuth and MCP servers. The live metadata document goes up on the website.

**Tests.** Golden path through the fixture. Edge cases: no refresh token, scopes from `401`, from
`scopes_supported` and none. Denials: no `S256`, a mismatched issuer or `iss`, token passthrough, a
changed tool hash. Failures: unreachable server, refused refresh, no credential on the executing
device. A scan of events, logs and model context for token bytes.

**Exit criteria.**
- Against the fixture: discovery, required `S256`, `resource`, the `iss` check, step-up, refresh, a
  refused refresh, the dynamic registration fallback and a refused token passthrough all pass.
- A changed tool definition is refused until approved again.
- No token appears in an event, a log line or a model context.
- The section 7.8 example works end to end against a fixture server. Add on the Mac, sign in on the
  iPhone, let a token expire, sign in again *(live)*.

**Live.** The 7.8 walk on a Mac and an iPhone in one mesh. The owner completes each sign-in.
**If a spike fails.** The spec gives none for the adapter or the callback. A phase 0 callback failure
stops this phase until the owner decides.
**Open item.** The spec does not say whether the connection family needs a Plane table. T2.2 decides
and a table brings the `SCHEMA_REVISION` rule.

## Phase 3: A2A, stdio and key connections

**Goal.** The other connection kinds work, each with the auth its server declares.
**Depends on.** Phase 2 and the answer to question 2. **Repositories.** AstralKernel, AstralDeep,
AstralProjection.
**Contracts.** The connection contract revised for `a2a`, `mcp-stdio`, `http-api` and `model` and the
card pin. `keys.md` revised so `key.credential` carries pasted keys only (C15).

**Work.**
1. A2A adapter for API key, HTTP bearer and basic, OAuth 2.0 and OpenID Connect with PKCE. **Today** the
   code publishes only its own `keycloak_oidc` scheme and reads none (research B97).
2. Extended card, signed card with pinned signer, unsigned card pinned by hash and origin (O12),
   and `TASK_STATE_AUTH_REQUIRED` as a sign-in prompt.
3. The stdio client. **Today** none exists (research B87). It runs on the existing macOS Seatbelt path in
   enforce mode, takes credentials from the keystore by environment and never runs on iOS.
4. Key connections in native secure fields, synced as sealed credential items. LLM Factory is a `model`
   connection with a pasted key (O18).

**Tests.** Golden path per scheme. Edge cases: an extended card, several requirements. Denials: a
changed unsigned card, a signed card with a new signer, a key opened on a device it was not sealed to,
a key typed into chat. Failures: `AUTH_REQUIRED` with the server down, a stdio server that tries the
network.

**Exit criteria.**
- A fixture A2A agent signs a device in for each supported scheme.
- A stdio server runs under enforce mode and reaches no network except through the proxy.
- A pasted key opens only on the owner's devices it was sealed to and never appears in the clear in an
  event.
- A changed unsigned card is refused until approved again.

**Live.** A stdio server on the Mac and a pasted key synced to the iPhone. The owner types the key.
**If a spike fails.** None for A2A. Stdio rides on the Seatbelt spike, whose fallback is the VM backend.
**Open item.** The spec does not say who moves the stdio servers onto phase 4's executor interface.
This plan gives it to phase 4 (T4.15).

## Phase 4: Docker-free node

**Goal.** The Mac app, the iPhone app and the AstralVessel command line run with no Docker (O3).
**Depends on.** Phase 1, the phase 0 sandbox and bundle spikes, and Plane on SQLite. It runs beside
phases 2 and 3.
**Repositories.** AstralVessel, AstralDeep, AstralProjection, AstralPlane, AstralKernel.
**Contracts.** The executor policy document with its five fields (10.3). The spec does not name it.
T4.1 decides its name and place.

**Work.**
1. Executor interface and policy. macOS backend on Seatbelt, always `enforce` for untrusted code. Linux
   backend on Landlock and seccomp from `confine.py` plus bubblewrap. Below the Landlock minimum,
   untrusted code is refused. A loopback egress proxy with a hostname allowlist, and the sandbox denies
   every other network path (C28).
2. The node bundle, signed and notarized in the Mac app, supervised by launchd or a systemd user unit.
   **Today** the Apple clients have no Python node supervisor (research C82).
3. `astralvessel up`, `check` and `down` stop calling `docker compose`. **Today**
   `astralvessel/stack.py` and `operate.py` drive Docker for all three. Vessel's isolation probes
   become the conformance suite that every backend passes with no containers. The no-Docker CI job
   follows (see Verification).
4. The C32 removals from the user path: `backend/start-docker.sh`, the Docker aliases in
   `agent_peer_auth.py`, `/app/.frame_trace`, and the `docker run` step in
   `scripts/node/install-personal-node.sh`. The container backend stays for institutions and CI and is
   never auto-selected (C31). The phase 3 stdio servers move onto the executor interface.

**Tests.** The conformance suite (10.5). Failures: a tool that ignores proxy settings, UDP, a hostname
that resolves to a private address, no enforcing backend.

**Exit criteria.**
- `astralvessel up` runs on a Mac and a Linux host with no Docker on the path *(live)*.
- Every backend passes the conformance suite.
- The no-Docker CI job finishes inside 30 minutes.
- Untrusted code is refused where no enforcing backend exists.

**Live.** `astralvessel up` on a real Mac and a real Linux host with Docker off the path.
**If a spike fails.** For the Mac sandbox, the Virtualization.framework backend behind the same
interface (C29). For the Landlock minimum and the bundle, the spec gives none.

## Phase 5: PHI on the device

**Goal.** The device that holds content labels it, and the owner alone decides where PHI may go (O2, O8).
**Depends on.** Phase 2, for the flow class and the model connections that need approval.
**Repositories.** AstralKernel, AstralDeep, AstralProjection. LETS only if the flow input needs it.
**Contracts.** `labels.md` (the flow input takes `endpoint_phi_approved` from the destination list),
`policy.md` (a PHI destination entry), `keys.md` (the PHI group is the owner's devices, with an
exclusion). Detector vectors are shared between Python and the kernel.

**Work.**
1. Port the rules of `backend/personalization/phi_gate.py` and `backend/shared/phi_redactor.py` to the
   kernel. Fail closed, and uncertain means PHI (C17).
2. The on-device model where hardware allows, starting with the contextual filter in Deep #318.
3. The PHI destination entry, signed with the presence key, one destination at a time, with the warning
   card from 8.3. No wildcard.
4. The flow input reads the destination list. A server hint can only tighten a class.
5. The PHI group follows the owner's devices, and the owner may exclude one (O14).

**Tests.** The same vectors in both languages. Edge cases: uncertain content, a hint that tries to lower
a class. Denials: PHI to a model endpoint with no approval, a wildcard destination, an excluded device.
Failures: the detector missing or erroring.

**Exit criteria.**
- The same vectors pass in Python and in the kernel.
- A failed or missing detector labels content PHI.
- PHI to a model endpoint is refused until the owner approves it by name.
- An iPhone with no node labels and gates its own content *(live)*.

**Live.** An iPhone with no node refuses clinical-looking input to an unapproved endpoint.
**Open items.** Research E42 says the contextual filter is not qualified. The spec gives no accuracy
measure against Presidio. T5.3 proposes one and the owner approves it. The spec does not say whether
LETS changes. The policy contract never read an attestation (C3).

## Phase 6: sharing

**Goal.** Two people share one item between their meshes with a `view` grant, and revoke it.
**Depends on.** Phases 1 and 5. **Repositories.** AstralKernel, AstralDeep, AstralProjection.
**Contracts.** A contacts and grants contract (invitation, comparison code, contact states, grant,
revocation). `sync.md` gains the second session kind that admits a contact's device by roster head
(9.3). `event.md` gains the families and `keys.md` the item key wrapped to a contact's devices. T6.1
names the new documents.

**Work.**
1. Contacts by one-time invitation, with the comparison code over both mesh ids and roster heads. A
   roster change returns the contact to `unverified`.
2. `view` grants signed by the presence key, replicated item by item over the second session kind.
   The sender follows the contact's roster. Revocation rotates the item key.
3. PHI items need a contact marked as a PHI destination, confirmed each time (C25). Contact events carry
   untrusted integrity. Connections and credentials are never shared (C26).

**Tests.** Two meshes in one test. Edge cases: a contact's new device, a changed roster. Denials: a
removed device, an event after revocation, a PHI item for a non-destination contact, an unknown label,
a contact event treated as an owner instruction. Failure: a contact offline at revocation.

**Exit criteria.**
- Two meshes exchange a `view` grant and the contact reads the item live *(live)*.
- A contact's new device receives the item and a removed one does not.
- After revocation the contact cannot read new events.
- A PHI item is refused for a contact that is not a PHI destination.
- A contact's event is labeled untrusted.

**Live.** Two owner-controlled meshes on separate devices, comparing the code in person.
**Open item.** The spec does not say how this coexists with `backend/orchestrator/artifact_share.py`
and `FF_ARTIFACT_SHARING`. T6.11 decides.

## Phase 7: Keycloak removal

**Goal.** Product code contains no Keycloak.
**Depends on.** Phases 1 to 6 finished on macOS and iOS. The deletion needs only phases 1 to 3. It
waits for the rest so the final rerun covers every exit criterion.
**Repositories.** AstralDeep, AstralProjection, AstralVessel, AstralKernel. **Contracts.** None.

**Work.**
1. Delete the modules in 6.4, the Keycloak parts of `auth.py`, `mesh_admission.py` and `token_verify.py`,
   and the client sign-in screens. **Today** 31 Apple, 20 Android and 20 Windows files mention
   Keycloak, OIDC or PKCE, tests and fixtures included (research B134).
2. Remove the leftover `KEYCLOAK_*` names and the Vessel and Kernel mentions. Add a check that fails on
   Keycloak in non-test product code. Rerun the full suites and every earlier exit criterion.

**Exit criteria.**
- A search for Keycloak in non-test product code finds nothing.
- Every earlier exit criterion still passes.

**Live.** A fresh install on a Mac and an iPhone creates a mesh and connects the fixture server.
**Open item.** **Today** the watch signs in through `backend/orchestrator/device_login.py`, a
Keycloak device grant. The spec does not say what the watch does after the cut, and O9 pauses Android
and Windows without saying what their apps show. Spec question 3 asks the owner (TO.5). Until the
owner answers, this phase deletes the Keycloak code on every client and leaves those three with a
plain "not available yet" screen.

## Cross-repository landing order

A change lands in this order so no repository pins something that does not exist yet.
1. **AstralKernel.** Code, its Python binding and its Apple library.
2. **AstralPlane.** Schema work only if a task needs it, with a `SCHEMA_REVISION` bump, repeat-safe
   migration and recovery procedure. Deep's pin moves only after that exact revision is qualified.
3. **AstralDeep.** Moves the pins in `config/astral-composition.json` (Plane, and the kernel package if
   Deep consumes one). Contracts, vectors and reference code land here.
4. **AstralVessel.** Pins the new Deep and kernel, since it bundles both.
5. **AstralProjection.** Last. Its kernel library pin moves here, and the clients render Deep's chrome.

AstralPrimitives does not change. LETS changes only if phase 5 needs it. **Open item.** The spec
does not say how each repository pins the kernel. T1.8 records the mechanism and every later bump
uses it.

## Verification and CI

- Start with the narrowest tests and widen with risk. For merge-level confidence mirror the explicit
  invocations in `.github/workflows/ci.yml`, since root pytest discovery skips suites.
- Changed Python lines keep at least 90% coverage. Tests cover golden paths, edge cases, denials and
  failures. Security changes run through the real dispatch path.
- Required gates stay deterministic. The fixture OAuth, MCP and A2A servers are local. No live
  third-party network service sits in a required gate, and no wall-clock bounds on shared runners.
- **The no-Docker job (phase 4).** It installs the node and runs it on a hosted runner with no Docker on
  the path, against the local fixtures, and runs the conformance suite. It finishes inside the 30
  minute cap. An over-budget job gets cheaper fixtures or loses its slowest tests, and is never
  sharded, raised or waived. **Open item.** T4.14 chooses its file name and place.
- Docker stays in development, existing CI and institutions (O3). macOS and iOS finish every phase.
  Apple gates are `swift test` on `AstralCore` and the affected unsigned `xcodebuild` schemes.

## Rules for whoever implements this

- An agent never performs a sign-in to any service and never types a credential. The owner completes
  every sign-in, enters every key and approves every Touch ID prompt. Test builds use the seeded test
  mesh and fixture servers.
- Nothing is pushed, merged, released or published without the owner's say. Local commits for work
  inside a task are fine. Each publish to `astraldeep.github.io` needs the owner's approval.
- `AGENTS.md` is edited in phase 1 with the section 13 wording (O19), and not before. No model picks an
  auth method, reads a token or types a credential (C10).
- Source files carry no spec or task ids. Each opens with a header of at most three sentences and holds
  no other comments except a one-line reason where essential.
- Keep unrelated changes and user data out of commits. Follow the vault checkpoint rule in `AGENTS.md`.

## What this plan does not cover

- **Android and Windows.** Paused until their kernel port (O9). No pairing-and-signing library is built.
- **`contribute` sharing.** A later spec (O10). Phase 6 builds `view` only.
- **The institution deployment recipe.** A later spec (O15). This plan ends with the Keycloak removal
  and the owner's UK example runs against a fixture server (C33).
- MCP enterprise-managed authorization, client credentials and DPoP (recognized and deferred, 7.4).
  Removing Docker from development, CI or institution deployments. Changes to AstralPrimitives.
