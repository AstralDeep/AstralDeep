# Tasks for spec 106

**Date:** 2026-11-13. **Status:** nothing started. No task is done. It begins after spec 105 merges.
Read [spec.md](spec.md) for intent and [plan.md](plan.md) for order and exit criteria. Evidence for
the paths is in [research.md](research.md). A path "decided in this task" means the spec and research
name no location, and the task records the choice.

Rules for every task. Follow `AGENTS.md`. A contract task changes the document, its vectors and its
reference code together. Source carries no spec or task ids. An agent never signs in to a service
and never types a credential. Nothing is pushed or published without the owner. Live tasks are
marked *(live)* and need real devices.

Columns are repository, work, where it lands and what closes it. `Deep` is AstralDeep, `Projection`
is AstralProjection, `Kernel` is AstralKernel, `Site` is `astraldeep.github.io`. `105 contracts` is
`specs/105-decentralized-personal-mesh/contracts/` with its `reference/` and `vectors/` folders.

## Owner tasks

| ID | Task | Needed by |
|---|---|---|
| TO.1 | Answer question 1, the Mac sandbox default if `sandbox-exec` goes, after phase 0 brings evidence | End of phase 0 |
| TO.2 | Answer question 2, where a pasted key goes by default | Before phase 3 |
| TO.3 | Review the C calls that no O row names (the spec says they are unreviewed) | Before phase 1 |
| TO.4 | Approve publishing the metadata document and app-link files on `astraldeep.github.io` | T0.13, T2.15 |
| TO.5 | Answer question 3, what the watch does after the cut and what the paused Android and Windows apps show | Before phase 7 |
| TO.6 | Complete every live sign-in and key entry, and every Touch ID prompt, during verification | Each *(live)* task |
| TO.7 | Approve the PHI accuracy measure proposed in T5.3 | Phase 5 |

## Phase 0: spikes

| ID | Repo | Work | Lands | Closes with |
|---|---|---|---|---|
| T0.1 | Projection | Seatbelt `enforce` spike. Run a bundled Python tool whose only route is the loopback proxy | Built on `apple-clients/AstralCore/Sources/AstralCore/Computer/ComputerSandboxProfile.swift` and `apple-clients/AstralApp/AstralApp/Computer/MacComputerEgressProxy.swift` | A write outside the workspace and any connection except to the proxy are refused. Written result |
| T0.2 | Projection | Virtualization.framework fallback spike behind the same interface | Spike folder decided in this task | Written result with a recommendation for question 1 |
| T0.3 | Vessel | Set the minimum Landlock version and say what happens below it | `astralvessel/confine.py` (`REQUIRED_ABI`) | Written result naming the minimum |
| T0.4 | Vessel | Read Microsoft Execution Containers as a policy layer over three platforms | Result written in this folder, name decided in this task | Written result with the call it supports or reverses |
| T0.5 | Deep | Build the relocatable Python 3.11 bundle with locked wheels, poppler, libmagic, ripgrep and the spaCy model | `scripts/install_local_components.py` and `config/astral-composition.json` are the inputs, output decided in this task | Bundle builds and a node module imports. Size and build time recorded |
| T0.6 | Projection | Sign and notarize the bundle inside the Mac app | `AstralApp/AstralApp.xcodeproj/project.pbxproj` and the entitlements files | Notarization accepted. Written result *(live)* |
| T0.7 | Projection | Test whether the App Sandboxed build can host the node and apply profiles to its children | `AstralApp-macOS.entitlements` and `AstralApp-DirectDownload.entitlements` | Written result with a yes or no |
| T0.8 | Deep | MCP over HTTP adapter spike against a fixture OAuth server on revision `2026-07-28` | Spike folder decided in this task. Compare `backend/orchestrator/remote_mcp_oauth.py` | Discovery, `S256` refusal and registration order shown. Written result |
| T0.9 | Projection | Test the HTTPS callback with `ASWebAuthenticationSession` on macOS and iOS | Spike target decided in this task | Callback returns to the app on a real Mac and iPhone *(live)* |
| T0.10 | Deep | A2A reader spike. One fixture card per scheme | Compare `backend/orchestrator/orchestrator.py` lines 5943 and 6126 | Reader parses each card. Written result |
| T0.11 | Deep | Draft the Client ID Metadata Document and the app-link association files | Local files, folder decided in this task | Files validate against the MCP revision's rules |
| T0.12 | Deep | Write the phase 0 results page: each item, the call it supports or reverses | This folder, name decided in this task | Every plan.md phase 0 item has a result |
| T0.13 | Site | Publish the metadata document and app-link files | `astraldeep.github.io`, path decided in T0.11 | Both files load over HTTPS. Owner approved first (TO.4) |

