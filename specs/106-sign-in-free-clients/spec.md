# Spec 106: Astral without a sign-in or Docker

**Date:** 2026-11-13. **Status:** design only, not implemented (O7). It starts after spec 105 merges.
Nothing here is built, tested or deployed. **Companions:** [research.md](research.md) (evidence, with
file and source references). **Builds on:**
[spec 105](../105-decentralized-personal-mesh/spec.md) and its
[contracts](../105-decentralized-personal-mesh/contracts/README.md).

**Scope:** the repositories of spec 105 (AstralDeep, AstralProjection, AstralPlane, AstralPrimitives,
LETS) plus AstralKernel and AstralVessel. AstralPrimitives does not change.

---

## 1. How to read this spec

Spec 105 put Keycloak at the edge of the mesh. This spec removes the edge, and removes Docker from
anything a user installs.

- A sentence marked **today** describes code or text that exists now, with paths relative to the
  repository that owns them. Every reference into spec 105 was checked against its files.
- A sentence marked **proposed** describes this design. Nothing proposed exists.
- O is the owner's decision. C is this design's call, with the alternative that lost. The owner has
  agreed to no C yet.

| Word | Meaning |
|---|---|
| Mesh, roster, node, peer, kernel, ticket, presence key, flow labels, executor | As in [spec 105](../105-decentralized-personal-mesh/spec.md) sections 4.1 and 8.2 |
| Outside service | Anything the mesh does not run: an MCP server, an A2A agent, an HTTP API, a model endpoint, an institution |
| Connection, credential, adapter | A signed record naming one outside service with no secret in it. The token or key for it, in one device's keystore. The code that signs a device in |
| Contact, grant, PHI destination | Another person's mesh as this mesh knows it. The owner's signed permission for one contact and one item. A place the owner approved by name to receive PHI |

---

## 2. Summary

Using Astral needs no account, no identity server and no Docker.

**Today** the clients sign in to Keycloak at `iam.ai.uky.edu`. Spec 105 keeps one use for it: a mesh
cannot be created until a device returns a Keycloak ID token, which the roster stores as an
attestation ([roster.md](../105-decentralized-personal-mesh/contracts/roster.md) section 6). A user of
AstralVessel needs Docker with Compose, and a Mac without Docker is not a supported host
(AstralVessel `docs/index.md` and `docs/security/platforms.md`).

**Proposed:** a person is their mesh keys. Every outside service does its own sign-in, and the owner
signs in to that service directly from each device. Six ideas carry it.

1. **The mesh id is the principal.** Creating a mesh is local: generate keys, write the first roster
   entry, show the recovery key. No network call.
2. **A connection is a signed record with no secret.** It syncs like any event. The token behind it
   never syncs. Each device signs in once and keeps its own token in its own keystore.
3. **Code picks the sign-in method from what the server declares.** The model never chooses one and
   never sees a token. For MCP over HTTP the adapter follows revision `2026-07-28`.
4. **PHI is labeled on the device that holds it,** from the connection's class and an on-device
   detector. Where PHI may go is the owner's signed choice, one named destination at a time.
5. **Sharing between people needs only the two clients.** A contact is another mesh, verified by a
   comparison code. A grant wraps one item's key to the contact's devices.
6. **Tool code runs in an OS-native sandbox.** The node ships as a bundle and runs as a process. The
   four Vessel containers become processes. Docker stays for development, CI and institutions.

Section 7.8 walks through the owner's example. A University of Kentucky MCP server protected by OAuth
is added on the Mac, approved once and signed in at the server's own login page. The iPhone in the
same mesh then asks for its own sign-in.

---

## 3. Goals, non-goals, anti-goals

**Goals**

- A new user creates a mesh on a Mac or an iPhone with no network, no account and no Docker.
- An owner connects an OAuth-protected MCP server from any device and signs in only to that server.
- The Mac app, the iPhone app and the AstralVessel command line run with no Docker on the machine.
- PHI handling and sharing between two people work with the clients alone.
- Every 105 gate stays on, and MCP and A2A stay first-class with their own auth rules.

**Non-goals**

- An identity service, account system, password store or verified real names.
- An OAuth authorization server run by Astral, or a multi-user profile with built-in IAM (O4).
- Syncing tokens between devices (O5).
- The Android and Windows kernel ports (question 1).
- MCP enterprise-managed authorization, client credentials and DPoP (section 7.4).
- Removing Docker from development, CI or institution deployments.

**Anti-goals: the design has failed if any of these happens**

- A token leaves the keystore of the device that obtained it, or a device runs a call with a
  credential another device obtained.
- A model picks an auth method, reads a token or types a credential.
- Untrusted code runs without an enforcing sandbox because a backend was missing.
- PHI-labeled content reaches a destination the owner did not approve by name.
- A user install picks Docker without being told to.
- An agent treats a contact's event as the owner's instruction.

---

## 4. Decisions

### 4.1 Decided by the owner (2026-11-13)

| # | Decision |
|---|---|
| O1 | Identity rests on mesh keys only. No identity provider is needed to use Astral. Keycloak attestation leaves the roster. |
| O2 | PHI handling and sharing between people work with the clients alone. No reliance on Keycloak or the UK IAM server. |
| O3 | Nothing a user installs needs Docker: the Mac app, the iPhone app, the AstralVessel command line. Tool and agent-code isolation moves to OS-native sandboxes. Docker stays for development, CI and institution deployments. |
| O4 | An institution's node or MCP server is an outside service that picks its own auth. It may run Keycloak as its own OAuth server. Astral ships no multi-user profile with built-in IAM. |
| O5 | A connection syncs to every device in the mesh. Each device signs in once itself. Tokens never leave the keystore of the device that obtained them. |
| O6 | Outside servers handle auth their own way, per the MCP or A2A rules or whatever they pick. The owner's example is a UK resource behind an OAuth-protected MCP server. Connect, authenticate with that server, use it. |
| O7 | Spec only for now. Nothing is implemented until the owner approves. |

O1 answers 105 question 3, "a mesh with no identity provider at all", with "in". It also ends a
contradiction in 105. Section 15 item 1 says a new device "can join unattested with reduced rights"
([spec.md](../105-decentralized-personal-mesh/spec.md) line 886). The roster contract says "Creating a
mesh needs one attestation" ([roster.md](../105-decentralized-personal-mesh/contracts/roster.md)
line 156). Both go.

### 4.2 Calls made in this design, for the owner to check