## Phase 1: identity cut

| ID | Repo | Work | Lands | Closes with |
|---|---|---|---|---|
| T1.1 | Deep | Decide where revised contracts live and record it | Note at the top of the roster change, decided in this task | Decision written and linked from `plan.md` |
| T1.2 | Deep | Roster contract at a new version. Drop `attest`, `issuer`, `phi_authority`, `phi_approval`, genesis `issuers`, add `attestation` and the 6.3 state fields | `105 contracts` `roster.md`, `vectors/roster.json`, `reference/astral_mesh_ref` | Reference tests pass. `vectors/attestation.json` removed |
| T1.3 | Deep | Drop context `astral.attestation/v1`. Update the README fixture list and remove the test issuer keys | `105 contracts` `event.md`, `README.md`, `reference/scenarios.py` | Reference tests pass with no issuer fixtures |
| T1.4 | Deep | Contract for the owner-minted ticket for callers that are not mesh devices. Settle fields and lifetime | `105 contracts` `ticket.md` and its vectors | Vectors for a named tool pass and another tool is refused |
| T1.5 | Kernel | Mesh creation as one local call. Remove `begin_mesh_creation`, `complete_mesh_creation(id_token)` and `reattest` | `crates/astral-mesh/src/roster.rs` and the facade | Creation test passes with the network blocked |
| T1.6 | Kernel | Remove `attestation.rs` and the issuer record. Update the scenario tests | `crates/astral-mesh/src/attestation.rs`, `scenario_tests.rs` | Workspace tests and strict clippy pass. Vectors match T1.2 |
| T1.7 | Kernel | Seeded test mesh with software keys, accepted only when `ASTRAL_ENV=development` | Location decided in this task | Test accepts it in development and refuses it with `ASTRAL_ENV` unset |
| T1.8 | Kernel | Record how each consumer pins the kernel | Note in the kernel README, decided in this task | Note lists the Deep, Vessel and Projection mechanism |
| T1.9 | Plane | Check whether the owner id change touches schema. Bump `SCHEMA_REVISION` with a guarded migration only if it does | `components/AstralPlane/` migration registry | Written check. Contract tests pass on the current schema |
| T1.10 | Deep | Owner id becomes the mesh id. Replace the issuer-derived id | `backend/shared/node_identity.py` and the owner-id call sites | Isolation tests pass with mesh ids. No `(issuer, subject)` derivation remains |
| T1.11 | Deep | Ticket verifier is the only door. Refuse a bearer JWT | `backend/orchestrator/auth.py`, `mesh_admission.py` | A bearer-only request is refused through the real dispatch path |
| T1.12 | Deep | Owner-minted ticket for non-device callers, named tools, expiry, revocation | Node API module decided in this task | A script calls a named tool and is refused for any other. Revocation refuses it |
| T1.13 | Deep | Remove mock auth from the new tree. Delete `USE_MOCK_AUTH` branches | `backend/orchestrator/auth.py`, `session_authority.py`, `shared/a2a_security.py` | A search of tests and code finds no mock auth |
| T1.14 | Deep | Production boot with a missing or placeholder secret still exits 78 | `backend/orchestrator/session_store.py` line 150 area, `backend/tests/test_fail_closed_boot.py` | Test passes. `ASTRAL_ENV` unset is production |
| T1.15 | Vessel | Pairing sends no owner token. Drop the issuer prompt and the `KEYCLOAK_*` exports | `astralvessel/node.py`, `operate.py`, `compose.py`, `deployment.py` | Vessel conformance check for pairing passes with mesh ids |
| T1.16 | Projection | Create a mesh and pair a device with no sign-in on macOS and iOS | `apple-clients/AstralApp/AstralApp/Mesh/` | Both platforms create and pair with the network blocked *(live)* |
| T1.17 | Deep | Edit `AGENTS.md` with the spec section 13 wording | `AGENTS.md` | Diff matches the section 13 rows. Owner reviews |
| T1.18 | Deep | Gate-level checks for phase 1 through the real dispatch path | `backend/tests/`, files decided in this task | Denial tests pass. Changed Python lines at 90% coverage or more |

## Phase 2: connections with MCP over HTTP

| ID | Repo | Work | Lands | Closes with |
|---|---|---|---|---|
| T2.1 | Deep | Connection contract. Record fields (C8), state events, approval signature. Choose event names | `105 contracts` or the folder from T1.1, `event.md` family and vectors | Reference tests pass. Record holds no secret field |
| T2.2 | Plane | Decide whether the connection family needs a table. Bump the schema only if it does | `components/AstralPlane/` | Written decision. Migration tests if a table is added |
| T2.3 | Kernel | Connection events and the per-device state fold | Crate decided in this task | Fold tests for `not signed in`, `signed in`, `needs attention` |
| T2.4 | Projection | Keychain credential store keyed by connection id and issuer | `apple-clients/AstralCore/` location decided in this task | Round trip test. Two issuers for one id do not collide |
| T2.5 | Vessel | Headless Linux keystore with Secret Service or a kernel-wrapped file | Location decided in this task | Test on a headless runner reads back a stored secret |
| T2.6 | Kernel | Adapter 1 discovery. `401`, `resource_metadata`, RFC 9728, RFC 8414 and OpenID order, issuer equality, required `S256` | Crate decided in this task | Fixture tests pass including no `S256` refused |
| T2.7 | Kernel | Client registration order. Pre-registered, metadata document, dynamic, ask for a client id. Set `application_type` | Same crate | Fixture tests cover each branch |
| T2.8 | Kernel | Authorization code flow with `resource` and the RFC 9207 `iss` check | Same crate | Mismatched `iss` abandons the flow. Code discarded |
| T2.9 | Kernel | Bearer on every request, token only to its server, step-up on `insufficient_scope`, refresh | Same crate | Passthrough refused. Step-up unions scopes. Retry limit holds |
| T2.10 | Deep | Move the node client to the stateless revision and out of the connector record | `backend/orchestrator/remote_mcp_connectors.py`, `remote_mcp_oauth.py`, `remote_mcp_pins.py` | Old handshake gone. No `access_token`, `refresh_token` or client secret in the record |
| T2.11 | Deep | Discovery fetches through the approved egress path | `backend/shared/external_http.py` | A private target is refused. Local server allowed only when the owner adds it |
| T2.12 | Projection | First-connect card drawn by the host. Presence-key signature. PHI-source toggle | `apple-clients/AstralApp/AstralApp/Mesh/` | Card shows address, issuer host, scopes, tools with hashes, labels, egress hosts. Test on both platforms |
| T2.13 | Projection | System browser sign-in with `ASWebAuthenticationSession` | Same area | Fixture sign-in completes on macOS and iOS simulator |
| T2.14 | Vessel | Device authorization grant, else print the URL and accept the pasted redirect | CLI module decided in this task | CLI test against the fixture |
| T2.15 | Site | Publish the live metadata document for the real app identity | `astraldeep.github.io`, path from T0.11 | Document loads. Owner approved (TO.4) |
| T2.16 | Deep | Fixture OAuth and MCP servers for CI. Local, deterministic | `backend/tests/` folder decided in this task | Fixtures serve every case in the phase 2 exit criteria |
| T2.17 | Deep | Failure rows from 7.7 as tests. Pins into the connection record | `backend/tests/` | Each row has a test. Changed hash gives `gate.tool_def_changed` |
| T2.18 | Deep | Scan events, logs and model context for token bytes | `backend/tests/` | Scan test passes over a full fixture run |
| T2.19 | Projection | The 7.8 walk, Mac then iPhone, with expiry and second sign-in | Verification report | Walk recorded on real devices *(live)* |

## Phase 3: A2A, stdio and key connections