| # | Call | Alternative that lost, and why |
|---|---|---|
| C1 | The principal is the mesh id, the hash of the first roster entry as in 105. The owner id used for isolation everywhere becomes the mesh id. It replaces ids derived from issuer and subject (`backend/shared/node_identity.py`, Vessel's `oidc.<sha256(issuer)>.<sub>` in `astralvessel/deployment.py`). | Keep an optional attested issuer pair. It keeps an identity server in the contract. |
| C2 | Creating a mesh needs only the device: generate keys, write the first roster entry, show the recovery key. No network call. The roster contract gets a new version that drops attestation entries, accepted issuers, re-attestation and the unattested state. 105 is not deployed, so there is no compatibility reader. | Tolerate old entries. It keeps dead code for data that does not exist. |
| C3 | One allowance per policy. The "smaller allowance for unattested devices" rule goes. 105's policy contract never had an attestation input. | A smaller allowance for devices with no platform check. It brings back two classes of device. |
| C4 | A node authenticates every caller by a roster-listed device key plus a ticket, never by a bearer JWT from an identity server. The Keycloak JWKS path, the client PKCE sign-in screens (Apple, Android, Windows) and mock auth are removed. Development uses a seeded test mesh with software keys, allowed only when `ASTRAL_ENV=development`. Production still fails closed (exit 78) on placeholder or missing secrets. Thin clients with no kernel yet (Android and Windows, 105 C9) pair with a node through the 105 pairing flow and sign requests with a keystore device key, through a small pairing-and-signing library. | Keep a bearer path beside the keys for thin clients. It leaves an identity server on the critical path. |
| C5 | RFC 8693 token exchange leaves Astral's core. Delegation is by 105 tickets only. If an institution's resource wants token exchange, it happens between the connection's OAuth client and the institution's own authorization server. | Keep RFC 8693 at the edge, as 105 section 8.2 says. It keeps Keycloak-shaped code for a case the adapters now cover. |
| C6 | Names of people are self-asserted and unverified. Two people verify each other with the 105 comparison codes. | Verified names through a directory or domain proof. It needs an operator. |
| C7 | Blind shared services (relay, mailbox, push gateway) admit by mesh key with rate limits and quotas. An operator may add a silent platform check (App Attest, Android Key Attestation) or run invite-only. No account either way. | Admit by account. It rebuilds the identity server one layer down. |
| C8 | A connection is a signed record in the event log: id, kind (`mcp-http`, `mcp-stdio`, `a2a`, `http-api`, `model`), endpoint or command, display name, discovered auth scheme, issuer, scopes, pinned tool definitions (105 section 9.5), flow class, egress hosts. It holds no secret. It syncs like any event. | A settings row with the token inside, as `backend/orchestrator/remote_mcp_connectors.py` stores connectors today. It cannot sync without syncing the secret. |
| C9 | Credentials live only in the device keystore (Keychain, Android Keystore, DPAPI or TPM on Windows, Secret Service or a kernel-wrapped file on headless Linux), keyed by connection id and issuer. Per-device state is `not signed in`, `signed in` or `needs attention`. Only the state is logged, so another device can show "signed in on Mac". | Sealed credential items synced to every device (105 `key.credential`). One sign-in per service, but a stolen refresh token on any device. O5 rules it out. |
| C10 | The auth adapter is chosen from what the server declares. Code decides. The model never picks an auth method and never sees a token. Section 7.4 lists the adapters. | Let a model or a setting pick. It puts the choice where an injected page can reach it. |
| C11 | Sign-in uses the system browser (RFC 8252): `ASWebAuthenticationSession` on Apple platforms, Custom Tabs with App Links on Android, the system browser with a loopback IP redirect on desktop and the command line. Headless AstralVessel uses the device authorization grant where the server offers it, else prints the URL and accepts the pasted redirect. | An embedded web view. RFC 8252 forbids it and the owner could not tell a real login page from a copy. |
| C12 | The device that executes the call must hold its own credential. If it does not, the effect fails closed and a sign-in prompt is routed to that device. A phone calls remote HTTP servers itself with its own token. | One device holds the token and proxies. It breaks when that device sleeps and it moves a token between devices. |
| C13 | First connect is an owner approval signed by the presence key. The owner sees the server address, the issuer host that will ask for sign-in, scopes, tools and labels. A model may propose a connection. Only the owner adds one. | Let a model add a server after an approval in chat. The prompt would come from model output, which 105 forbids. |
| C14 | Discovery and metadata fetches go through the approved egress path (`backend/shared/external_http.py` on a node, the kernel's equivalent on a device). HTTPS only. Private addresses only when the owner adds a local server on purpose. | Each adapter fetches directly. Every adapter would need its own SSRF guard. |
| C15 | One rule for every secret in the first version, including pasted API keys and model keys: per device, never synced. | Sync pasted keys sealed to each device. A plausible later step, and question 2. |
| C16 | A Client ID Metadata Document and the app-link association files are static HTTPS files on the project website. They join the list of things that stay centralized. | Dynamic client registration only. The MCP revision deprecates it. |
| C17 | The kernel sets the PHI label on the device from two sources: the connection's flow class (the owner marks a connection as a PHI source and a server hint may only tighten) and the on-device detector (the rules of `backend/personalization/phi_gate.py` as the floor, an on-device model where hardware allows). Uncertain means PHI. No large model on a node is required. | PHI checks that need a large model on a node (105 section 16.2). A phone with no node would have no check. |
| C18 | Where PHI may go is an owner decision, signed with the presence key into mesh policy as a "PHI destination" entry. The owner's own devices and on-device models qualify by default. Every model endpoint and outside service is off until the owner approves it by name, with a plain warning. The institution signing key from 105 `roster.md` line 162 is removed. | Keep institution-signed approvals. 105 never named an institution that would sign. |
| C19 | Astral's PHI gate protects the owner from accidental leaks. An institution's compliance duty is enforced at the institution's own server, through its own auth and terms. Section 8.4 says so. | Present the gate as meeting an institution's rules. The institution cannot see the device. |
| C20 | Device-side gates stay on by default (105 C7). The hash-chained audit stays in the event log. | Relax device gates because no server reviews them. |
| C21 | A person is a mesh. A contact is another mesh id plus that mesh's roster head, added by a one-time invitation (link or QR) and verified by comparison code. Contacts are events in the owner's log with a state: unverified or verified. | A shared directory of people. It is a server. |
| C22 | A grant is signed by the owner's presence key. It names a contact, one item (a chat, a surface or artifact, a file) and a kind. The item's key is wrapped to the contact's current device keys. The item's events replicate to the contact's mesh over the same sync protocol, limited to that item. | A bearer link, as `backend/orchestrator/artifact_share.py` mints today. It says nothing about who reads. |
| C23 | Two kinds. `view` is live and read-only and ships first. `contribute` lets the contact append events and ships last. A contact's events always carry untrusted integrity, so an agent never treats them as the owner's instructions. | Ship both together. `contribute` is the larger risk and question 3 asks whether it belongs here. |
| C24 | The sender follows the contact's signed roster changes, wraps to new devices and stops wrapping to removed ones. Revoking writes a revocation and rotates the item key. Data already copied cannot be recalled. | Wrap once to the devices known at grant time. A new phone of the contact would never see the item. |
| C25 | Sharing a PHI-labeled item needs the contact marked as a PHI destination, confirmed with the presence key each time. Labels travel with the shared events. | A standing PHI flag on a contact. One tap would cover every later item. |
| C26 | Connections and credentials are never shared. A contact connects for themselves. | Share a connection record so the contact skips setup. It hands over the owner's relationship with a service. |
| C27 | The node ships as a self-contained bundle: a relocatable Python 3.11, locked wheels and the native libraries (poppler, libmagic, ripgrep, the spaCy model). One artifact per OS and architecture. Inside the Mac app it is signed and notarized. Supervision is launchd on macOS, a systemd user unit on Linux, a Windows service later. Storage is Plane on SQLite. 105 plans it and Deep does not enable it yet, so it is a dependency. | Ask the user to install Python and run `pip`. Native libraries and models would fail in ways nobody could support. |
| C28 | Vessel's four containers become processes. Node: a process. Executor: a sandboxed child per tool call or agent. Egress: a loopback proxy with a hostname allowlist, and the sandbox denies every other network path. `astralvessel up`, `check` and `down` stop shelling out to `docker compose`. | Keep the four services and have the app start Docker. It is still Docker. |
| C29 | One executor interface, one policy document (file reads, writable workspace, hosts, limits, time), three backends. Section 10.3 gives the rules. | A Virtualization.framework Linux VM on the Mac. It runs Linux binaries only and is heavier. It stays a phase 0 fallback behind the same interface. |
| C30 | Vessel's isolation probes become a conformance suite that every backend passes with no containers. A new CI job installs and runs the node on runners with no Docker on the path, using local fixture OAuth, MCP and A2A servers so required gates stay deterministic and inside the 30-minute budget. | Run the probes only inside containers. "No Docker" would go untested. |
| C31 | The container backend stays for institution deployments and CI. It is never required and never auto-selected on a user install. | Use Docker when present. The owner chose against it (O3). |
| C32 | Docker-only runtime assumptions in Deep leave the user path: `backend/start-docker.sh`, the Docker host aliases in `backend/orchestrator/agent_peer_auth.py`, the `/app/.frame_trace` path, the `docker run` step in `scripts/node/install-personal-node.sh`. | Leave them behind flags. They would keep Docker in the default path. |
| C33 | 105's phase 7 institution profile is replaced. An institution that wants to serve many people runs Deep as an outside service: a resource-server deployment that validates bearer tokens from any configured OAuth issuer, publishes protected resource metadata and partitions data by token subject. It is a deployment recipe, generic to any issuer, never installed by a user. It is the last phase. It is also what makes the owner's UK example connectable. | Keep 105's profile with fresh Keycloak attestation and a move runbook. It binds institutions to one vendor. |

Section 16 lists the questions only the owner can settle.

---

## 5. What spec 105 assumed and what changes

Section numbers are 105's unless a file is named.

| 105 place | Says | Change |
|---|---|---|
| spec 1 idea 3, 3.1 O2, 4.2 topology | Keycloak proves identity at enrollment. Keep the instance for now. A Keycloak box in the picture | The enrollment is the proof. The box and the dependency go |
| spec 7.3, "What Keycloak does now" | Attestation, `(issuer, subject)` principal, 30-day re-attestation, unattested devices, accepted issuers | Whole subsection replaced by section 6 here |
| spec 8.2, last paragraph | Device holds third-party OAuth tokens. RFC 8693 stays for an institution's Keycloak resources | Tokens in keystores stays and is generalized (section 7). RFC 8693 leaves (C5) |
| spec 9.3, "Who grants a PHI approval" | An institution key signs. An owner cannot approve their own device | The owner decides, by name (section 8) |
| spec 9.5 | MCP tool definition pinned by hash. A2A card must be signed to register | Pins move into the connection record. The signed-card rule stays (7.4.2) |
| spec 14, OAuth 2.1 and RFC 8693 rows | Sign-in at Keycloak. Exchange at an institution's Keycloak | OAuth 2.1 is used only toward outside services. The RFC 8693 row goes |
| spec 15 items 1 and 13 | Identity proof stays centralized. Institution nodes need a known operator | Item 1 goes. Item 13 becomes an outside service (section 12) |
| spec 16.2 Vessel row | Sandbox and egress "containers become the executor" | Processes and OS sandboxes become the executor (section 10) |
| spec 17 security row, spec 21 questions 3 and 12 | "Keycloak remains the only source of user identity". Mesh with no provider. Who signs PHI approval | Replaced by section 13. Answered by O1, O2 and C18 |
| roster.md sections 3, 5, 6, 7, 9 | `genesis.issuers`, `attest`, `issuer`, `phi_authority`, `phi_approval` entries, their state fields, both attestations, their error codes | New roster version drops all of it (6.3) |
| roster.md section 8 | Pairing steps 2 and 4 sign in to Keycloak and carry the attestation | Invite, request, comparison code and presence approval stay. The sign-in and attestation go |
| event.md | Context `astral.attestation/v1`. No connection, contact or grant events | Context dropped. Three families added (sections 7 and 9) |
| keys.md sections 2 and 4.1 | PHI group is the members holding a PHI approval. `key.credential` syncs sealed keys | PHI group follows the owner's destination entries (8.3). No credential sync in v1 (C15) |
| mailbox.md sections 3 to 5 | Presence-signed registration, an operator admission list, quotas | Kept. A rate limit and an optional platform check are added (C7) |
| sync.md section 1 | A peer is refused unless its key is on the roster | A second session kind admits a contact's device for one item (9.3) |
| tools.md section 2 | Definition hash over name, description, schema, annotations | Reused as the pin inside a connection record |
| contracts README fixtures, tasks.md kernel FFI | Test issuer keys, `institution/uky` key, `begin_mesh_creation`, `complete_mesh_creation(id_token)`, `reattest` | Fixtures and `attestation.json` go. Creation becomes one local call |

105's policy contract (section 5) never read an attestation, which is why C3 costs nothing. 105 does
not name Docker. Its one container mention is section 16.2, which this spec replaces.

---

## 6. Identity without a provider

### 6.1 The principal and the first device

**Proposed.** The mesh id is the hash of the first roster entry (105 section 7.2) and is the owner id
that isolates data. A device creates a mesh in three steps. Generate the identity, presence and
operations keys, in hardware where the device has it. Write the genesis entry with the first device,
the first epoch and the recovery key's public halves, and no issuer list. Show the recovery key
once. No step uses the network.

**Today** two derivations tie data to an identity server: `backend/shared/node_identity.py` and
Vessel's `astralvessel/deployment.py`. The owner id appears in about 150 to 160 backend files, so
changing it reaches most of the node. 105 chose a clean break and is not deployed, so this spec adds
no migration of owner ids.

### 6.2 What attestation bought, and what replaces each use

| Use in 105 | What it bought | Replacement here |
|---|---|---|
| Mesh creation needed one attestation | A login had happened somewhere | Nothing. Creation is local. Shared services bound abuse by quota (6.5) |
| Principal as `(issuer, subject)` pairs | One stable name across realms | The mesh id. A person with two meshes is two principals |
| Re-attestation every 30 days | Evidence the person still holds an account | None. Removal and the stop counter handle lost devices (105 section 7.5) |
| Accepted issuers, the Keycloak move runbook | Surviving a realm move | Nothing to move |
| Pairing step 2, a sign-in on the new device | The same human owned both devices | Presence-key approval on an existing device after both screens show the comparison code |
| Institution nodes refusing unattested devices | The institution learned the owner's account | The institution runs its own auth (section 11) |
| A smaller allowance for unattested devices | A reduced right for unproven devices | Nothing. The policy contract never read the state (C3) |

The cost is real. Nothing proves that two devices belong to one human or that a mesh belongs to a
human at all. Pairing proves that the person holding an existing device wanted the new one, which is
what Signal's linking flow proves.

### 6.3 The roster after the cut

**Proposed.** The roster contract takes a new version, since 105's change rule raises the version
and moves document, vectors and reference code together. Entry types `attest`, `issuer`,
`phi_authority` and `phi_approval` go. Genesis loses `issuers` and add loses `attestation`. State
loses `unattested`, `stale_attestation`, `principal`, `issuers`, `phi_authorities` and `phi_members`.
Pairing keeps invite, offer, request, invitation, comparison code and presence approval. Every other
105 rule stays: removal beats addition, a removed device's later entries are void, the recovery key
outranks any device, and the stop counter only rises.

### 6.4 How a node knows who is calling

**Today** the node accepts a Keycloak bearer JWT (`backend/orchestrator/auth.py`, checked by
`backend/shared/token_verify.py` and `jwks_cache.py`), keeps tokens in `session_store.py` and has a
mock mode (`USE_MOCK_AUTH`). 105's ticket verifier already requires that the signing key be "an
operations key on the roster" ([ticket.md](../105-decentralized-personal-mesh/contracts/ticket.md)
section 4, step 4).

**Proposed.** The ticket verifier is the only door. A node authenticates a caller by the device key
that signs the request and the ticket it carries. No bearer JWT is accepted. Mesh devices sign with
their operations key. Thin clients with no kernel pair through the 105 flow and sign through the
pairing-and-signing library (C4). Some callers are not mesh devices, such as another agent calling a
node's MCP endpoint. The owner's decisions do not cover them. This design proposes a narrow ticket
the owner mints for that caller, with the format settled in phase 1. Question 11 asks the owner to
confirm.

Phase 7 deletes the Keycloak modules: `web_auth.py`, `device_login.py`, `offline_grant.py`,
`native_session_custody.py`, `delegation.py` and its RFC 8693 exchange, and the Keycloak parts of
`auth.py`, `mesh_admission.py` and `token_verify.py` (all in `backend/orchestrator/` or
`backend/shared/`), plus the client sign-in screens. About 39 non-test backend Python files mention
Keycloak today.

### 6.5 Names, abuse, blind services and development

Names are strings their owner typed. Nothing checks them, and people confirm each other with a
comparison code (9.2).

**Today**, per 105, the mailbox admits a new mesh by a presence-signed registration, supports an
operator admission list of operations keys and caps meshes, items and notices
([mailbox.md](../105-decentralized-personal-mesh/contracts/mailbox.md) section 4 and 6). **Proposed:**
the relay, mailbox and push gateway admit by mesh key and add a per-mesh rate limit. An operator may
add a silent platform check or run invite-only.

| Check | What it proves | What it needs |
|---|---|---|
| Apple App Attest | A key sits in a Secure Enclave and an unmodified app made it | A call to Apple when the key is attested. Availability on Mac apps is unconfirmed |
| Android Key Attestation | A key sits in secure hardware, with boot and patch state | A verifier holding Google's roots and its revocation list. No per-request call |

Neither names a person, and no account exists in any case.

**Development.** Tests and local runs use a seeded test mesh with software keys, accepted only when
`ASTRAL_ENV=development`. `ASTRAL_ENV` unset still means production. A production boot with a
missing or placeholder secret still exits 78, as `session_store.py` does today in production posture.
Mock auth does not exist in the new tree.

---

## 7. Connections

### 7.1 What exists today

- `backend/orchestrator/remote_mcp_connectors.py` stores an owner-scoped connector and starts with the
  `initialize` handshake at protocol version `2025-11-25`. `remote_mcp_oauth.py` follows a
  `WWW-Authenticate` challenge to resource metadata, requires an advertised `S256`, sends `resource`,
  registers dynamically and refreshes, and keeps `access_token`, `refresh_token` and
  `oauth_client_secret` in the connector record. `remote_mcp_pins.py` pins tool definitions.
- No general stdio MCP client exists. No code reads an external A2A agent's `securitySchemes`. The
  orchestrator publishes only its own `keycloak_oidc` scheme.

### 7.2 The connection record

**Proposed.** A connection is an event family with scope `mesh`. Adding one needs the owner's consent
signature, like a 105 grant ([event.md](../105-decentralized-personal-mesh/contracts/event.md)
section 10.3). Event names and field spelling belong to a contract written in phase 2.

| Field | Holds |
|---|---|
| `id`, `name` | A random id unique in the mesh. The display name the owner saw |
| `kind` | `mcp-http`, `mcp-stdio`, `a2a`, `http-api` or `model` |
| `endpoint` or `command` | An HTTPS URL, or a program and arguments for `mcp-stdio` |
| `auth`, `issuer`, `scopes` | The scheme the server declared, the authorization server's issuer for the RFC 9207 check, the scopes the owner approved |
| `tools` | Each tool's name and definition hash ([tools.md](../105-decentralized-personal-mesh/contracts/tools.md) section 2) |
| `flow` | `private` by default, `phi` when the owner marks it a PHI source |
| `egress` | The hostnames it may reach, at least the endpoint host and the issuer host |

A change to any field after approval is a new event and needs consent again. A changed tool
definition has a new hash and is a new tool, and its grant names `<name>@<first 8 hex digits>`
(tools.md section 4). The record holds no token, client secret or pasted key.

### 7.3 Credentials and per-device state

**Proposed.** A credential lives in the keystore of the device that obtained it, keyed by connection
id and issuer (the MCP revision requires keying by issuer). The keystores are Keychain, Android
Keystore, DPAPI with the TPM where present, and on headless Linux Secret Service or a file wrapped by
a kernel-held key.

Each device logs one small state event per connection. `not signed in` means this device holds no
credential. `signed in` means it holds one and its last call was accepted. `needs attention` means the
credential was refused, expired with no refresh, or the server wants more scope. The event carries the
state and the time, never a token, so another device can show "signed in on Mac". Removing a device
does not cancel its tokens, and the owner revokes them at the service, as 105 section 7.5 says.

C15 covers every secret in the first version, so a pasted API key behaves like a token. 105
`key.credential` items stay unused until the owner answers question 2.

### 7.4 The adapters

The kernel picks the adapter from what the server declares. A model never picks one.

| Adapter | Chosen when | What the code does |
|---|---|---|
| 1. MCP over HTTP | `kind` is `mcp-http` | Section 7.4.1 |
| 2. MCP over stdio | `kind` is `mcp-stdio` | The node starts the server in the sandbox and passes credentials by environment from the keystore. The MCP revision says stdio servers should take credentials from the environment. Needs a stdio client that does not exist today. Never on iOS |
| 3. A2A | `kind` is `a2a` | Section 7.4.2 |
| 4. HTTP API or model endpoint | `kind` is `http-api` or `model` | A key typed into a native secure field, never into chat |
| 5. Recognized and deferred | The server declares one | MCP enterprise-managed authorization, MCP client credentials, DPoP. None is built here |

Both MCP auth extensions are stable at revision `2026-07-28`. Enterprise-managed authorization has the
client sign in to an identity provider, exchange there by RFC 8693 and present the result by
RFC 7523. Client credentials is machine-to-machine with pre-registered clients. DPoP is not in the MCP
specification, and its proposal (SEP-1932) is an open draft. A keystore-held DPoP key would fit O5.

#### 7.4.1 MCP over HTTP, revision `2026-07-28`

1. Send the request with no token. On `401` read `resource_metadata` from `WWW-Authenticate`, else
   try `/.well-known/oauth-protected-resource` at the path and then the root.
2. Read the protected resource metadata (RFC 9728) and choose one authorization server. Read that
   server's metadata by RFC 8414 and OpenID discovery in the order the MCP revision fixes. Its issuer
   must equal the issuer used to find it. Require `S256` in `code_challenge_methods_supported` and
   refuse if it is absent.
3. Register a client in this order: pre-registered information, a Client ID Metadata Document when
   the server supports one (C16), dynamic registration when `registration_endpoint` exists, then ask
   the owner for a client id. Dynamic registration is deprecated in this revision and stays as a
   fallback because `remote_mcp_oauth.py` has it. A registration sets `application_type`.
4. Run the authorization code flow in the system browser (7.5) with the scope the `401` named, else
   `scopes_supported`, else none. Send `resource`, the server's canonical URI, in the authorization
   and token requests. Check `iss` on the redirect against the recorded issuer before redeeming the
   code (RFC 9207).
5. Store tokens under connection id and issuer. Send `Authorization: Bearer` on every request, never
   in the query string.
6. On `403` with `insufficient_scope`, ask again with the union of old and challenged scopes, up to a
   retry limit. Refresh with the refresh token when one exists, and never assume one was issued.
7. Send a token only to the server its authorization server issued it for, and never pass it to
   another API.

The revision also made MCP stateless, with no `initialize` handshake and no session id. The existing
client speaks the older handshake, so adapter 1 includes moving it.

#### 7.4.2 A2A

The adapter reads the card's `securitySchemes` and its requirements field, which the A2A text names
`security` in places and `securityRequirements` in others.

- **Schemes:** API key, HTTP bearer and basic, OAuth 2.0 and OpenID Connect with PKCE. Mutual TLS later.
- **Extended card:** after sign-in, fetch it when the card enables it and replace the cached card.
- **Signed cards:** verify a signed card as a JWS and pin its signer when the owner approves. A2A
  does not say which signer to trust, so the pin is this design's policy. 105 section 9.5 requires a
  signed card to register and A2A makes signing optional, so section 15 lists the effect.
- **`TASK_STATE_AUTH_REQUIRED`:** becomes a sign-in prompt on the device. The transition asks for
  credentials and never authorizes anything.

### 7.5 Sign-in

Sign-in uses the system browser and never a web view inside the app (RFC 8252).

| Platform | Mechanism |
|---|---|
| iOS and macOS | `ASWebAuthenticationSession` with an HTTPS callback on the project website (C16) |
| Android | Custom Tabs with an App Link as the redirect |
| Windows and the command line | The system browser with a loopback redirect to `127.0.0.1` or `[::1]` on an ephemeral port |
| Headless AstralVessel | The device authorization grant where offered, else the CLI prints the URL and accepts the pasted redirect |

The MCP revision allows only localhost or HTTPS redirect URIs, which leaves out private-use custom
schemes. That is why Apple and Android use claimed HTTPS links. RFC 8252 discourages the name
`localhost` and prefers the IP literals. Two points are unconfirmed. Apple's primary documentation
for the HTTPS callback form (iOS 17.4, macOS 14.4) could not be read, and reports of flakiness are
secondary. Phase 0 tests it on both platforms.

### 7.6 First connect

**Proposed.** First connect is an owner approval signed with the presence key (C13). The host draws
the card from the discovered facts and never from model output. It shows the server address, the host
of the authorization server that will ask for sign-in, the scopes, each tool with its hash, the labels
(untrusted integrity and the flow class) and the egress hosts. The owner may mark the connection a PHI
source here, and a server's metadata may only tighten that. A model may propose a connection, and the
card still waits for the owner. Every other device then lists the connection as `not signed in`.

A Client ID Metadata Document cannot stop another program on the same machine from using the same
redirect port, per the MCP security text. That is why the card shows the redirect host.

### 7.7 Execution and failure

**Proposed.** The device that runs a call needs its own credential (C12). A call from a turn on the
Mac runs from the Mac with the Mac's token. If the executing device has none, the effect fails closed
and the sign-in prompt appears on that device. Discovery and metadata fetches use the approved egress
path (C14), which on a node rejects private, loopback and link-local targets and pins the address it
checked.

| Failure | What the device does | What the owner sees |
|---|---|---|
| No credential here | Fails the call, logs `not signed in` | A sign-in card on this device |
| Access token expired, refresh works | Refreshes and retries once | Nothing |
| Refresh refused or none issued | Logs `needs attention`, fails the call | A sign-in card here. Other devices show "needs attention on iPhone" |
| `403` `insufficient_scope` | Runs the step-up in 7.4.1 | A card listing the added scope |
| `iss` on the redirect differs | Abandons the flow, discards the code | An error naming both issuers |
| A tool's hash changed | The gate refuses with `gate.tool_def_changed` | An approval card for the new tool |
| Signed A2A card has a new signer | Refuses to use it | An approval card naming the signer |
| Server unreachable | Fails the call, keeps the credential | A waiting notice, never a done state |

The model sees "sign-in needed" and nothing from any token. Output from an outside service always
carries untrusted integrity (105 section 9.2).

### 7.8 Worked example: a University of Kentucky MCP server

The address `https://mcp.research.example/mcp` and the login host `https://login.research.example`
stand in for the real ones. UK may use Keycloak for its login. Astral does not need to know.

**On the Mac**

1. The owner opens Connections, chooses Add and pastes the address.
2. The Mac fetches it through the egress path with no token. The server answers `401` and points to
   its protected resource metadata. The Mac reads it, finds the authorization server, reads that
   server's metadata and confirms it advertises `S256`.
3. A card shows the address, the login host, the scopes, the five tools it found with their hashes
   and the labels. The owner marks the connection a PHI source because the data is clinical, then
   approves with Touch ID. That signs the consent with the presence key and writes the record.
4. The Mac registers a client with a Client ID Metadata Document if the server supports one, else by
   dynamic registration. The owner sees nothing unless the server offers neither. Then a field asks
   for a client id.
5. A system browser sheet opens at the UK login page. The owner signs in the way UK asks and agrees to
   the scopes. The sheet returns to the Mac.
6. The Mac checks `iss`, redeems the code with `resource` set, stores the tokens in its Keychain
   under the connection id and issuer, and logs `signed in`. The tools appear in chat.

**On the iPhone, in the same mesh**

7. The record arrives through sync. The iPhone lists "UK research server" with the line "signed in on
   Mac, not signed in here". The signed approval from step 3 covers the record, so the phone does not
   ask for approval again.
8. The owner asks something that needs the server. The phone has no credential, so the effect fails
   closed and a sign-in card appears on the phone.
9. The owner taps it. The phone repeats discovery itself, opens `ASWebAuthenticationSession`, and the
   owner signs in at the same UK page. The phone keeps its tokens in its own Keychain and logs
   `signed in`. The Mac now shows "signed in on iPhone". If the server needs dynamic registration, the
   phone registers its own client.
10. The phone calls the server over HTTPS with its own token. Nothing passes through the Mac.

**When the token expires**

11. The access token is short-lived. When it lapses, the device refreshes with its own refresh token,
    if the server issued one, and the owner sees nothing.
12. A week later the server refuses the refresh. The next call on the iPhone gets `401`. The phone
    logs `needs attention` and fails the call, and the model sees "sign-in needed". The owner sees a
    card on the phone and the Mac shows "needs attention on iPhone". The owner signs in on the phone
    again. The Mac's token was never involved.

A connection and its credential are never shared with a contact (C26), and a model never reads or
writes a token.

---

## 8. PHI on the device

### 8.1 What exists today

`backend/personalization/phi_gate.py` screens text and fails closed, using Presidio, which runs on a
node. `backend/shared/phi_redactor.py` holds field and value patterns and fails closed too. 105 puts
"the PHI checks that need large models" on the node (section 16.2) and has an institution key sign any
PHI approval (roster.md section 7), though it names no institution and ships no authority. Its flow
contract takes `endpoint_phi_approved` as a boolean and never says where that comes from
([labels.md](../105-decentralized-personal-mesh/contracts/labels.md) section 4).

### 8.2 Where the label comes from

**Proposed (C17).** The kernel sets the confidentiality label on the device that holds the content.

| Source | Rule |
|---|---|
| The connection's flow class | The owner marks a connection `phi` when approving it. A server hint may raise the class and never lower it |
| The on-device detector | The rules of `phi_gate.py` and `phi_redactor.py` are the floor, ported to the kernel and checked against shared vectors. An on-device model adds to the floor where hardware allows |

Uncertain means PHI. A detector that fails or cannot run labels the content PHI, which matches the
existing fail-closed behavior. No large model on a node is required, so a phone with no node still
labels its own content. 105's issue triage names a local contextual filter as the way to run these
checks without Presidio (Deep #318). That is the first job for the on-device model.

### 8.3 Where PHI may go

**Proposed (C18).** The owner decides one destination at a time, with a presence-key signature, as a
"PHI destination" entry in mesh policy.

| Destination | Default | How it changes |
|---|---|---|
| The owner's own devices, an on-device model | Allowed | The owner may exclude a device |
| A model endpoint, an outside service | Off | The owner approves it by name |
| A contact | Off | The owner marks the contact and confirms each share (9.5) |

The approval card gives a plain warning. The destination is not run by the owner, anything marked PHI
will leave the owner's devices, and Astral cannot see what the destination does with it. The card
names one destination and offers no wildcard. 105's flow rule `flow.phi_to_model` takes its boolean
from this list and does not otherwise change. The label axes, the running label, PHI epoch keys and
the rule that a peer outside the PHI group receives a header and never a body all stay (105 section
9.3 and `sync.md`).

**The PHI key group.** 105 defined the PHI group as the members holding an institution's approval.
An owner could not approve their own device, so a mesh had no PHI members. **Proposed:** the PHI
group is the owner's devices, and the owner may exclude one with a destination entry. This extends
the destination list slightly and is flagged for the owner's check.

### 8.4 What gets weaker

Astral's PHI gate protects the owner from accidental leaks. It does not discharge an institution's
compliance duty (C19). That duty is enforced at the institution's own server, through its own
authentication and terms. The institution cannot see the owner's device and Astral does not pretend
otherwise. An owner can approve a destination that mishandles PHI. Question 4 asks whether that is
acceptable for UK or UofL research use.

---

## 9. Sharing between people

### 9.1 What exists today

105 has one line on it. A non-goal says "Sharing between different people beyond an explicit grant"
(section 2), and a key rule says "individual chat keys by grant, re-wrapped to them" (section 7.4). Its
contracts define no person identity, no grant to a person and no revocation of one. In Deep,
`backend/orchestrator/artifact_share.py` mints revocable snapshot links with a hashed token, behind
`FF_ARTIFACT_SHARING`. A link carries no identity.

### 9.2 Contacts

**Proposed (C21).** A person is a mesh. The owner adds a contact with a one-time invitation, a link
or a QR code built like the 105 invitation. Both screens then show a comparison code. It is computed
like 105's but over both mesh ids and both roster heads, sorted so each side sees the same string.
That sorting follows Signal's safety numbers. Two people compare it by voice or in person.

A contact is an event in the owner's log with a state, `unverified` or `verified`. Verifying needs
the presence key. A change in the contact's roster raises a notice and returns the contact to
`unverified`. Names are what the contact typed (C6).

### 9.3 A grant

**Proposed (C22).** A grant is signed by the owner's presence key and names a contact, one item (a
chat, a surface or artifact, or a file) and a kind. The item's key is wrapped to the wrap keys of the
contact's current devices, as 105 wraps an epoch key. The item's events replicate to the contact's
mesh over the sync protocol, limited to that item's scope. 105 sync admits only roster members
(`sync.md` section 1), so this needs a second session kind that admits a contact's device by the
contact's roster head. A contract written in phase 6 fixes the messages. The contact's devices render
the item locally from the replicated events.

### 9.4 Two kinds

| Kind | The contact can | Ships |
|---|---|---|
| `view` | Read the item live | First |
| `contribute` | Append events to the item | Last, and only if question 3 says yes |

A contact's events always carry untrusted integrity (C23). A contact's message in a shared chat is
data to an agent. Under 105 section 9.3 an untrusted value used as an argument to an outside effect
needs consent, and that covers a contact too.

### 9.5 Following roster changes, and revoking

**Proposed (C24).** The sender follows the contact's signed roster changes. A new device of the
contact gets a wrap. A removed device gets none and stops receiving events. Revoking writes a
revocation event and rotates the item key, so later events are unreadable to the contact.

**Limit.** Data the contact already copied cannot be recalled. The app says so at revocation.

**PHI in a shared item.** Sharing a PHI-labeled item needs the contact marked as a PHI destination,
confirmed with the presence key each time (C25). Labels travel with the shared events, so the
contact's kernel applies its own flow rules. A kernel that does not know a label refuses the event.
A grant covers one item and never the mesh or the log's other scopes. Connections and credentials
are never shared (C26).

---

## 10. Running with no Docker

### 10.1 What exists today

| Where | Today |
|---|---|
| AstralVessel | `astralvessel/stack.py` and `operate.py` run `docker compose` for `up`, `check` and `down`. Its docs require Docker with Compose and either Linux 6.12 or newer with Landlock ABI 6 or Docker Desktop on Apple silicon. Four Compose services: node, executor, a sandbox per agent, egress. `confine.py` applies Landlock and seccomp per call on Linux. `probes.py` runs inside the containers |
| Deep node | A `python:3.11-slim` image with poppler, libmagic, build tools and the `en_core_web_lg` model. `backend/start-docker.sh` is the entrypoint. `agent_peer_auth.py` lists Docker host aliases. `orchestrator.py` writes a frame trace only if `/app/.frame_trace` exists. `scripts/node/install-personal-node.sh` ends by printing a `docker run` command |
| Mac app | AstralProjection `apple-clients/AstralCore/Sources/AstralCore/Computer/ComputerSandboxProfile.swift` builds a Seatbelt profile. Enforce mode is `(deny default)` with network allowed only to a loopback proxy. Shadow mode is `(allow default)` and only logs, and `ComputerSandboxPolicy.swift` defaults to shadow. The app has a per-job loopback egress proxy |
| Deep on Linux | `backend/orchestrator/sandbox.py` sets resource limits and scrubs secrets. `sandbox_bwrap.py` and `sandbox_qualification.py` run a pinned sandbox runtime with bubblewrap. A vendored helper it needs is not tracked in git |
| Windows | No AppContainer, Job Object or other sandbox code was found |

105 does not say how the executor is packaged on a Mac or in a personal install without Docker.

### 10.2 The node as a bundle

**Proposed (C27).** One artifact per OS and architecture holds a relocatable Python 3.11 (the
production image and Ruff's target are 3.11), the locked wheels of the node and its components, the
native libraries the images install today (poppler, libmagic, ripgrep), and the spaCy model Deep
downloads at image build. Inside the Mac app it is signed and notarized with the app. Storage is
Plane on SQLite. Deep still calls `create_postgres_runtime`, so Plane on SQLite is a dependency of
this work. Supervision is launchd on macOS, a systemd user unit on Linux and a Windows service later.
No Python node supervisor exists in the Apple clients today.

**Four containers become processes (C28).** The node is a supervised process. The executor is a
sandboxed child per tool call or agent, and it verifies the ticket before it runs anything. The
per-agent sandbox is the policy applied to that child. Egress is a loopback proxy with a hostname
allowlist, and the sandbox denies every other network path. `astralvessel up`, `check` and `down`
start and stop these processes.

### 10.3 One interface, one policy, three backends

**Proposed (C29).** The executor takes one policy document with five fields: file reads, the one
writable workspace, allowed hosts, limits (memory, processes, open files, file size) and time.

| Platform | Backend | Rules |
|---|---|---|
| macOS | Seatbelt through `sandbox-exec`, building on `ComputerSandboxProfile.swift` | Untrusted code runs in `enforce`, never `shadow`. Seatbelt is deprecated and its profile language is not publicly documented. Codex, Claude Code, Cursor and Gemini CLI still ship it. Phase 0 evaluates a Virtualization.framework fallback behind the same interface |
| Linux | Landlock and seccomp (`astralvessel/confine.py`) plus bubblewrap for namespaces and network | If the kernel lacks the needed Landlock version, untrusted code is refused. Vessel requires ABI 6 today and phase 0 sets the minimum. Bubblewrap needs unprivileged user namespaces, and Ubuntu 24.04 needs an AppArmor change |
| Windows, later | AppContainer plus Job Objects | AppContainer filters the network by capability and not by domain. Job Objects limit resources and restrict neither files nor network. So egress still needs the proxy. Phase 0 evaluates Microsoft Execution Containers (MXC) as one policy layer over all three platforms |
| iOS | None | The phone runs no subprocesses. See 10.4 |

The Mac app's existing governed-command host accepts only the direct-download build, which is not App
Sandboxed. Whether the App Sandboxed build could host the node and apply profiles to its children is
unconfirmed, and phase 0 answers it.

### 10.4 Egress and iOS

**Proposed.** The sandbox denies direct network access. On macOS the Seatbelt profile allows outbound
connections only to the loopback proxy's port. On Linux a network namespace with no route does the
same. The proxy passes only hostnames in the policy and refuses addresses that resolve to private,
loopback, link-local or metadata ranges. The limits are those of the pattern. The proxy filters by the
requested hostname and does not inspect TLS, so a hostname that disagrees with its destination may
pass, and that is unconfirmed here. Tools that ignore proxy settings fail closed. UDP does not pass.

**iOS.** iOS apps cannot create child processes (the source is an Apple forum answer, not Apple
documentation). App Store guideline 2.5.2 also limits an app that downloads code to change its
features. The phone runs kernel-embedded first-party tools and remote HTTP connections. Code
execution routes to the owner's Mac or node through a request event (105 section 4.3).

### 10.5 Conformance, and what stays on Docker

**Proposed (C30).** Vessel's isolation probes become one conformance suite that every backend passes
with no containers. It checks that a child cannot read outside its policy, cannot write outside its
workspace, cannot reach any address except through the proxy, that the proxy refuses a private address
and an off-policy port, and that limits hold. A new CI job installs and runs the node on runners with
no Docker on the path, with local fixture OAuth, MCP and A2A servers.

The container backend stays for institution deployments and CI (C31), is never required, and is never
auto-selected on a user install. `Makefile` targets and Compose files are development tooling and
stay. The assumptions in C32 leave the user path.

---

## 11. Institutions as outside services

**Proposed (C33, O4).** An institution is an outside service. It picks its own auth and may run
Keycloak as its own OAuth server. An institution that wants to serve many people from Astral's agents
runs Deep as a resource server. It validates bearer tokens from any issuer it is configured for,
publishes protected resource metadata (RFC 9728), checks that a token's audience is itself, never
passes the token to another API and partitions data by the token's subject within its issuer.

**Today** `backend/orchestrator/mcp_authz.py` is already an MCP-only resource server with protected
resource metadata and a `resource_metadata` challenge. The recipe widens that pattern. It is a
deployment recipe, generic to any issuer, and a user never installs it.

This replaces 105's phase 7 institution profile with its fresh Keycloak attestation, its PHI approval
descriptor and its Keycloak move runbook. From the owner's side an institution is a connection, as in
section 7.8. The institution revokes only its own tokens. Question 5 asks whether the recipe belongs
in this spec.

---

## 12. What stays centralized

The delta against [105 section 15](../105-decentralized-personal-mesh/spec.md).

| 105 item | Change |
|---|---|
| 1, Identity proof | Removed. No identity server is needed |
| 2 to 4, Relay, mailbox, push gateway | Unchanged. They admit by mesh key with rate limits and quotas (C7). An operator may add a platform check |
| 5, App distribution and signing | Wider. The Mac bundle and its nested binaries are signed and notarized |
| 6, Model endpoints | Unchanged. The owner approves each for PHI by name |
| 11, Names and certificates | Wider. Every outside server and authorization server is reached by DNS and TLS |
| 13, Institution nodes | Becomes an outside service with its own auth (section 11) |
| 15, Third-party tools and agents | Wider. Each connection adds its server and its authorization server |

| # | New item | Why it cannot be decentralized | What it sees | Without it |
|---|---|---|---|---|
| N1 | Client metadata document and app-link association files on the project website (C16) | A server fetches the document by URL and the OS checks an app link against the site | Requests for the files | Dynamic registration or a typed client id. HTTPS redirects fail |
| N2 | Each connection's authorization server | The service decides who may use it | Sign-ins and token requests for that service | That connection is unavailable |
| N3 | OS sandbox primitives | Seatbelt, Landlock and AppContainer belong to the OS vendors | Nothing | Untrusted code is refused |
| N4 | Platform attestation vendors, only if an operator opts in | Only Apple and Google vouch for their hardware roots | Attestation requests | The operator's check is off and quotas still apply |

---

## 13. Rules this changes

This spec proposes the wording and does not edit `AGENTS.md`. The changes need the owner's sign-off
because they contradict current text.

| Where | Says today | Proposed |
|---|---|---|
| `AGENTS.md`, security | "Preserve Keycloak-only user IAM, RFC 8693 attenuated delegation, owner isolation, tool permission/policy gates, PHI gates, egress validation, confirmation gates, and hash-chained audit provenance." | "Preserve mesh-key identity, ticket-based attenuated delegation, owner isolation keyed on the mesh id, tool permission and policy gates, PHI gates, egress validation, confirmation gates, and hash-chained audit provenance. Outside services authenticate through their own adapters." |
| `AGENTS.md`, credentials | "LLM credentials are configured in-product and encrypted. Do not reintroduce environment-backed provider keys, type user keys, or perform the user's Keycloak/provider sign-in." | "Every outside credential is entered in a native secure field and kept in the device keystore, per device, never synced. Do not reintroduce environment-backed provider keys, type user keys, or perform the owner's sign-in to any service." |
| `AGENTS.md`, `ASTRAL_ENV` | "Production configuration must fail closed, missing/placeholder secrets and mock auth must retain the documented exit-78 posture." | "Production configuration must fail closed. Missing or placeholder secrets retain the exit-78 posture. Mock auth no longer exists. A seeded test mesh with software keys is accepted only when `ASTRAL_ENV=development`." |
| `AGENTS.md`, egress | "Backend access to user-controlled URLs must use `backend/shared/external_http.py`" | Adds "A device uses the kernel's equivalent for the same fetches." |
| `AGENTS.md`, Apple sign-in | "Apple sign-in uses real PKCE and must be completed by the user, without a development-token bypass." | "Astral has no sign-in. Sign-in to an outside service uses the system browser and is completed by the owner. Test builds use the seeded test mesh and fixture servers." |
| `AGENTS.md`, build and verification | `make up`, `make sync` and `docker exec astraldeep ...` as the lifecycle | Adds "These are development and CI commands. A user install needs no Docker. A CI job installs and runs the node with no Docker on the path." |
| 105 section 17, security row | "Keycloak remains the only source of user identity, proven at enrollment ... RFC 8693 remains at the edge." | Replaced by the first row above |

---

## 14. Phases

A phase is finished when macOS and iOS pass, as in 105 C9. Android and Windows follow as thin clients
of a node until their own port. Criteria marked *(live)* need devices or a live server and all others
are automated tests. A phase is finished only when both kinds are shown. Every phase starts after
spec 105 merges.

| Phase | Work | Exit criteria |
|---|---|---|
| 0. Spikes | Seatbelt `enforce` runs a bundled Python tool whose only route is a loopback proxy. Test the Virtualization.framework fallback, set the minimum Landlock version, read MXC. Build the Python bundle, measure it, sign and notarize it in the Mac app, test whether the App Sandboxed build can host it. Run the MCP adapter against a fixture OAuth server and test the HTTPS callback on macOS and iOS. Check the A2A reader against a card with each scheme. Choose who hosts the metadata document (question 6) | A written result for each item with the call it supports or reverses. The Seatbelt spike refuses a write outside the workspace and any connection except to the proxy. The HTTPS callback returns to the app on a real Mac and iPhone *(live)* |
| 1. Identity cut | Roster contract, vectors and reference code at the new version. Kernel local mesh creation, with the nonce, `complete_mesh_creation(id_token)` and `reattest` dropped. In Deep, Plane and Vessel the owner id is the mesh id and a node authenticates by device key and ticket. The seeded test mesh. The pairing-and-signing library for Android and Windows | A Mac and an iPhone each create a mesh and pair with the network blocked *(live)*. A request with only a bearer JWT is refused. A production boot with a placeholder secret exits 78 and `ASTRAL_ENV` unset means production. No test uses mock auth. An Android or Windows thin client pairs with a node and signs a request |
| 2. Connections with MCP over HTTP | The connection event family, its contract and the per-device state events. The keystore layer on Apple, then Android and Windows. Adapter 1 on the kernel and the node, on the stateless revision. First-connect card, sign-in, step-up, refresh, failure states. Fixture OAuth and MCP servers | Against the fixture: discovery, required `S256`, `resource`, the `iss` check, step-up, refresh, a refused refresh, the dynamic registration fallback and a refused token passthrough all pass. A changed tool definition is refused until approved again. No token appears in an event, a log line or a model context. The UK example works end to end: add on the Mac, sign in on the iPhone, let a token expire, sign in again *(live)* |
| 3. A2A, stdio, key connections | The A2A adapter with schemes, extended card, signed card with pinned signer, and `AUTH_REQUIRED`. The stdio client on the existing macOS Seatbelt path, never on iOS. Key connections for HTTP APIs and models in native secure fields | A fixture A2A agent signs a device in for each supported scheme. A stdio server runs under enforce mode and reaches no network except through the proxy. A pasted key stays on its device and a second device shows `not signed in` |
| 4. Docker-free node, after Plane on SQLite is enabled in Deep | The bundle, supervision, the executor interface, the macOS and Linux backends, the egress proxy, `astralvessel up`, `check` and `down` without `docker compose`, the conformance suite, the no-Docker CI job, the C32 removals | `astralvessel up` runs on a Mac and a Linux host with no Docker on the path *(live)*. Every backend passes the conformance suite. The no-Docker CI job finishes inside 30 minutes. Untrusted code is refused where no enforcing backend exists |
| 5. PHI on the device | The detector rules ported with shared vectors, the on-device model where hardware allows, the PHI destination entry, the warning card, the flow input, the PHI group following the owner's devices | The same vectors pass in Python and in the kernel. A failed or missing detector labels content PHI. PHI to a model endpoint is refused until the owner approves it by name. An iPhone with no node labels and gates its own content *(live)* |
| 6. Sharing | Contacts, the comparison code, `view` grants, revocation with key rotation, the second sync session kind. Kind `contribute` only if question 3 says yes | Two meshes exchange a `view` grant and the contact reads the item live *(live)*. A contact's new device receives the item and a removed one does not. After revocation the contact cannot read new events. A PHI item is refused for a contact that is not a PHI destination. A contact's event is labeled untrusted |
| 7. Keycloak removal and the institution recipe | Delete the modules in 6.4 and the Keycloak parts of the clients. Write and qualify the resource-server recipe (C33) | A search for Keycloak in non-test product code finds nothing. The recipe accepts a fixture issuer's token, refuses another issuer's, publishes protected resource metadata, checks audience and partitions data by token subject |

---

## 15. Risks, and what gets weaker

| Risk | Why | Answer |
|---|---|---|
| No recovery without the recovery key | No account or server holds it | Said at setup. The 105 backup file helps only if the owner keeps one |
| No verified real names | Names are self-asserted (C6) | Comparison codes between two people |
| No abuse identity for shared services | A mesh is free to create | Quotas, rate limits and an optional platform check. A determined abuser can still make many meshes |
| An institution can revoke only its own tokens | Astral holds no account to disable | The institution's server enforces its own terms (section 11) |
| The PHI gate protects the owner and nobody else | The owner approves destinations | Section 8.4, said plainly. Question 4 |
| An OS sandbox shares the host kernel | It is no VM | Phase 0 tests a VM fallback. Untrusted code never runs without an enforcing backend |
| `sandbox-exec` is deprecated | Apple may remove it | Seatbelt stays behind one interface. Question 7 |
| Sign-in friction grows with devices times services | Each device signs in to each service (O5) | Accepted as the cost of O5. One tap and one login page per device |
| Dynamic registration is deprecated | The MCP revision prefers metadata documents | Both stay, and the website hosts the document (C16) |
| The phone cannot run stdio servers | iOS has no subprocesses | The phone uses remote HTTP and routes code to a Mac or node |
| Unsigned A2A cards cannot register | 105 section 9.5 requires a signature and A2A makes it optional | Kept as 105 wrote it. Most public A2A agents would be refused. Question 12 |
| Redirect hijack on a shared machine | A metadata document cannot stop another local program using the port | The first-connect card shows the redirect host |
| Removing a device does not cancel its tokens | They sit in that device's keystore | The owner revokes at the service (105 section 7.5) |

---

## 16. Questions for the owner

1. **Android and Windows.** Are they in scope here, or paused until their kernel port? The design
   gives them a pairing-and-signing library meanwhile (C4).
2. **Pasted API keys.** May they sync, encrypted to each device, across the mesh? C15 says no for the
   first version, so each device takes its own paste.
3. **Contribute sharing.** Is `contribute` wanted in this spec, or only `view` (C23)?
4. **PHI for research.** Is an owner-decided PHI destination acceptable for UK or UofL research use
   (C18, C19)? Their compliance offices may say no.
5. **The institution recipe.** Should the resource-server recipe live in this spec or its own (C33)?
6. **The client metadata document.** Who hosts it, and under which domain (C16)?
7. **The Mac sandbox default.** Which backend is the Mac default if Apple removes `sandbox-exec` (C29)?
8. **The model endpoint.** Is the LLM Factory endpoint reachable with a per-device key?
9. **Platform checks.** May a shared blind service require one (C7)?
10. **The rule changes in section 13.**
11. **Callers that are not mesh devices.** Is an owner-minted narrow ticket the right way for another
    agent or program to call a node (6.4)?
12. **Unsigned A2A cards.** 105 refuses an agent whose card is unsigned, and A2A makes signing
    optional. Keep the refusal, or allow an unsigned card pinned by its hash and origin with a warning?
13. **The PHI key group.** Should every device of the owner hold PHI by default, with exclusion by
    choice (8.3)?