| ID | Repo | Work | Lands | Closes with |
|---|---|---|---|---|
| T3.1 | Deep | Revise the connection contract for `a2a`, `mcp-stdio`, `http-api`, `model` and the card pin | Same folder as T2.1, with vectors | Reference tests pass |
| T3.2 | Deep | Revise `key.credential` for pasted keys only | `105 contracts` `keys.md`, `vectors/keys.json`, `vectors/credential.json` | Reference tests pass. Sign-in tokens are not valid credential items |
| T3.3 | Kernel | A2A adapter. Read `securitySchemes` and requirements. API key, bearer, basic, OAuth, OpenID Connect | Crate decided in this task. **Today** no reader exists | One fixture agent per scheme signs a device in |
| T3.4 | Kernel | Extended card after sign-in. Signed card as JWS with pinned signer | Same crate | New signer is refused until approved |
| T3.5 | Kernel | Unsigned card pinned by hash and HTTPS origin, shown as unsigned | Same crate | A changed card is refused until approved again |
| T3.6 | Kernel | `TASK_STATE_AUTH_REQUIRED` becomes a device sign-in prompt | Same crate | The transition asks for credentials and authorizes nothing |
| T3.7 | Deep | Stdio client on the node. Credentials by environment from the keystore. Never on iOS | New module, decided in this task. **Today** none exists | A fixture stdio server runs and gets only the credential it needs |
| T3.8 | Projection | Run stdio servers under Seatbelt `enforce` | `apple-clients/AstralCore/.../Computer/` | The server reaches no network except through the proxy |
| T3.9 | Projection | Native secure fields for API keys and model keys, synced as sealed items | `apple-clients/AstralApp/AstralApp/Mesh/` | A key opens only on devices it was sealed to. Never clear in an event |
| T3.10 | Deep | LLM Factory as a `model` connection with a pasted key | `backend/llm_config/` | Default model call works from a pasted key in a fixture |
| T3.11 | Deep | Fixture A2A agents in CI | `backend/tests/` | Each scheme runs with no live network |
| T3.12 | Projection | Stdio on the Mac and a pasted key on the iPhone, shown on devices | Verification report | Recorded *(live)* |

## Phase 4: Docker-free node

| ID | Repo | Work | Lands | Closes with |
|---|---|---|---|---|
| T4.1 | Vessel | Executor interface and the five-field policy document. Name and place of the contract | Decided in this task | Document, vectors and reference code agree |
| T4.2 | Projection | macOS backend. Seatbelt through `sandbox-exec`, untrusted code only in `enforce` | `apple-clients/AstralCore/.../ComputerSandboxProfile.swift`, `ComputerSandboxPolicy.swift` | Conformance suite passes. Shadow mode refused for untrusted code |
| T4.3 | Vessel | Linux backend. Landlock and seccomp from `confine.py` plus bubblewrap | `astralvessel/confine.py` and a new backend module | Suite passes. Below the minimum, untrusted code is refused |
| T4.4 | Vessel | Egress proxy with hostname allowlist, private-address refusal, no other network path | Reuse `egress/proxy.py` logic, location decided in this task | Private address, off-policy port and UDP refused |
| T4.5 | Vessel | Executor verifies the ticket before it runs anything | `astralvessel/executor.py` | A call with a bad ticket is refused |
| T4.6 | Deep | Enable Plane on SQLite for the node. Confirm the precondition | `backend/orchestrator/plane_composition.py` | A node boots on SQLite and the Plane suite passes |
| T4.7 | Deep | Bundle build per OS and architecture | Output decided in T0.5 | Bundle runs the node with no Python on the host |
| T4.8 | Projection | Embed, sign and notarize the bundle in the Mac app. launchd supervision | `AstralApp/AstralApp.xcodeproj/project.pbxproj` | Notarized app launches the node on a clean account *(live)* |
| T4.9 | Vessel | systemd user unit for Linux | Location decided in this task | Unit starts and stops the node |
| T4.10 | Vessel | `astralvessel up`, `check` and `down` as processes. Remove `docker compose` | `astralvessel/stack.py`, `operate.py`, `cli.py` | Commands run with no Docker on the path |
| T4.11 | Vessel | Isolation probes become the conformance suite | `astralvessel/probes.py`, `conformance.py` | Every backend passes with no containers |
| T4.12 | Deep | Remove Docker-only assumptions from the user path | `backend/start-docker.sh`, `backend/orchestrator/agent_peer_auth.py`, `orchestrator.py` frame trace, `scripts/node/install-personal-node.sh` | No user path reaches them. Tests updated |
| T4.13 | Vessel | The container backend for institutions and CI, never auto-selected | `astralvessel/compose.py` | Test shows no auto-selection on a user install |
| T4.14 | Deep | New CI job that installs and runs the node with no Docker on the path | `.github/workflows/` file decided in this task | Job runs the conformance suite and the fixtures, under 30 minutes |
| T4.15 | Deep | Move phase 3 stdio servers onto the executor interface | Node stdio module from T3.7 | Phase 3 tests still pass |
| T4.16 | Vessel | `astralvessel up` on a Mac and a Linux host with no Docker | Verification report | Recorded *(live)* |

## Phase 5: PHI on the device

| ID | Repo | Work | Lands | Closes with |
|---|---|---|---|---|
| T5.1 | Deep | Shared detector vectors from `phi_gate.py` and `phi_redactor.py` rules | `backend/tests/` and a vectors folder decided in this task | Vectors pass in Python |
| T5.2 | Kernel | Port the detector rules. Fail closed, uncertain means PHI | Crate decided in this task | Same vectors pass in the kernel. Missing detector labels PHI |
| T5.3 | Deep | Propose a measure for the contextual filter against Presidio | This folder, decided in this task | Proposal written for TO.7 |
| T5.4 | Projection | On-device model adapter for the filter where hardware allows | `apple-clients/AstralApp/AstralApp/OnDevice/` | Adapter adds to the floor and never lowers a label |
| T5.5 | Deep | PHI destination entry in policy, signed with the presence key | `105 contracts` `policy.md`, vectors and reference | One destination per entry. A wildcard is refused |
| T5.6 | Kernel | Flow input reads the destination list. Server hint can only tighten | `astral-authz` area | Hint cannot lower a class. Model endpoint refused until approved by name |
| T5.7 | Projection | Warning card for a destination, no wildcard | `apple-clients/AstralApp/AstralApp/Mesh/` | Card test on macOS and iOS |
| T5.8 | Kernel | PHI group follows the owner's devices, with an exclusion | `105 contracts` `keys.md` and the kernel | An excluded device gets no PHI body |
| T5.9 | Projection | An iPhone with no node labels and gates its own content | Verification report | Recorded *(live)* |

## Phase 6: sharing

| ID | Repo | Work | Lands | Closes with |
|---|---|---|---|---|
| T6.1 | Deep | Contacts and grants contract. Invitation, comparison code, states, grant, revocation | Folder from T1.1, vectors and reference | Reference tests pass. Code matches on both sides |
| T6.2 | Deep | Second sync session kind admitting a contact's device by roster head | `105 contracts` `sync.md`, vectors | Vectors pass. A contact reads only the granted item |
| T6.3 | Kernel | Contacts as events with `unverified` and `verified`. Roster change returns to `unverified` | Crate decided in this task | Fold tests |
| T6.4 | Projection | Invitation by link or QR and the comparison code screen | `apple-clients/AstralApp/AstralApp/Mesh/` | Both screens show one string on macOS and iOS |
| T6.5 | Kernel | `view` grant signed by the presence key. Item key wrapped to the contact's devices | Same crate | Contact reads the item. A third mesh cannot |
| T6.6 | Kernel | Item-scoped replication to the contact's mesh | Same crate | Only the item's events replicate |
| T6.7 | Kernel | Follow the contact's roster. Wrap to new devices, stop for removed ones | Same crate | A new device receives the item and a removed one does not |
| T6.8 | Kernel | Revocation event with item key rotation | Same crate | Contact cannot read new events. The app says copied data cannot be recalled |
| T6.9 | Kernel | PHI item needs a PHI destination contact, confirmed each time. Unknown label refused | Same crate | Refused for a contact that is not a PHI destination |
| T6.10 | Kernel | Contact events carry untrusted integrity | Same crate | An agent never treats one as an owner instruction. Effect with a contact value needs consent |
| T6.11 | Deep | Decide how sharing coexists with `artifact_share.py` and `FF_ARTIFACT_SHARING` | `backend/orchestrator/artifact_share.py`, decision in this task | Written decision. Tests for any change |
| T6.12 | Projection | Two meshes exchange a grant and the contact reads live | Verification report | Recorded *(live)* |

## Phase 7: Keycloak removal

| ID | Repo | Work | Lands | Closes with |
|---|---|---|---|---|
| T7.1 | Deep | Delete `web_auth.py`, `device_login.py`, `offline_grant.py`, `native_session_custody.py` and `delegation.py` | `backend/orchestrator/` | Backend suites pass |
| T7.2 | Deep | Remove the Keycloak parts of `auth.py`, `mesh_admission.py`, `token_verify.py`, with `jwks_cache.py` and `auth_clients.py` | `backend/orchestrator/`, `backend/shared/` | Suites pass. Coverage 90% on changed lines |
| T7.3 | Projection | Delete Apple client sign-in screens and Keycloak settings | `apple-clients/AstralCore/.../Configuration.swift`, `Auth/` | `swift test` and unsigned `xcodebuild` pass |
| T7.4 | Vessel, Kernel | Remove remaining Keycloak mentions and `KEYCLOAK_*` names | `astralvessel/`, kernel docs and README | Search finds nothing |
| T7.5 | Deep | CI check that searches non-test product code for Keycloak | `.github/workflows/` and `scripts/`, decided in this task | Check finds nothing and fails when one is added |
| T7.6 | Deep | Rerun every earlier exit criterion on macOS and iOS | Verification report | All pass. Live ones shown again *(live)* |
