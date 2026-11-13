# Spec 106 research

**Date:** 2026-11-13. This is the evidence companion to [spec.md](spec.md).
Code references describe the trees as read on that date on branch `feat/105-personal-mesh`
(AstralVessel on `feat/105-vessel-base`, AstralKernel on `codex/105-kernel-integration`).

## A. What spec 105 says

Reference forms in this section: `spec:N` is line N of
`specs/105-decentralized-personal-mesh/spec.md`. `plan:`, `tasks:`, `rc:`, `lih:` and `it:` are that
folder's `plan.md`, `tasks.md`, `runtime-composition-plan.md`, `local-integration-handoff.md` and
`issue-triage.md`. A bare name such as `roster.md` is in its `contracts/` folder. `spec:106` means
line 106 of spec 105, not this spec.

### Identity and Keycloak

- **A1.** Idea 3 says Keycloak proves who a person is when a device is enrolled, and is not asked
  again for each request. Ref: `spec:33-34`.
- **A2.** Owner question O2 keeps the Keycloak instance at `iam.ai.uky.edu` for now. The spec says
  the design must not depend on where it lives, so the issuer must be movable. Ref: `spec:87`.
- **A3.** Rejected alternative 105-C6 says RFC 8693 needs a server round trip per hop. The spec
  gives this as rationale only. Ref: `spec:102`.
- **A4.** The topology draws Keycloak as the only box outside the mesh that is not a relay or
  mailbox. Its role is to prove identity when a device is enrolled. Ref: `spec:147`, `spec:150-151`.
- **A5.** Section 7.3, "What Keycloak does now," is the core dependency. Keycloak proves identity at
  enrollment and again on a slow schedule. It is not consulted per request. Ref: `spec:337-340`.
- **A6.** Enrollment uses PKCE with the nonce set to a hash of the device identity key and the mesh
  id. The ID token is stored in the roster as a statement signed by Keycloak. Any peer checks it
  offline against the issuer's published keys. Ref: `spec:342-345`.
- **A7.** The mesh principal is the mesh id plus one or more attested `(issuer, subject)` pairs.
  Nothing is keyed on a bare `sub`. Ref: `spec:346-347`.
- **A8.** Re-attestation runs every 30 days when the issuer is reachable, which the spec calls a
  starting value. It is silent while the Keycloak session is alive. Otherwise the owner signs in. A
  device that cannot reach the issuer keeps working.
- **A9.** An institution node may demand fresh attestation and refuse otherwise. Ref:
  `spec:348-351`.
- **A10.** Keycloak 24 and later omit the nonce from refresh-token ID tokens, so re-attestation is a
  new authorization request. The spec says "Phase 0 confirms this against the deployed version."
  Ref: `spec:352-354`, `plan:111-112`.
- **A11.** A device added while Keycloak is unreachable is marked unattested. It gets a smaller
  allowance and no access to institution nodes until it attests. Ref: `spec:355-356`.
- **A12.** Moving the realm adds an accepted-issuer entry, approved on an existing device. Devices
  re-attest against the new issuer. The spec says continuity rests on the mesh's own keys and keeps
  the rule that Keycloak is the only source of user identity. Ref: `spec:358-363`.
- **A13.** Section 8.1 describes the paper's per-call token exchange as one Keycloak round trip per
  WebSocket tool dispatch. That makes the identity server a per-request dependency, which this
  design removes. Ref: `spec:418-421`, `spec:425`.
- **A14.** Third-party MCP OAuth tokens are held in device keystores. RFC 8693 remains for an
  institution's Keycloak-protected resources and stops being the core of delegation. Ref:
  `spec:445-447`.
- **A15.** The protocol table's OAuth 2.1 row covers sign-in at Keycloak and access to third-party
  MCP servers. Its RFC 8693 row covers an institution's Keycloak as an edge adapter.
- **A16.** The spec says no MCP or A2A profile exists for authorization between devices that share
  no authorization server. Ref: `spec:864-867`, `spec:870-871`.
- **A17.** Section 15, item 1, lists the identity proof as centralized. Its "without it" column says
  enrolled devices keep working and a new device can join unattested with reduced rights. This
  conflicts with `roster.md:156`, see E2. Ref: `spec:886`.
- **A18.** AGENTS.md's security row reads "Keycloak-only user IAM and RFC 8693 attenuated
  delegation." The proposal is that Keycloak remains the only source of user identity, proven at
  enrollment, and that RFC 8693 remains at the edge. Ref: `spec:945-946`.
- **A19.** Section 18 keeps the decentralization draft's `(issuer, subject)` identity. The
  stack-integration draft says nothing in the wider stack may depend on Keycloak. The spec says this
  design is compatible with both. Ref: `spec:960`, `spec:963-964`.
- **A20.** Section 20 names the paper's "delegation by token exchange at Keycloak for each call" as
  the item being replaced. Ref: `spec:992`.
- **A21.** Question 3 asks whether a mesh with no identity provider at all is in or out. The spec
  notes that a purely personal mode would conflict with the Keycloak-only rule. The question is
  open. Ref: `spec:1013-1016`.

### Roster and contracts

- **A22.** The roster defines attestation as a statement that can be verified offline. Its vectors
  list `attestation.json`. Ref: `roster.md:3`, `roster.md:5`.
- **A23.** The type table's `issuer` entry adds or removes accepted issuers. Genesis carries
  `issuers`, with at least one accepted issuer. Ref: `roster.md:60`, `roster.md:71`.
- **A24.** An issuer record has the shape `{"iss", "clients", "keys": [JWK]}`. It is how a mesh
  learns which Keycloak realm to trust. Genesis from an app build lists `astral-desktop`,
  `astral-mobile` and `astral-watch`.
- **A25.** Any non-Keycloak verifier would need a different record shape. Ref: `roster.md:72`.
- **A26.** The `attest` entry is used by the creating device right after genesis, once its Keycloak
  sign-in has returned. The remove rule is `roster.unknown_issuer`. Ref: `roster.md:75`,
  `roster.md:97`.
- **A27.** For accepted issuers, removal beats a concurrent add. The newest add, ordered by `(clock,
  id)`, supplies the keys. Ref: `roster.md:122`.
- **A28.** The roster state fields `unattested`, `stale_attestation`, `principal` and `issuers` all
  derive from the identity server. Ref: `roster.md:127`.
- **A29.** Section 6, identity attestation, is Keycloak-specific. The nonce is
  `hex(sha256(identity_public_key || mesh_id_bytes))`, 64 hex digits. Ref: `roster.md:133-137`.
- **A30.** The attestation fields are `kind, device, mesh, issuer, subject, id_token, sig`. The
  `id_token` is the compact JWS as issued. Ref: `roster.md:139`.
- **A31.** There are eight verification steps. Step 6 allows only RS256 or ES256 and refuses `none`.
  Step 7 checks `iss`, `azp` or `aud` against the issuer record's `clients`, plus `sub`, `nonce`,
  `iat` and `exp`.
- **A32.** Step 8 judges the token at the entry's signed time with a 300 second skew, not at the
  verifier's clock. Ref: `roster.md:143-150`.
- **A33.** An attestation is stale 30 days after `iat`. Staleness never changes whether an entry is
  valid. Re-attestation is an `attest` entry, not a refresh. Ref: `roster.md:152`.
- **A34.** The principal is the mesh id plus `(issuer, subject)` pairs. Token claims are visible to
  every member, so the mailbox must not receive them in the clear. Ref: `roster.md:154`,
  `roster.md:158`.
- **A35.** Unattested devices are members. Policy gives them a smaller allowance, and institution
  nodes refuse them until they attest. Creating a mesh needs one attestation. Ref: `roster.md:156`.
  No policy contract carries the smaller allowance, see E5.
- **A36.** The recovery key cannot be rotated. The notes list this beside the attestation text, but
  it is not identity-server related. Ref: `roster.md:196-198`.
- **A37.** Pairing (section 8 and 8.1): the new device signs in to Keycloak with the section 6 nonce
  rule, and pairing cannot complete without it. The `add` entry carries the attestation, and null is
  allowed. Ref: `roster.md:171`, `roster.md:173`, `roster.md:182`.
- **A38.** Pairing and attestation failures use the error codes `attestation.*` and `pairing.*`.
  Ref: `roster.md:190`.
- **A39.** The event context `astral.attestation/v1` covers the identity attestation (device
  identity key) and PHI approval (institution key). Its replacement is needed if the identity
  attestation format changes. Ref: `event.md:57`.
- **A40.** The context `astral.pairing/v1` signs invites and requests. Ref: `event.md:58`.
- **A41.** The event families `auth.grant`, `auth.revoke`, `auth.narrow`, `auth.policy`,
  `auth.consent`, `auth.receipt` and `auth.transfer` name no identity provider. Ref:
  `event.md:164-168`.
- **A42.** A grep found no Keycloak reference in `keys.md`, `ticket.md`, `labels.md`, `sync.md`,
  `mailbox.md`, `tools.md`, `requests.md`, `surface.md`, `runs.md` or `learning.md`. Epoch and
  credential keys are device-keyed.
- **A43.** The ticket root key is the device's Ed25519 ops key. Ref: `ticket.md:40`.
- **A44.** The test issuer keys are an ES256 scalar `issuer/es256` and a fixed 2048-bit RSA key in
  `reference/scenarios.py`. The notes say these would become fakes for an identity source that no
  longer exists or is replaced. Ref: `contracts/README.md:59`.
- **A45.** The identity nonce is lowercase hex of `sha256(identity key || mesh id bytes)`. It is
  listed as an addition to the fixed choices. Ref: `contracts/README.md:44`.
- **A46.** The LETS input has no attestation or identity-state field. The policy text says "Whether
  a device is fresh or solo depends on the device's own clock and contact. It is not part of the
  input." Ref: `policy.md:81`, `policy.md:83`.

### Tasks, plan and status

- **A47.** Owner answers: the owner signs in to Keycloak when asked (tasks:16), and a mesh needs one
  Keycloak attestation to be created (tasks:26), which the notes take as the default answer. Ref:
  `tasks:14-18`, `tasks:26`.
- **A48.** Checks against real systems dated 2026-11-12: a device can exchange an authorization code
  itself. `astral-desktop` and `astral-mobile` are public clients. The realm advertises RS256, ES256
  and EdDSA. Ref: `tasks:36-37`.
- **A49.** The Kernel FFI methods in the Mesh area are the Keycloak touchpoints:
  `begin_mesh_creation() -> AttestationRequest` ("the OIDC nonce to send"),
  `complete_mesh_creation(id_token) -> RecoveryKit`, and `reattest(id_token)`. Ref: `tasks:134`.
- **A50.** Deep lane N1 covers `auth.py`, `delegation.py`, `mesh_*.py` and `session_store.py` in the
  AstralDeep orchestrator. Its work is issuer and audience checks on all three verification paths,
  `(issuer, subject)` owner ids, and Ed25519 local delegation. Ref: `tasks:160`.
- **A51.** Projection lane A1 covers enrollment sign-in with a bound nonce. Ref: `tasks:162`.
- **A52.** Wave 3 holds the institution profile and "the Keycloak move runbook." Ref: `tasks:188`,
  `plan:331-332`.
- **A53.** Plan phase 1, Deep: enforce token audience and issuer (`auth.py:303-310`), key owners on
  `(issuer, subject)`, and mint delegation locally with an asymmetric key in place of the
  per-dispatch Keycloak exchange (`orchestrator.py:22320`). Ref: `plan:143`.
- **A54.** Plan phase 1 also covers the Projection sign-in nonce bound to the device key
  (`plan:146`) and the Plane `(issuer, subject)` owner key, with no migration of old data
  (`plan:147`). Ref: `plan:146-147`.
- **A55.** Phase 1 exit criteria: with Keycloak blocked after sign-in, the session keeps working and
  tool calls still dispatch. A token issued for one node is refused by another. A second issuer's
  token carrying a first issuer's subject is refused. Ref: `plan:151-156`.
- **A56.** Phase 7 requires fresh identity attestation, and a runbook for adding the new issuer and
  re-attesting devices.
- **A57.** Its exit criteria: an institution node refuses a ticket with a stale identity
  attestation, and devices keep working through an issuer change. Ref: `plan:331-332`,
  `plan:341-343`.
- **A58.** CI testing uses a fake identity issuer. No live third-party service is part of the gate.
  Ref: `plan:350`.
- **A59.** The genuine owner or external inputs include interactive identity-provider sign-in and
  institution attestation authority. Live tests need the owner for Keycloak sign-in and Touch ID.
  Ref: `rc:122`, `lih:133`, `lih:137`.
- **A60.** Status as written: `plan.md` and `tasks.md` have no per-phase or per-task status marks.
  Status appears only in the handoff file. Ref: `plan:3`, `tasks:3`, `lih:17-25`, `lih:115-118`.
- **A61.** Enrollment attestation: contracts are written, and mesh creation with attestation is
  implemented in the kernel facade. The notes do not record a status for `reattest`, and they do not
  record the Keycloak move runbook as done. Ref: `lih:115`, `tasks:134`, `tasks:188`.
- **A62.** Institution nodes are specified only, in phase 7 and wave 3. The handoff shows no
  evidence of work on them. Ref: `plan:330-332`, `tasks:186-189`.
- **A63.** PHI approval: the roster types exist. Authority selection is open, and no build ships an
  authority. Device-side gate work in the kernel is partly uncommitted. Ref: `roster.md:61-62`,
  `roster.md:162-164`, `tasks:29-30`, `lih:116`.
- **A64.** Sharing between people has no phase, task or lane. The notes say it is not stated
  anywhere in the plan or tasks.

### Breakage if the identity server vanished

- **A65.** Stated: enrolled devices keep working (`spec:886-887`). Phase 1 keeps the session working
  with Keycloak blocked after sign-in (`plan:153`).
- **A66.** Stated: a new device can join unattested with reduced rights (`spec:886`,
  `spec:355-356`). Institution nodes refuse unattested devices (`roster.md:156`, `spec:356`,
  `plan:341`). Mesh creation needs an attestation (`roster.md:156`, `spec:1014-1016`).
- **A67.** Stated: re-attestation stops, and devices stay working (`spec:348-351`). Pairing cannot
  complete, because the new device must sign in (`roster.md:171`, `roster.md:182`).
- **A68.** A realm move is an accepted-issuer change, and no data is orphaned (`spec:358-360`).
- **A69.** Contradiction: `spec:886` says a new device can join unattested when Keycloak is absent.
  `roster.md:156` and `tasks:26` say creating a mesh needs one attestation. `spec:1014-1016` asks
  whether a personal mode with no identity provider is in or out.
- **A70.** Which rule holds is partly open, see E2.

### PHI

- **A71.** The confidentiality axis is `public, private, PHI, secret`. The source's declared class
  assigns the label, and content checks raise it. The spec names "a local privacy filter. Presidio
  on nodes." Ref: `spec:530-533`, `labels.md:12`.
- **A72.** A turn's running label is the join of the labels of everything loaded into its context.
  An event derived from others inherits the least trusted and most confidential of its sources. Ref:
  `spec:535-539`, `labels.md:22-24`.
- **A73.** The only place that names the device-side PHI check is issue-triage item 100 (Deep #318,
  phase 3): "Qualify local contextual privacy filtering for free-form tool text." The item says a
  local filter lets PHI checks run on a device without Presidio. Ref: `it:100`.
- **A74.** Flow rule: PHI-labeled content to a model endpoint is refused unless that endpoint is
  declared PHI-approved. The on-device model always qualifies. Ref: `spec:553`, `labels.md:43`.
- **A75.** The effect input takes `endpoint_phi_approved` as a boolean. No contract says how that
  boolean is established. Ref: `labels.md:36`.
- **A76.** PHI-labeled events in sync: bodies go only to members of the PHI key group (section 7.4).
  Other peers receive the header only. They can still verify the chain, and the header shows the
  content is held on approved devices. Ref: `spec:554`, `spec:213-214`.
- **A77.** The sync table sends a `phi` event to PHI group members and withholds it from others with
  reason `phi`. Withheld `secret` events other than `key.credential` are withheld from everyone.
- **A78.** The `hello` message carries a `phi` claim that the receiver verifies against the roster.
  Ref: `sync.md:64-72`, `sync.md:21`.
- **A79.** A `phi`-labeled event must use a content key introduced under a phi epoch key. The PHI
  group's epoch key encrypts the content keys of PHI scopes.
- **A80.** Phi epochs are not wrapped to the recovery key, so PHI history is not recoverable from
  the recovery key alone. Ref: `keys.md:67`, `keys.md:23`, `keys.md:27`, `keys.md:120`.
- **A81.** Section 7.4: the key of a PHI-labeled scope is wrapped only to the PHI group. A device
  outside the group cannot decrypt PHI even if it obtains the ciphertext. Ref: `spec:370-372`.
- **A82.** Who grants PHI approval (section 9.3): an approval for a device, node or model endpoint
  is a statement signed by an institution's key. The owner adds that institution as a PHI authority,
  with consent.
- **A83.** An owner cannot approve their own device for an institution's PHI.
- **A84.** Which institutions sign, and by what process, is for the owner (section 21). Ref:
  `spec:561-564`.
- **A85.** The roster types are `phi_authority`, which adds or removes an institution key, and
  `phi_approval`, which grants or revokes a subject. Ref: `roster.md:61-62`. A mesh with no
  authority has no PHI members, and no build ships an authority. Ref: `roster.md:76`.
- **A86.** A PHI approval object has the fields `kind` (phi), `institution` (P-256 key, base64url),
  `subject_kind` (device, node or endpoint), `subject`, `issued_s`, `not_after_s` and `sig`.
- **A87.** It is signed with the institution key under context `astral.attestation/v1`. Ref:
  `roster.md:162`.
- **A88.** Checks on a PHI approval: fields, subject kind, `not_after_s` above `issued_s`, the
  institution is an authority, the signature, and the window. Ref: `roster.md:164`.
- **A89.** PHI authority and approval follow the issuer merge rule, so revoke beats a concurrent
  grant. An approval counts only while its institution is an authority and `not_after_s` has not
  passed.
- **A90.** The revoke action is `{"action": "revoke", "subject": string}`. Ref: `roster.md:123`,
  `roster.md:62`.
- **A91.** Device versus node: a pocket peer runs PHI-labeled work only if the device is declared
  approved. A desktop peer is the same. A node does so if it is declared approved. Ref: `spec:166`.
- **A92.** The AstralDeep node row says it hosts "the PHI checks that need large models," along with
  the heavy first-party agents as MCP servers and a mailbox and relay. Ref: `spec:928`. Other
  large-model text covers on-device use for other jobs. Ref: `spec:980`, `spec:544`.
- **A93.** An institution's node never trusts a device's gates. It verifies the ticket and runs its
  own. Gates run on the proposing device, and again on the executor when the effect is remote.
- **A94.** Steps 4 and 5 are default-off on the server today and default-on in the kernel. Ref:
  `spec:442-443`, `spec:568-580`, `labels.md:59-70`.
- **A95.** Institution nodes: "PHI stays on nodes that declare approval." Ref: `spec:88`. An
  institution node uses PostgreSQL, where Plane holds the events in new tables and keeps its
  existing tables as views. Ref: `spec:253-255`.
- **A96.** Section 15, row 13: PHI rules on institution nodes "require a known operator, an
  agreement and an audit trail." The sentence cites no source. Ref: `spec:898`.
- **A97.** Phase 7 names "PHI approval declared in its descriptor." No node-descriptor contract
  exists in `contracts/`. Ref: `plan:330-331`.
- **A98.** Phase exits: phase 2 says the body of a PHI-labeled event is not sent to a peer without
  PHI approval (`plan:192-193`). Phase 7 says an institution node refuses PHI-labeled work from an
  unapproved peer (`plan:341-342`).

### Sharing between people

- **A99.** Sharing between different people is a non-goal, "beyond an explicit grant. The mesh is
  one person's." Ref: `spec:66`. Owner decision O3 says sharing between people is by explicit grant.
  Ref: `spec:88`.
- **A100.** An institution's node and another person are never in the personal group. They receive
  individual chat keys by grant, re-wrapped to them, and can read only those chats. Ref:
  `spec:373-374`, `keys.md:35`.
- **A101.** The only grant shape in the contracts is `auth.grant` and `auth.revoke`, keyed by
  `(agent, tool, scope)`. They grant tool authority to an agent, not access to a person. Ref:
  `event.md:164-165`.
- **A102.** Every grant needs consent. An `auth.consent` event must name it, and the event must be
  signed by the presence key of a roster member. Ref: `event.md:199-200`, `policy.md:40`.
- **A103.** The roster holds only owner devices. Institution keys identify institutions, not people.
  The contracts define no person identity, no person grant event and no person revocation. Ref:
  `roster.md:1-25`, `roster.md:3`, `roster.md:61`, `roster.md:162`.
- **A104.** The notes list these gaps in spec 105: person identity, person enrollment,
  person-to-person key wrap, a person-scoped grant event, person revocation, and what a grantee's
  device may read or write. Ref: the references in A99 to A103.

### Third-party services and outside tools

- **A105.** Where a remote service understands only OAuth (a third-party MCP server), the device
  holds that service's OAuth tokens in its keystore and presents them as today. Ref: `spec:445-447`,
  `spec:866`.
- **A106.** Removing a device does not cancel its third-party tokens or model keys. Another device
  revokes the lost device's connector grants where the provider allows.
- **A107.** Custody moves from a server key to device keystores, and credentials sync as items
  sealed to each device. Ref: `spec:401-404`, `spec:948`.
- **A108.** Model provider API keys sync as `key.credential` events with one box per device. Ref:
  `keys.md:69-85`.
- **A109.** Other references: the research note keeps "explicitly approved third-party MCP OAuth
  separate" (`rc:75`). Connector credentials move to device keystores and sync as sealed items, not
  as rows encrypted with a server key (`it:84`, Deep #122).
- **A110.** Connector credentials in device keystores are tracked as Plane #122 (`plan:372`).
- **A111.** Tool-definition pinning: gate step 2 checks that the tool definition matches the hash
  pinned at approval. A changed definition is a new tool and needs approval again. Ref: `spec:569`,
  `spec:584-585`, `labels.md:60`, `plan:315`.
- **A112.** The `hash` in `tools.md` is the `sha256:` of canonical JSON of `{name, description,
  input_schema, annotations}`. It is the same hash that pins an MCP tool definition. The grant name
  is `<name>@<first 8 hex digits of the hash>`. Ref: `tools.md:24`, `tools.md:32`, `tools.md:49`.
- **A113.** Every call to a peer tool still needs a consented grant, must pass the gates of
  `labels.md`, and must carry a ticket. Ref: `tools.md:51`.
- **A114.** The ticket action carries `tool_def`, the hash pinned at approval, and the call block
  pins the action digest, which includes `tool_def`. Ref: `ticket.md:32`, `ticket.md:35-36`,
  `ticket.md:90`.
- **A115.** An agent card must be signed to be registered. The notes say the current code does not
  check card signatures. Ref: `spec:586`. Section 14 says agent cards must be signed and provenance
  labels travel with each hop. Ref: `spec:865`.
- **A116.** Phase 6 requires signed A2A cards at registration. Ref: `plan:302`.
- **A117.** The integrity axis marks as untrusted anything the owner did not write, including remote
  MCP output and other agents. A tool cannot label its own output. Ref: `spec:532`.
- **A118.** Anything sent to a remote tool exposes its labels to the executor, which may refuse it.
  Ref: `spec:556`.
- **A119.** An A2UI surface from an outside agent is translated into primitives on arrival and
  treated as untrusted. Ref: `spec:845-847`. HTML from an MCP server is shown only inside the
  existing sandboxed artifact view, on clients that have one. Ref: `spec:848-849`.
- **A120.** Provenance: the kernel records `model`, `tool` or `host`. A document that a model
  produced and that claims `host` is refused with `surface.false_provenance`. Ref: `spec:838`,
  `surface.md:27`, `surface.md:31`.
- **A121.** Between peers, MCP runs over the mesh transport with the ticket in request metadata.
  Ref: `spec:864`. The MCP client sits in the kernel engine (`astral-engine`). Ref: `plan:207`,
  `tasks:106`.
- **A122.** Outward reads (fetch and search) are flows judged by the rules of section 9.3, and the
  rule of two covers them. Ref: `spec:430-431`, `spec:484`, `spec:551`.
- **A123.** The model supplies handles, and the host resolves them, which closes the exfiltration
  path through rendered links. Ref: `spec:836`.
- **A124.** Labels `outside_effect` and `outward_read` carry consent reasons. The strict validator
  rejects links and images in model-written text. Ref: `labels.md:36`, `labels.md:45-47`,
  `tasks:88-91`.
- **A125.** Phase R notes call for routing remote requests through deterministic egress and redirect
  controls, and for egress validation. Status items list "no redirects to model or MCP servers" and
  "egress validation." Ref: `rc:72`, `rc:75-76`, `lih:116`.
- **A126.** A remote MCP server or A2A agent is someone else's service. The spec's row asks what is
  sent to it, and says the fallback is that the tool is unavailable. Ref: `spec:900`. The spec says
  not to run an open skill registry. Ref: `spec:699`.
- **A127.** Learned items may name only tools already granted. Ref: `spec:741`.

### Institutions and the node

- **A128.** The node is "an optional always-on peer with no screen: today's orchestrator,
  repackaged." The topology lists it as "optional, always on," with the orchestrator and
  AstralPlane. Ref: `spec:123`, `spec:142-143`.
- **A129.** Section 16.2, AstralDeep row: "a single-owner profile for personal use and a multi-owner
  profile for institutions." Ref: `spec:928`.
- **A130.** Multi-owner hosting is not kept working as a second mode between phases 2 and 7. It
  returns as the institution profile. Ref: `spec:106`.
- **A131.** Question 11 asks whether a multi-user server must keep working between the personal node
  and the institution profile. The spec says that if yes, the node keeps a qualified multi-owner
  mode throughout and every phase costs more. Ref: `spec:1026-1028`.
- **A132.** Phase 7 work: several owners partitioned by owner, PHI approval declared in the
  descriptor, fresh identity attestation required, and the node's own gates run on every ticket. The
  phase depends on a host, the UofL VM. Ref: `plan:330-332`, `plan:323`.
- **A133.** The institution profile sits in wave 3. Lane N1 covers only the single-owner node
  profile. Ref: `tasks:186-189`, `tasks:160`.
- **A134.** The node is the relay and mailbox for its own mesh. It runs the heavy first-party
  agents, with shell, files and browser inside its sandbox. Ref: `spec:288-289`, `spec:164-165`.
- **A135.** Institution-node PHI rules, operator agreements and onboarding are not specified beyond
  the lines above. See E17.

### Executor, sandbox and containers

- **A136.** The executor checks an owner-signed ticket before a tool runs. It may be the same
  device, another of the owner's devices, or an institution's node. Ref: `spec:441-442`. It is also
  the lease holder for a run. Ref: `spec:709-716`.
- **A137.** The verifier steps are in `ticket.md:64-78`.
- **A138.** A desktop peer "can run a full agent loop, local tools, a sandbox and larger local
  models." Ref: `spec:120`. Shell, files, browser and computer use are "No" on a pocket peer, "Yes"
  on a desktop, and "In its sandbox" on a node. Ref: `spec:164`.
- **A139.** "Do not ship with the sandbox off and prompts disabled." The spec presents this as a
  lesson from others' incidents. Ref: `spec:699`.
- **A140.** The only use of "containers" in spec.md is in the AstralVessel row.
- **A141.** Its current state is "a separate Python agent server with its own loop, sandbox and
  egress." After the change, "its sandbox and egress containers become the executor." Ref:
  `spec:931`, `spec:966`.
- **A142.** Plan phase 1 says Vessel's "sandbox and egress become the node's executor," and its
  conformance suite runs against the node. Ref: `plan:144`.
- **A143.** Plan phase 1 says "the sandbox stops being required here." That sandbox is the hosted
  sandbox.ai.uky.edu, not a container. Ref: `plan:16`, `plan:134`, `plan:151`, `plan:158`. Lane V
  keeps its sandbox and egress as the ticket-checking executor. Ref: `tasks:182`.
- **A144.** AstralVessel is the personal install on Plane SQLite. Ref: `tasks:22`, `spec:248`.
- **A145.** Tasks item 115 builds a Linux wheel for the node inside Docker, using
  `scripts/build-python.sh`. It is the only build reference to Docker in the design. Ref:
  `tasks:115`.
- **A146.** Lane rules forbid running `make up`, `docker compose`, or anything that recreates the
  shared `astraldeep` container or touches the `astraldeep-postgres` database.
- **A147.** Backend tests run in a disposable container, with a throwaway `postgres:17-alpine` on
  its own network and `docker run --rm` of `astraldeep:latest` with no `.env`. Ref: `tasks:195-197`.
- **A148.** A Docker Rust compile result is invalid and must never count as a pass or a product
  failure. Task containers are used for backend qualification. These lines are not design text. Ref:
  `lih:55`, `lih:101`, `lih:110`, `lih:136`.
- **A149.** The notes say the production executor's runtime is not stated: Docker, Podman, a VM or a
  native process. Ref: see E19.

### Conventions a sibling spec copies

- **A150.** Spec header: `# Spec 105: <title>`, then one line with `**Date:** YYYY-MM-DD.
  **Status:** ...`, a `**Companions:**` line with links, and a `**Scope:**` line naming
  repositories. A rule follows the header. Ref: `spec:1-11`.
- **A151.** Section order: summary, goals and anti-goals, decisions (owner table O1 to O6, calls
  table 105-C1 to 105-C10), the picture, the event log, sync, identity and devices, authority
  without a server, flow labels and gates,
- **A152.** the turn engine, long-running work, voice, generative UI, protocols, what stays
  centralized, layers and repositories, rules this changes, departures from the reference documents,
  risks, the exam paper, and questions for the owner. Ref: the section headings of `spec.md`, which
  the notes did not number.
- **A153.** Open items are never called "TBD." They appear as questions in section 21, or as "a
  starting value" or "for the owner to tune." Owner decisions carry a date, such as "(owner,
- **A154.** 2026-11-12)." The phrase "Not stated" is not used in spec 105, and the notes say a
  sibling spec should decide whether to adopt it. Ref: `spec:95`, `spec:479`, `spec:1017`.
- **A155.** Plan entries use the bold labels `**Goal.**`, `**Depends on.**`, `**Work.**`,
  `**Tests.**` and `**Exit criteria.**`. Tasks use lane IDs such as `N1`, `A1` and `V`, and carry no
  status marks. Ref: `plan.md` and `tasks.md` headers.

## B. Identity and auth code today

Checkouts as read on 2026-11-13: AstralDeep, AstralProjection, AstralPlane, LETS and
AstralPrimitives on `feat/105-personal-mesh`. AstralVessel on `feat/105-vessel-base`. AstralKernel
on `codex/105-kernel-integration`. Ref: checkout table in the identity inventory.

### AstralDeep backend

- **B1.** `AstralDeep/backend/orchestrator/auth.py` is the BFF auth proxy and REST JWT verifier. Its
  `USE_MOCK_AUTH` branch at lines 25-28 logs that all tokens are accepted as test_user, and again at
  line 236. Key functions sit at 213, 309, 315, 586 and 616.
- **B2.** The form allowlist includes `code_verifier` at line 59. Ref:
  `AstralDeep/backend/orchestrator/auth.py:25-28`, `:59`, `:213`, `:233`, `:236`.
- **B3.** `AstralDeep/backend/shared/token_verify.py` has `verify_idp_token` (49), `jwks_url_for`
  (36, the Keycloak JWKS URL from the issuer), `check_idp_audience` (25) and `with_owner_identity`
  (40). It uses `jose` (line 9). Ref: `AstralDeep/backend/shared/token_verify.py`.
- **B4.** `AstralDeep/backend/shared/jwks_cache.py` is a TTL-cached Keycloak JWKS fetch. The env
  name `JWKS_STALE_GRACE_SECONDS` appears at line 42. Ref:
  `AstralDeep/backend/shared/jwks_cache.py:42`.
- **B5.** `AstralDeep/backend/shared/auth_clients.py` holds audience sets. `MCP_AUDIENCE =
  "astral-mcp"` is at line 12. A comment at line 3 says Keycloak tokens share `aud="account"`. Ref:
  `AstralDeep/backend/shared/auth_clients.py:3`, `:12`.
- **B6.** `AstralDeep/backend/orchestrator/web_auth.py` (1168 lines) is the server-side OIDC login.
  It has `_pkce_pair` (559), S256 authorize at 599-612, token exchange at 636-656, OIDC discovery at
  579, and a mock flag helper at 88. Ref: the same file, lines as listed.
- **B7.** `AstralDeep/backend/orchestrator/session_store.py` (1005 lines) is the Postgres session
  store. It encrypts tokens at rest and fails closed in production. It exits with code 78 at line
  150, after a refusal log at lines 144-149.
- **B8.** The only other hit is `scripts/tests/test_backend_web_gate.py:448`. Ref:
  `AstralDeep/backend/orchestrator/session_store.py:144-150`.
- **B9.** `AstralDeep/backend/orchestrator/native_session_custody.py` (450 lines) is opt-in native
  custody of a fresh Keycloak grant. Verifier validation is at 292-300. The client redirect map is
  at 37-38. Ref: the same file.
- **B10.** `AstralDeep/backend/orchestrator/device_login.py` (503 lines) is an RFC 8628 device grant
  broker for watch QR sign-in. It uses PKCE S256 at 278-287, an opaque encrypted poll handle at 330,
  and discovery at 178. Ref: the same file.
- **B11.** `AstralDeep/backend/orchestrator/offline_grant.py` (365 lines) is an encrypted
  offline-grant store. It holds a reference to the Keycloak session, with a 365-day cap.
  `mint_access_token` is at 288 and the refresh exchange at 311. Ref: the same file.
- **B12.** `AstralDeep/backend/orchestrator/ui_auth_renewal.py` (about 560 lines or more) handles
  subject-token capture and renewal. `capture_subject_token_resolver` is at 357-370, and `mock_auth`
  gating at 162-205.
- **B13.** `AstralDeep/backend/orchestrator/session_authority.py` has mock auth at 113 and 189.
- **B14.** `AstralDeep/backend/shared/node_identity.py` (338 lines) is the personal-node identity.
  It holds an Ed25519 node signing key. `normalize_issuer` is at 73, `accepted_issuers` at 77,
  `issuer_digest` at 88, and `NodeIdentity` at 121.
- **B15.** It derives the owner id from verified issuer claims.
- **B16.** `AstralDeep/backend/orchestrator/mcp_authz.py` (266 lines) is an MCP-only OAuth resource
  server.
- **B17.** Its protected resource metadata is at `/.well-known/oauth-protected-resource/mcp`
  (68-75), and its `WWW-Authenticate` header with `resource_metadata` is at 127.
- **B18.** `mcp_server_endpoint.py` at lines 526 and 663 serves the same path.
- **B19.** A2A agent cards advertise a `keycloak_oidc` OpenIdConnect security scheme. The code is in
  `AstralDeep/backend/orchestrator/a2a_orchestrator_executor.py` (466-509) and
  `AstralDeep/backend/shared/a2a_bridge.py` (119-153). Ref: those lines.
- **B20.** `AstralDeep/backend/shared/a2a_security.py` checks A2A bearer tokens, with a mock flag at
  49-58. Its header says it enforces RFC 8693 delegation scopes. Ref: the same file, lines 49-58.
- **B21.** `AstralDeep/backend/orchestrator/connection_guide.py:156` refuses mock auth outside
  development.
- **B22.** Identity env var names (values not read): `USE_MOCK_AUTH` (about 19 references),
  `KEYCLOAK_AUTHORITY` (17), `KEYCLOAK_CLIENT_ID` (13), `KEYCLOAK_CLIENT_SECRET` (4),
  `KEYCLOAK_URL`, `KEYCLOAK_TOKEN_URL`, `KEYCLOAK_REALM`,
- **B23.** `KEYCLOAK_DEVICE_CLIENTS`, `KEYCLOAK_ALLOWED_AZP`, `ASTRAL_ENV` (7),
  `JWKS_STALE_GRACE_SECONDS`, `AUTH_TOKEN_PROXY_RATE`, `AUTH_REVOCATION_RETRY_SECONDS`,
  `PUBLIC_BASE_URL`, `BACKEND_PUBLIC_URL`, `ASTRAL_RUNTIME_AUTHORITY_JSON`. Ref: the AstralDeep
  backend, names only.
- **B24.** Other env var names (values not read): `AGENT_SERVICE_CLIENT_SECRET` (delegation.py,
  about line 36), `WEB_SESSION_ENC_KEY`, `WEB_SESSION_SECRET`, `OFFLINE_GRANT_ENC_KEY`,
  `TXN_TOKEN_KEY`, `DELEGATION_CHILD_SIGNING_KEY`,
- **B25.** `DELEGATION_REQUIRED`, `AUDIT_HMAC_SECRET`, `CREDENTIAL_ENCRYPTION_KEY`,
  `MEMORY_HMAC_KEY`, `MAS_MESSAGE_KEY`, `AGENT_API_KEY`, `AGENT_KEY_PATH`,
  `AGENT_KEY_TRUSTED_HOSTS`. Ref: the AstralDeep backend, names only.
- **B26.** Relevant tests: `backend/tests/test_fail_closed_boot.py`, `test_env_compat.py`,
  `test_offline_grant_lifecycle.py`, `test_logout_revocation.py`,
  `test_session_issuer_binding_088.py`, `test_a2a_server_hardening.py`,
- **B27.** `test_real_exchange.py` and `test_web_callback_mesh_custody.py`. Ref:
  `AstralDeep/backend/tests/`.
- **B28.** Count: 39 non-test backend Python files mention Keycloak, and 26 of them are in
  `backend/orchestrator`. Ref: the identity inventory count.

### Native clients

- **B29.** Apple: `AstralProjection/apple-clients/AstralCore/Sources/AstralCore/Configuration.swift`
  (lines 8-63) holds the Keycloak authority, with a fallback of
  `https://iam.ai.uky.edu/realms/Astral` at line 14.
- **B30.** Client ids `astral-mobile`, `astral-desktop` and `astral-watch` are at 58-60.
- **B31.** The redirect `com.personalailabs.astraldeep:/oauth2redirect` is at 62-63.
- **B32.** Apple PKCE and session files: `Auth/PKCE.swift` has the verifier at line 8, the challenge
  at 14, `OIDCConfig` at 27 and the authorize URL at 51. `Auth/ServerSession.swift` is the session.
- **B33.** The watch refreshes with `watchClientId` at `AstralWatch/WatchModel.swift:676`, and
  `WatchVoiceREST.swift` sends the Keycloak bearer. 31 Apple files mention keycloak, oidc or pkce,
  including tests and UI fixtures.
- **B34.** Android: `AstralProjection/android-client/app/src/main/kotlin/.../app/auth/` has
  `OidcAuth.kt`, `OidcEndpoints.kt` and `ServerSession.kt`. It uses AppAuth 0.11.1
  (`gradle/libs.versions.toml` lines 12 and 44).
- **B35.** `app/build.gradle.kts` lines 128-220 pin the registered OIDC client and redirect, and
  fail the build if they change. `AppConfig.kt` is at 13-23. 20 files mention keycloak, oidc or
  pkce.
- **B36.** Windows: `AstralProjection/windows-client/astral_client/` has `auth.py`, the dedicated
  auth module, and `app.py`. In `app.py`, `_current_token` is at 3604-3614 and Keycloak and native
  logout and revoke are at 3913-3932.
- **B37.** `app.py:216` has a `dev-token` shortcut.
- **B38.** `rest.py` and `deployment.py` are also here. 20 files mention keycloak, oidc or pkce.

### AstralVessel

- **B39.** `AstralVessel/astralvessel/deployment.py` lists `KEYCLOAK_AUTHORITY`,
  `KEYCLOAK_CLIENT_ID` and `KEYCLOAK_ALLOWED_AZP` in its env allowlist (30-31). `issuer` is at 81
  and `accepted_issuers` at 85.
- **B40.** The owner's subject-derived id is `oidc.<sha256(issuer)[:16]>.<subject>` at line 99.
- **B41.** `AstralVessel/astralvessel/compose.py` (78-91) exports `KEYCLOAK_*` and
  `ASTRAL_ACCEPTED_ISSUERS` into the node container. `AstralVessel/astralvessel/operate.py`
  (142-147) prompts interactively for an OIDC issuer URL.
- **B42.** `AstralVessel/astralvessel/node.py` checks the issuer at 81-82 and sends the owner token
  in pairing requests at 107-134. It refuses a pairing without a current owner token at 111.
- **B43.** `astralvessel/conformance.py` (76-108) includes "pairing refuses a caller who is not the
  owner."
- **B44.** Twelve Vessel files mention keycloak or oidc, including docs, tests and
  `deploy/deployment.toml`. Vessel holds no identity code of its own. It passes identity through to
  the AstralDeep node.

### AstralKernel

- **B45.** `AstralKernel/crates/astral-mesh/src/attestation.rs:1` has a header saying identity
  attestation is "made from a Keycloak ID token, checked offline against issuer keys."
  `crates/astral-mesh/src/scenario_tests.rs` also mentions Keycloak or OIDC.
- **B46.** Other hits are in docs and the README.
- **B47.** The inventory found no JWT or JWKS code in the Rust tree (inferred from the search).

### Delegation (RFC 8693)

- **B48.** `AstralDeep/backend/orchestrator/delegation.py` (530 lines) is the RFC 8693
  token-exchange service. Its mock branch is at 39-41 and 69. `DelegationService` is configured for
  Keycloak at 45-47.
- **B49.** `exchange_token_for_agent` at line 48 calls Keycloak through aiohttp at 112.
- **B50.** `mint_child_delegation` at 410 builds depth-bounded child chains. The personal node signs
  locally with Ed25519, per the file header.
- **B51.** `AstralDeep/backend/orchestrator/chain_authority.py` (255 lines):
  `MachineTurnAuthority.derive` at 159 builds the consent-derived root authority. `ChainBudget` sets
  the hop, depth and time ceiling.
- **B52.** `AstralDeep/backend/orchestrator/mesh_admission.py` has `encode_delegation_payload` at
  540, `exchange_token_for_agent` at 556, the exchange result as `subject_token` at 610, and
  `permissions.is_tool_allowed` at 534.
- **B53.** `offline_grant.py` at 288 and 311 performs an unattended exchange of a stored refresh for
  a fresh access token. `a2a_orchestrator_executor.py` (221-236) takes the subject token from task
  state or the bearer header.
- **B54.** Subject-token plumbing: `AstralDeep/backend/orchestrator/api.py` (98-101 and 1854-2759),
  `work_api.py:118`, `terminal_api.py:90-146` and `framework_operation_authority.py:83`.
- **B55.** Scope derivation: `ToolPermissionManager` owns `is_tool_allowed`, `get_agent_scopes` and
  `set_agent_scopes`.
- **B56.** `is_tool_allowed` is called from `mesh_admission.py:534`, `tool_visibility.py:88`,
  `personalization/api.py:200` and `agents/cresco/mcp_tools.py:116`.
- **B57.** The inference is that the scope source of truth is `is_tool_allowed`, which matches the
  team memory note on empty-scope derivation (inferred).
- **B58.** `set_agent_scopes` is called from `verification/drivers/in_process.py` (365-490) and
  `scheduler/api.py:125`. The table name `agent_scopes` appears in
  `backend/seeds/013_per_tool_permissions.sql`, for reference only.
- **B59.** No explicit `act` claim builder was found. Actor lineage appears to be carried by
  `ChainBudget` and `mint_child_delegation` (inferred, not verified further).

### Mesh enrollment and attestation

- **B60.** `AstralDeep/backend/orchestrator/mesh_enrollment.py` (1323 lines) holds the invitation
  and membership store (`MeshEnrollmentStore` at 313). Its error taxonomy is at 57-112.
- **B61.** `public_invitation` is at 248, `public_member` at 271 and `parse_enrollment_payload` at
  1302.
- **B62.** It writes through AstralPlane in one transaction with audit. The notes call it
  substantial and not stubbed.
- **B63.** `AstralDeep/backend/orchestrator/mesh_admission.py` (819 lines): `verify_member_token` at
  122, with the member token audience check at 115. `MeshAdmission` is at 238.
- **B64.** The Keycloak RFC 8693 exchange is at 556, the mint at 644 and the nonce budget at 777.
- **B65.** Its header says it binds member possession to current Plane membership and
  server-custodied Keycloak authority.
- **B66.** `AstralDeep/backend/orchestrator/mesh_api.py` (608 lines) has these routes:
  `create_invitation` (228), `confirm_invitation` (281), `reject_invitation` (315), `list_members`
  (342), `revoke_member` (355), `rename_member` (370),
- **B67.** `member_self` (408), `redeem_enrollment` (418, unauthenticated one-time redemption),
  `member_nonce` (481), `member_session` (497), `invoke_member_tool` (532) and `member_presence`
  (571).
- **B68.** `AstralDeep/backend/orchestrator/node_api.py` (238 lines) serves the public descriptor
  and the node JWKS at `/.well-known/astral-node/jwks.json` (line 154). It offers
  owner-authenticated pairing with a one-time short-code lookup.
- **B69.** Presence and shared surface: `mesh_presence.py` (301 lines), `mesh_links.py` (40),
  `mesh_web.py` (38) and `projection_surfaces/mesh.py` (834), all in AstralDeep.
- **B70.** Assessment in the notes: invitation, confirm, redeem, nonce and member-session code is
  real and wired. The member token and admission paths both depend on Keycloak, at the exchange in
  line 556 and the member audience in line 115.
- **B71.** The spec term "unattested" has no code hit in the mesh path. Its only hits are in
  `backend/personalization/semantic_pilot/` (report.py 23-63, providers.py 28), where it means
  something else.
- **B72.** The inference is that the spec's unattested state is not implemented in Deep (inferred).
- **B73.** The Kernel crate `AstralKernel/crates/astral-mesh/src/` is about 7,500 lines across 17
  files, with no `todo!`, `unimplemented!` or stub hits in non-test files. `roster.rs` has 98 hits
  for roster, nonce, attest and pairing terms.
- **B74.** In `attestation.rs` (99 hits): `IssuerRecord` at 32 (issuer keys, default clients at 85),
  `nonce_for` at 98 (identity public key plus mesh id), `IdentityAttestation` at 105, `build` at
  153,
- **B75.** `verify_identity` at 263 (an offline check against issuer keys), and the PHI summary
  `build_phi` at 371 and `verify_phi` at 394.
- **B76.** `AstralKernel/crates/astral-mesh/src/pairing.rs` (114 hits): `PairingOffer` at 56,
  `create` at 65, `verify` at 109, `code()` at 142 (the short code), `answer_invite` at 153 and
  `PairingInvite` at 168.
- **B77.** `recovery.rs` (31): `recovery_writer_key` at 19, `check_recovery_key` at 23,
  `recover_epoch_keys` at 34, `recover_add_device` at 52, and backup seal, open, export and import
  at 108-211.
- **B78.** Kernel crypto: XChaCha20-Poly1305 at `AstralKernel/crates/astral-core/src/crypto.rs`
  (258-305), with wraps in `astral-core/src/wrap.rs`.
- **B79.** Other files include `device.rs`, `epochs.rs`, `freshness.rs`, `writer.rs`, `entry.rs`,
  `wire.rs`, `signer.rs`, `error.rs`, `testutil.rs`, `vector_tests.rs` and `scenario_tests.rs`.
- **B80.** Assessment in the notes: the cryptographic and data-structure layer looks complete, with
  vectors and scenario tests. Whether AstralDeep calls it, through a pyo3 binding or `astral-net`,
  is not verified.

### Outside services

- **B81.** `AstralDeep/backend/orchestrator/remote_mcp_connectors.py` (1059 lines) is the
  owner-scoped connector store and outbound MCP client. Validation is at `_validate_url` (48),
  `_validate_oauth_endpoint` (63) and `_validate_config` (78).
- **B82.** Connector list and save are at 164-233.
- **B83.** JSON-RPC over HTTP is `_mcp_request` at 416, with notify at 457, the SSE session
  `_SSESession` at 479, SSE parsing at 581 and the schema sanitizer at 593.
- **B84.** In the same file: `_initialize` at 765 sends protocolVersion `2025-11-25` (line 774).
  `tools/list` is at 790, `discover_tools` at 820, `call_tool` at 998 and `tools/call` at 1038.
  Bounded fixed-origin egress runs through `http_request` at 837.
- **B85.** Tokens in `remote_mcp_connectors.py` are handled as credential fields at lines 81-344 and
  encrypted through `orch.credential_manager` (line 158). The notes mark the last link as inferred
  from the call, not read.
- **B86.** `AstralDeep/backend/orchestrator/remote_mcp_pins.py` and `remote_mcp_security.py` handle
  tool-definition pinning and changed-tool flagging (`_flag_changed_tool` at 977).
  `remote_mcp_api.py` holds the routes and binds the OAuth browser at start.
- **B87.** No general stdio MCP client was found. Searches for `stdio_client` and
  `StdioServerParameters` return nothing in backend code outside tests. `stdio` appears only in
  `agent_generator.py` (276-322), as a generated agent's transport (inferred).
- **B88.** Remote MCP OAuth in `AstralDeep/backend/orchestrator/remote_mcp_oauth.py` (550 lines):
  protected resource metadata URLs at 90-99. `WWW-Authenticate` `resource_metadata` parsing at 28
  and 348-359, which must be the same host and path.
- **B89.** Authorization-server metadata candidates at 79-86 (RFC 8414 and OIDC).
- **B90.** Authorization servers come from PRM at 364.
- **B91.** The same file requires an advertised S256 method (178-181), sends `code_challenge` in the
  auth request (306) and `code_verifier` and `resource` in the token request (473).
- **B92.** Dynamic client registration is a POST to `registration_endpoint` at 395-404, with grant
  types `authorization_code` and `refresh_token` (400).
- **B93.** Refresh is at 508-511, with token updates stored at 528-541.
- **B94.** Connector record fields `access_token`, `refresh_token` and `oauth_client_secret` appear
  in `remote_mcp_connectors.py` at lines 81, 96, 259, 286, 315 and 344.
- **B95.** A2A client: `AstralDeep/backend/orchestrator/orchestrator.py` (5943-5944) fetches
  `/.well-known/agent-card.json` with aiohttp. Lines 6126-6136 and 6183-6192 use
  `a2a.client.A2ACardResolver` from the a2a-sdk.
- **B96.** `AstralDeep/backend/shared/base_agent.py` (753-790) serves each agent's card and is not a
  client.
- **B97.** The code publishes only its own `keycloak_oidc` scheme. No code reads an external agent's
  `securitySchemes` or `securityRequirements`. External A2A agents do not have their auth modeled
  (inferred from a grep with no reader hits).

### Per-user credentials and keys

- **B98.** `AstralDeep/backend/orchestrator/credential_manager.py` (391 lines) is a per-user,
  per-agent encrypted store on Postgres.
- **B99.** It uses Fernet for orchestrator-readable secrets and ECIES for agent-only secrets
  (`register` at 90, encrypt at 100, and 159, 229, 260).
- **B100.** Possible issue, unverified: in `credential_manager.py` (lines 68-86), the key comes from
  `CREDENTIAL_ENCRYPTION_KEY` if set. If not set, the code reads or generates a `.credential_key`
  file in the data directory (`data/`).
- **B101.** The notes did not confirm an ASTRAL_ENV gate on that fallback, and they flag it for
  review against the fail-closed rule.
- **B102.** `AstralDeep/backend/shared/crypto.py` implements ECIES (ECDH P-256, HKDF-SHA256,
  AES-256-GCM) with an `e2e:` prefix (lines 1-15, 87-129).
- **B103.** `AstralDeep/backend/shared/base_agent.py` (133-148) manages per-agent ECIES key files,
  including load, generate and predecessor keys.
- **B104.** `AstralDeep/backend/orchestrator/framework_credentials.py` (341 lines) mints durable
  owner bearer tokens for SDK, MCP and A2A callers. Only a hash and a prefix are stored.
- **B105.** `byo_authority.py` (138 lines) is the server-owned authority for personal BYO executors.
- **B106.** `user_agents.py` (117 owner_id hits) holds user agent records.
- **B107.** `external_identity_links.py` (370 lines) links ORCID through signed, short-lived
  assertions stored in user preferences. `agent_identity.py` (135 lines) projects verified external
  identity claims into the MCP envelope.
- **B108.** `notification_routing.py` (lines 22 and 55-100) uses Fernet with the same
  `CREDENTIAL_ENCRYPTION_KEY`.
- **B109.** LLM provider keys: `AstralDeep/backend/llm_config/user_store.py` uses Fernet, with
  `_resolve_fernet` at 103-104 and `CREDENTIAL_ENCRYPTION_KEY`.
- **B110.** Its records are `EncryptedLLMConfigRecord` from `astralplane.repositories.secrets` (line
  17), so the ciphertext lives in AstralPlane's secrets repository.
- **B111.** `llm_config/DECISIONS.md` (25-26) says custom configs are encrypted in an opaque
  credential column with a `decisions:v1:` prefix.
- **B112.** `AstralDeep/backend/orchestrator/sandbox.py` (line 16) lists `OPENAI_API_KEY`,
  `ANTHROPIC_API_KEY` and `SEARCH_API_KEY` as names to strip from sandboxes.
- **B113.** `clinical_extraction.py` (389) and `evidence_context.py` (1434) call
  `open_captured_user_key` for per-call user keys (inferred: decrypt on use).

### PHI gates and sharing

- **B114.** `AstralDeep/backend/personalization/phi_gate.py` (273 lines) is fail-closed PHI
  screening. `PHIGate` has `available` (111), `contains_phi` (114) and `contains_phi_for_sharing`
  (146).
- **B115.** It uses Presidio, and its warning log at line 90 names `presidio_unavailable` (inferred
  from the log text).
- **B116.** `AstralDeep/backend/shared/phi_redactor.py` has field patterns at 18, value patterns at
  59 and redaction at 142-217. It fails closed with `phi_redaction_unavailable` (195, 216).
- **B117.** `AstralDeep/backend/personalization/phi_policy.py` (72 lines) keeps per-user PHI
  protection off until the user turns it on.
- **B118.** Redaction and reminders are gated on that preference, which is read from AstralPlane.
- **B119.** PHI gate call sites: `work_submit.py` (406-410 and 517-521, failing closed with 503
  `assignment_phi_gate_unavailable`), `work_operations.py` (114), `hitl_confirmation.py` (995-1063),
  `agentic_creation.py` (351-358),
- **B120.** `supervisor.py` (72-89, output review, fail closed), `mcp_work_resources.py` (162-178),
  `remote_confirmation.py` (414-426) and `artifact_share.py` (screens at mint).
- **B121.** Mesh PHI in the kernel: `attestation.rs` `build_phi` and `verify_phi` (371 and 394)
  carry the institution-signed PHI approval.
- **B122.** `AstralDeep/backend/orchestrator/artifact_share.py` (351 lines) makes revocable snapshot
  links. Token SHA-256 hashing is at `hash_token` (169), with `ShareGrantStore` at 173 and
  `SharePHIRefusedError` at 165, behind the `FF_ARTIFACT_SHARING` flag.
- **B123.** It is wired in `orchestrator.py` (1452-1455) and in the `api.py` `/share` routes.
- **B124.** Mesh membership sharing in `mesh_enrollment.py` is per-owner membership, not
  per-artifact sharing. `computer_grants.py` and `framework_credentials.py` are the other
  sharing-adjacent files.
- **B125.** No general user-to-user sharing grant was found beyond these (inferred).

### Owner isolation

- **B126.** `owner_id` or `user_id` appears in about 150 to 160 non-test backend files.
- **B127.** The highest counts are `voice_sessions.py` (123), `mesh_enrollment.py` (121),
  `user_agents.py` (117), `agent_lifecycle.py` (112), `emergency_stop.py` (109), `history.py` (97),
  `evidence_context.py` (81), `lets_lifecycle.py` (65),
- **B128.** `terminal_sessions.py` (60), `remote_mcp_connectors.py` (59), `projects.py` (49) and
  `push_destinations.py` (46).
- **B129.** Direct `sub` reads are about 187 lines across 62 files in `orchestrator`, `shared` and
  `agents`.
- **B130.** The owner id comes from verified issuer claims in `shared/node_identity.py`
  (`issuer_digest` at 88). Vessel's `deployment.py` (line 99) uses
  `oidc.<sha256(issuer)[:16]>.<subject>`.
- **B131.** Changing the identity provider changes every owner id, so this is a migration point.
- **B132.** The assessment is that the coupling is pervasive, and removing Keycloak means changing
  owner-id derivation everywhere, not only at the auth edge.

### Removal size

- **B133.** About 215 non-doc, non-spec files mention Keycloak across backend, scripts,
  AstralProjection and AstralPlane, including tests. Of those, 39 are backend non-test Python files,
  and 26 are in `orchestrator`.
- **B134.** Native clients: Apple 31, Android 20 and Windows 20 files, including tests and UI
  fixtures.
- **B135.** Vessel has 12 files, about 5 of them code. Kernel has 2 Rust files plus docs.
- **B136.** Core modules that carry identity: `auth.py`, `web_auth.py`, `session_store.py`,
  `delegation.py`, `offline_grant.py`, `device_login.py`, `native_session_custody.py`,
  `ui_auth_renewal.py`, `session_authority.py`,
- **B137.** `mesh_admission.py`, `token_verify.py`, `jwks_cache.py`, `auth_clients.py`,
  `mcp_authz.py`, `a2a_security.py`, `a2a_bridge.py`, `a2a_orchestrator_executor.py`,
  `node_identity.py` and `remote_mcp_oauth.py`.

## C. Docker, containers and sandboxes today

### AstralVessel containers

- **C1.** `AstralVessel/astralvessel/stack.py:41-42` builds `["docker", "compose", "-f", ...]` in
  `compose_command()`.
- **C2.** Lines 45-62 hold `compose()`, `exec()` (`compose exec -T`), `logs()` and
  `require_compose()`, which drive Docker for up, check, down and logs.
- **C3.** Line 2 says so in the file's own words: "up, check and down drive Docker through this."
- **C4.** `AstralVessel/astralvessel/operate.py:166-168` runs `docker` through `_docker(*args)` and
  fails if docker is missing. Lines 178-194 run `docker ps`, `volume ls` and `network ls/inspect`,
  and pick a free /24 in 172.31.0.0/16.
- **C5.** Line 320 runs `docker image inspect` on the node image. Line 344 runs `compose ps -a`.
- **C6.** `operate.py:399-402` calls `write_compose()`, then `compose up -d --build
  --remove-orphans`, behind `astralvessel up`. Line 432 runs `compose down`, with `-v` optional.
- **C7.** `AstralVessel/astralvessel/cli.py:65-69`: `deploy --up` runs `docker compose up`. Line 101
  has a comment about keeping lines in order with docker's when logged.
- **C8.** `AstralVessel/astralvessel/compose.py:1-2` says nothing in the running system touches
  Docker, and the operator runs `docker compose up`. The notes point out that the generator writes a
  file, but `up` and `check` call Docker.
- **C9.** Line 57 sets a build context when run from a checkout.
- **C10.** Line 139 uses a `docker-image://<published>` base for user sandbox images installed from
  a package.
- **C11.** `compose.py:141` builds a per-agent sandbox image from the agent's own Dockerfile
  (`manifest.sandbox.dockerfile`, `manifest.py:43-44`). Third-party package images are built on the
  host.
- **C12.** The probes in `AstralVessel/astralvessel/probes.py` run from inside containers, per lines
  1-2 ("each claim is checked from inside the containers ... one container and prints a JSON line").
- **C13.** `reach()` socket checks are at 20-22, a noexec `/tmp` probe at 53-57, sandbox socket and
  ctypes probes at 105-114.
- **C14.** Lines 184 and 216 check that the executor cannot reach 1.1.1.1:443 and that egress
  refuses odd ports.
- **C15.** `AstralVessel/astralvessel/sandbox.py:1-2` says the executor reaches a sandbox container
  over HTTP,
- **C16.** and "The sandbox container holds no session store." Lines 129-132 read "not the
  container's init process (never reap a developer's machine)." The notes read this as showing that
  the in-process path also runs on developer machines.
- **C17.** `AstralVessel/astralvessel/deployment.py` assumes Docker's IPAM hands containers an
  address range (`front_dynamic_range`, lines 58-73). Lines 201-202 hash sandbox image inputs from
  the Dockerfile.
- **C18.** Line 219 mounts packages inside the executor container at `<root>/<agent id>`.
- **C19.** `manifest.py:111-112` requires the agent `sandbox.dockerfile` to be inside the package.
- **C20.** `AstralVessel/Dockerfile:1-2` says compose applies the sandbox hardening, and the build
  needs `--build-context kernel-artifacts`. Lines 4-5 are `FROM scratch AS kernel-artifacts` and
  `FROM python:3.11-slim`.
- **C21.** Lines 8-10 install ripgrep and ca-certificates.
- **C22.** Line 18 runs `pip install .`, and lines 19-22 bind the kernel through
  `scripts/install_kernel.py --allow-missing`. Lines 24-29 create the non-root user `vessel` (uid
  1001), with `USER vessel` at line 26 and `CMD astralvessel executor` at line 29.
- **C23.** `AstralVessel/egress/Dockerfile:1-2` says egress runs unprivileged on a read-only
  filesystem, and compose gives it the only route out. Line 3 uses `python:3.12-slim`, which differs
  from the executor image's 3.11.
- **C24.** Lines 16-17 hold the HEALTHCHECK against 127.0.0.1:8080, with ports 8080 (model
  passthrough) and 3128 (web proxy).
- **C25.** Docs and README in Vessel: `deploy/deployment.toml:2` says "The node image must exist
  locally." `README.md:5` says it stores data in SQLite in a Docker volume. `README.md:49` lists
  Docker in its threat model as trusted.
- **C26.** `SECURITY.md:22` says the egress token must not be read from outside the egress
  container.
- **C27.** Compose services in `astralvessel/compose.py`: `node` (117-131) is the personal Deep
  node, publishes a host port (line 123), and joins the `front` and `outside` networks.
- **C28.** `egress` (110-115) exists only when `[egress]` is set, holds the model key
  (`LLM_API_KEY`), joins `egress` (internal) and `outside`, and has 512 MB and 128 pids.
- **C29.** The `sandbox-<agent>` service (145-158) runs `astralvessel sandbox`, with one network
  `tools-<agent>` set to `internal: True` (line 133), port 8710, and profile memory, CPU and pid
  limits.
- **C30.** The `executor` service (161-184) runs `astralvessel executor`, joins `front` (static IP)
  and one `tools-<agent>` per agent, and has 1 GB and 256 pids on port 8700.
- **C31.** Every service gets the same hardening (`compose.py:24-29`): `read_only: True`, `cap_drop:
  ["ALL"]`, `no-new-privileges`, `restart: unless-stopped`, and json-file log limits.
- **C32.** Roles inside the containers, per the notes' reading of `docs/get-started/how-it-works.md`
  and `docs/security/isolation.md`: the node runs Deep's personal node
  (`ASTRAL_NODE_PROFILE=personal`, `how-it-works.md:14`).
- **C33.** The executor verifies the owner's ticket for each tool call, then forwards the call to
  the right sandbox with that sandbox's token.
- **C34.** Each sandbox runs grep, bash and declared command tools, and each call is confined with
  Landlock and seccomp (`confine.py`).
- **C35.** The egress container holds the model passthrough and a guarded web proxy.
- **C36.** Per `egress/proxy.py:1-2`, the proxy "reaches only public web servers on ports 80 and
  443." `docs/run/models-and-egress.md:11` says the egress is the only container that holds the key.
- **C37.** Host requirements, per `docs/index.md:27-30`: Linux 6.12 or newer with Landlock ABI 6, or
  Docker Desktop on Apple silicon, plus Docker with Compose.
- **C38.** `docs/security/platforms.md:8-12` marks Linux 6.12+ on x86_64 or arm64 with Landlock ABI
  6 as supported.
- **C39.** Older Linux, or Landlock disabled, makes sandboxes refuse to start.
- **C40.** `docs/security/platforms.md:8-12` marks macOS with Docker Desktop on Apple silicon as
  supported, because the containers run in Docker Desktop's Linux VM, checked on "Docker Desktop
  29.5 (VM kernel 6.12)".
- **C41.** Windows with Docker Desktop is marked unverified at line 12.
- **C42.** Quickstart and checks: `docs/security/platforms.md:21-24` runs the Landlock check inside
  a container with `docker run --rm astralvessel:local python -c "...confine.abi_version()"`.
- **C43.** `docs/get-started/quickstart.md:10` runs `docker build --build-context
  kernel-artifacts=... -t astralvessel-node:local .`.
- **C44.** Lines 18-21 say `pip install astralvessel` installs the package, and from a checkout `pip
  install -e .` builds the images locally. Line 35 says `up` starts the containers, waits for the
  node and the sandboxes, and runs the checks.
- **C45.** `docs/troubleshooting.md:9` says to run `docker compose -f compose.generated.yml logs
  node`. Line 21 says a failure in a sandbox probe means isolation does not hold on this host.
- **C46.** `docs/security/threat-model.md:23` lists kernel and container escapes, with isolation
  resting on Landlock, seccomp and Docker.
- **C47.** `docs/security/trust.md:9` says Docker and Compose enforce networks, read-only mounts and
  resource limits. `docs/security/isolation.md:6-18` has the container table.
- **C48.** Inference from the notes: `pip install astralvessel` alone does not give a working node.
  The user also needs Docker with Compose, an image build or pull, and a Linux kernel with Landlock
  ABI 6 or Docker Desktop on Apple silicon.
- **C49.** Plain macOS without Docker is not a supported Vessel host today (inferred).
- **C50.** Conformance and tests: `AstralVessel/astralvessel/conformance.py:12-13` imports `node`
  and `httpx`, and runs checks over HTTP against a node URL. No Docker call appears in the file.
- **C51.** Line 99 is the first check: the node reports ready with its database up.
- **C52.** Vessel tests: `tests/test_conformance.py:12` and lines 61-136 call
  `conformance.run(DEPLOYMENT, TOKEN)` without starting containers.
- **C53.** `tests/test_operate.py:1` and lines 26-27 describe "the operator commands without
  Docker," with a `no_docker` fixture that patches `_docker` to return `""`.
- **C54.** `tests/test_cli.py:43-49` asserts the CLI prints `start it with: docker compose -f`.
  `tests/test_deploy.py:101-182` checks the generated Compose for network isolation and per-sandbox
  confinement.
- **C55.** `tests/test_units.py:107` checks that the reaper is a no-op when the process is not
  container init.
- **C56.** Vessel CI: `.github/workflows/ci.yml:46-52` installs ripgrep from a pinned release. Lines
  65-88 run `docker compose -f compose.generated.yml up -d --build executor`, then an `exec -T
  executor python -c ...` readiness check, with logs on failure.
- **C57.** The notes say the container path is CI-only.
- **C58.** `.github/workflows/release.yml` (lines 1, 53-64 and 103) publishes the vessel and egress
  images to GHCR for linux/amd64 and linux/arm64.
- **C59.** Inference from the notes: the probes (`astralvessel check`) and the CI executor smoke
  test require containers. The unit-level conformance suite does not. The isolation claims are
  exercised only inside containers (inferred).

### AstralVessel native isolation

- **C60.** `AstralVessel/astralvessel/confine.py:1-2` says it confines one tool call before it runs.
  Landlock lets the call read the system and the agent's data, and write only its own scratch
  directory. seccomp blocks IPC and networking.
- **C61.** In `confine.py`, `MAKE_SOCK` is never granted, because Landlock does not mediate
  `connect()` to a socket file (line 21). `abi_version()` and `apply()` are at 32-48. `apply()`
  requires `REQUIRED_ABI` and otherwise raises.
- **C62.** It uses `prctl(NO_NEW_PRIVS)` and `landlock_restrict_self`.
- **C63.** The code is Linux only, through ctypes to libc.
- **C64.** `block_shared_ipc()` at `confine.py:79-117` builds a seccomp BPF filter that blocks SysV
  IPC, POSIX message queues, `setsid`, `setpgid`, and IPv4 and IPv6 sockets.
- **C65.** `docs/security/isolation.md:20-30` has the matching table of Landlock filesystem, network
  and scoping rules, plus seccomp and no_new_privs.
- **C66.** Inference from the notes: Vessel's native sandbox is Linux only. It works on a Mac only
  inside Docker Desktop's Linux VM. No macOS-native path exists (inferred).

### Spec 105 and Docker

- **C67.** Docker is not mentioned in `spec.md`, `plan.md` or `runtime-composition-plan.md`. The
  only mentions are in `phase-r-plan.md` (dev commands, line 61 and line 17) and
  `local-integration-handoff.md` (qualification).
- **C68.** The design does not state a container requirement for the Mac app or for `astralvessel
  up`, and it does not say they must avoid one. Ref:
  `AstralDeep/specs/105-decentralized-personal-mesh/` (grep).
- **C69.** The Mac app: `spec.md:917-919` says AstralVessel is the base and its command-line tools,
  with no GUI. On the Mac it carries the whole base, including Deep's Python node on Plane's SQLite
  backend as a supervised background process.
- **C70.** `plan.md:271` puts the first-party agents in a supervised local process inside the Mac
  and Windows apps. `plan.md:285` says a Mac with no node runs a bundled first-party agent. Table
  4.3 is `spec.md:162-165`.
- **C71.** An iPhone carries the kernel only, since iOS cannot run Deep's Python node
  (`spec.md:919`).
- **C72.** What `up` needs, per the spec: `plan.md:144` says `astralvessel up` installs a personal
  node that runs Deep's turn loop and gates, with Plane and LETS. Vessel's own loop is retired, and
  its sandbox and egress become the node's executor.
- **C73.** The table at `spec.md:929-931` says the same, and `spec.md:966` repeats it.
- **C74.** `runtime-composition-plan.md:115-116` calls for building the supervised Mac node pack and
  retiring Vessel's loop through ticket conformance and sandbox and egress acceptance tests.
- **C75.** Inference from the notes: the 105 documents say Vessel's containers become the executor.
  They do not say how the executor is packaged on a Mac or on a personal install without Docker.
- **C76.** As written, the executor is still Vessel's sandbox and egress pair, which today are
  Docker containers.
- **C77.** A Docker-free design would have to replace the container boundary with an OS-native one,
  and the documents do not describe that replacement (inferred).
- **C78.** Packaging of Deep's Python node on the Mac is not stated.
- **C79.** The docs say "supervised background process" (`spec.md:919`) and "supervised local
  process" (`plan.md:271`), but not how the interpreter, dependencies, component wheels or model
  files are bundled.
- **C80.** `AstralDeep/Dockerfile:60` installs components as wheels through
  `scripts/install_local_components.py`, which exists but is not described for the Mac app
  (inferred).
- **C81.** Supervision is not stated. No launchd, login item, helper tool or `SMAppService` is named
  in any 105 document.
- **C82.** A grep of `AstralProjection/apple-clients` for launchd, SMAppService, LoginItem and
  ProcessSupervisor found no hits outside Info.plist text (code search). No Python node supervisor
  exists yet.
- **C83.** Databases: devices use "SQLite inside the kernel" (`spec.md:245`), and "No device needs
  PostgreSQL" (`spec.md:246`).
- **C84.** The node gains a SQLite backend in AstralPlane, so Deep's Python node runs with no
  database server, inside the Mac app and in a personal AstralVessel install (`spec.md:247-250`).
- **C85.** Institutions keep PostgreSQL (`spec.md:253`).
- **C86.** Status of the SQLite node: `local-integration-handoff.md:69-71` says Deep still calls
  `create_postgres_runtime` and expects PostgreSQL's registry.
- **C87.** Its SQLite host selection remains to implement, and changing the library pin alone does
  not enable a SQLite node. `runtime-composition-plan.md:105-111` says the same.
- **C88.** The handoff lists `feat/105-sqlite-node` at `dd61b1a6` as adding SQLite or PostgreSQL
  selection for Plane, with the step "boot a personal node on SQLite end to end" still pending
  (`lih:117-130`).
- **C89.** The word "executor" means different things. In spec 105 it is the component that checks
  an owner-signed ticket before a tool runs (`spec.md:441-442`). In Vessel today it is the container
  service (`compose.py:161-184`).
- **C90.** In Deep the name also appears in `generated_lets_executor.py` and
  `a2a_orchestrator_executor.py`, for LETS and A2A, not containers.

### Mac and Linux native sandboxes

- **C91.** `AstralProjection/apple-clients/AstralCore/Sources/AstralCore/Computer/ComputerSandboxProfile.swift:1-3`
  generates the macOS Seatbelt (SBPL) profile text for one governed command job.
- **C92.** Enforce mode writes only inside granted roots and the per-job directory, keeps protected
  metadata read-only, makes exclusions and keychains unreadable, and permits network only to the
  loopback egress proxy.
- **C93.** Line 10 sets `launcherPath = "/usr/bin/sandbox-exec"`.
- **C94.** Shadow mode is `(allow default)` with violation logging only
  (`ComputerSandboxProfile.swift:45-53`).
- **C95.** Enforce mode is `(deny default)`, with `allow process-exec*`, `allow file-read* (subpath
  "/")`, `allow mach-lookup`, `allow ipc-posix*`, `allow ipc-sysv*`, `network-outbound` only to the
  proxy port on localhost,
- **C96.** and `file-write*` only inside granted roots (`ComputerSandboxProfile.swift:184-210`).
- **C97.** `ComputerSandboxPolicy.swift` defines `enum ComputerSandboxMode { shadow, enforce }` at
  lines 7-9. The fallback configuration is shadow (line 124), and decoding defaults to shadow when
  the field is missing (line 140).
- **C98.** `AstralProjection/apple-clients/AstralApp/AstralApp/Computer/MacComputerSandbox.swift:1-3`
  prepares each governed-command launch.
- **C99.** It loads the owner's sandbox configuration, starts the per-job loopback egress proxy, and
  generates and compile-probes the Seatbelt profile.
- **C100.** It refuses the job before it starts if the launcher, profile or proxy cannot be readied.
  `MacComputerHost.swift:672` checks whether the mode is enforce.
- **C101.** `MacComputerCommands.swift:1-4` says the sandbox controller wraps each spawn in a probed
  Seatbelt profile, behind a loopback egress proxy. Lines 481-506 call `sandbox.prepare(...)` and
  then spawn, and on failure throw `sandbox_refused`.
- **C102.** Lines 749-783 use the `posix_spawn` path, with pipes for stdout and stderr.
- **C103.** Lines 409 and 466 use `/bin/zsh -lc` for the PATH probe and free-form shell.
- **C104.** `AstralCore/.../ComputerCommandPolicy.swift:31` holds a denylist of programs, including
  `node`, `ruby`, `perl`, `env`, `xargs`, `osascript`, `open`, `sudo`, `su`, `launchctl` and
  `defaults`.
- **C105.** `MacComputerEgressProxy.swift:1-2` and `:27` run a per-job loopback proxy on
  Network.framework, bound to 127.0.0.1.
- **C106.** `MacComputerSystemIO.swift:99` calls `AXIsProcessTrusted()` for the Accessibility
  permission.
- **C107.** `ComputerHostProtocol.swift:883-886` accepts a direct-download build only when
  `!sandboxed && hardened && signatureValid && identityMatches && !debuggerAllowed`.
- **C108.** `ComputerSandboxProfileTests.swift:3` is described as "system sandbox-exec on a real
  Mac."
- **C109.** Mac entitlements, under `AstralProjection/apple-clients/AstralApp/`:
  `AstralApp-macOS.entitlements` sets `com.apple.security.app-sandbox` to true (lines 11-12),
  `network.client` (15-16), `network.server` (20-21),
- **C110.** `device.audio-input` (24-25) and `files.user-selected.read-write` (35-36).
- **C111.** `AstralApp-DirectDownload.entitlements` has only `device.audio-input` and no app-sandbox
  key. `AstralApp-iOS.entitlements` has `aps-environment` only.
- **C112.** Build settings in `AstralApp/AstralApp.xcodeproj/project.pbxproj`: lines 649-650 set
  `ENABLE_APP_SANDBOX` and `ENABLE_HARDENED_RUNTIME` to YES for macosx.
- **C113.** Lines 1007-1009 set the DirectDownload configuration to `ENABLE_APP_SANDBOX` NO with
  hardened runtime YES.
- **C114.** `apple-clients/README.md:213-217` and `:327`, and `apple-clients/KNOWN-ISSUES.md:22-23`,
  say the Release configuration enables App Sandbox and Hardened Runtime.
- **C115.** Inference on how the Mac app runs governed commands: it spawns `/bin/zsh -lc` or a
  direct argv through `posix_spawn`, wraps the spawn in `/usr/bin/sandbox-exec` with a generated
  SBPL profile, and routes network through a per-job loopback proxy.
- **C116.** The default mode is shadow, which allows everything and logs violations.
- **C117.** Whether enforce is the shipped default is not established by these files. The
  DirectDownload build is not App Sandboxed (inferred).
- **C118.** Linux, AstralDeep: `AstralDeep/backend/orchestrator/sandbox.py:1-4` is an OS-level
  sandbox for launched child processes, using POSIX resource limits through a preexec_fn plus a
  scrubbed, temp-scoped environment.
- **C119.** `SandboxLimits` at lines 39-45 sets `address_space_mb=2048`, `file_size_mb=256`,
  `open_files=512` and `processes=512`, read from env.
- **C120.** In the same file, `_apply_limits` (60-83) sets RLIMIT_AS, RLIMIT_FSIZE, RLIMIT_NOFILE
  and RLIMIT_NPROC, and an optional RLIMIT_CPU. `make_preexec()` (86-93) returns None when `os.name`
  is not posix. On macOS it sets rlimits only.
- **C121.** `sandbox_env()` (96 on) drops keys in `_SECRET_ENV_DENYLIST`.
- **C122.** Callers of `sandbox.py`: `backend/orchestrator/agent_lifecycle.py:5180` (draft agents),
  and `backend/agents/general/file_tools/structured_document.py:22` (`make_preexec` and
  `sandbox_env`).
- **C123.** `AstralDeep/backend/orchestrator/sandbox_bwrap.py:1-3` seals the pinned SRT runtime's
  private data mounts before it executes the captured Bubblewrap binary.
- **C124.** `sandbox_qualification.py:1-3` qualifies the pinned Linux sandbox for its registered
  pilot and operator diagnostics, through the LETS executor.
- **C125.** `sandbox_qualification.py:84-95` pins `@anthropic-ai/sandbox-runtime` `0.0.78`, with a
  SHA-512 tarball pin. Lines 106-117 list support for linux/amd64 and linux/arm64 on ubuntu-24.04
  with the distribution bubblewrap, and refuse other hosts.
- **C126.** Lines 589-594 require a vendored `vendor/seccomp/<arch>/apply-seccomp` helper.
- **C127.** The `vendor/` tree is not tracked in git (`git ls-files` returned nothing) (code).
- **C128.** `sandbox_tool_sessions.py:1-3` binds the fixed sandbox pilot to reviewed in-process
  dispatch and normal dynamic LETS admission. The test is
  `AstralDeep/backend/tests/test_sandbox_bwrap_seal.py`.
- **C129.** Inference: Deep's bwrap and seccomp sandbox is a Linux-only pilot, reached through the
  LETS executor. It is not on the macOS path, and its runtime helper is a vendored binary that is
  not in git (inferred).
- **C130.** Windows, WASM and other platforms: no AppContainer, Job Object or WASM runtime was found
  in any checkout. Grep found those names only in lock files. No firejail, nsjail, gVisor, Kata or
  cgroup code appears in Deep or Vessel source.
- **C131.** Projection has no Windows sandbox code in the paths checked.
- **C132.** The Windows client was not searched in depth.
- **C133.** LETS has a `Dockerfile`, and `docs/development.md:88-92` describes Docker volumes for
  databases. That is not a sandbox.
- **C134.** AstralKernel's `scripts/build-python.sh:2-16` builds the Linux arm64 wheel inside a
  pinned `rust:1.99.1-bookworm` Docker image, which is build tooling only.
- **C135.** AstralPlane's `docs/endpoint-failover-readiness.md:173` says "Docker-hosted platform
  acceptance" is outside its own CI. Neither is a runtime sandbox.
- **C136.** Inference: the notes count two egress implementations on the Mac path. One is in the
  Swift app (Seatbelt plus loopback proxy). The other is Vessel's Python proxy inside a container. A
  native design needs one of these, not both (inferred).
- **C137.** Inference: Linux has native isolation in two forms, Vessel's `confine.py` (Landlock plus
  seccomp) and Deep's `sandbox_bwrap.py` and `sandbox_qualification.py` (Anthropic sandbox-runtime,
  bubblewrap and seccomp).
- **C138.** Both are Linux only, and neither is macOS native (inferred).

### Egress

- **C139.** Deep, user-controlled URLs: `AstralDeep/backend/shared/external_http.py:1-3` is an
  SSRF-guarded helper that rejects loopback, private and link-local targets on every A and AAAA
  record.
- **C140.** Lines 78-88 use `_resolve_host_addresses()` with `socket.getaddrinfo`.
- **C141.** Lines 91-107 hold `_classify_blocked()` and `_is_private_address()`, with
  `shared.net_guard.is_private_http_address`.
- **C142.** `external_http.py:110-141` `validate_egress_url()` allows http and https only, and
  blocks private addresses unless they are listed in `EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS`.
- **C143.** Lines 144-210 capture the resolved address, so the request connects to the checked IP
  (pinned, resistant to DNS rebinding) (code).
- **C144.** Line 211 starts `_PrivateHttpAdapter`. Line 256 starts `request(...
  allow_redirects=False ...)`, with typed errors `EgressBlockedError` and `ResponseTooLargeError`.
- **C145.** Vessel egress: `AstralVessel/egress/proxy.py:1-2` is a minimal forward proxy that
  reaches only public web servers on ports 80 and 443. Each target is resolved and checked, then
  connected by the checked address.
- **C146.** Line 12 imports `Forbidden` and `resolve_public` from `egress.app`.
- **C147.** Vessel network enforcement: `AstralVessel/astralvessel/compose.py:100-108` sets the
  `front` and `egress` networks to `internal: True`. Line 133 does the same for each `tools-<agent>`
  network.
- **C148.** Lines 111-115 join egress to `egress` (internal) and `outside`.
- **C149.** Lines 117-125 join the node to `front` and `outside`, so the node reaches the executor
  and the egress only through named networks.
- **C150.** `docs/get-started/how-it-works.md:9` says "sandbox-<agent>: its own tools network, which
  is internal." `probes.py:184` and lines 216-226 check that the executor cannot reach 1.1.1.1:443,
  and that the proxy refuses port 22.
- **C151.** Mac command egress: `MacComputerEgressProxy.swift:1-2` and `:27` run a per-job loopback
  proxy with a domain allowlist.
- **C152.** `ComputerSandboxProfile.swift:201-204` limits Seatbelt `network-outbound` to localhost
  on the proxy port, and limits `network-bind` and `network-inbound` to localhost.
- **C153.** In shadow mode (`ComputerSandboxProfile.swift:45-53`) the profile does not block
  network, though the proxy still exists.
- **C154.** Mechanisms that enforce egress today: Docker internal networks (Vessel), a pure-Python
  proxy with resolve-and-check (Vessel and the Mac side), Seatbelt deny default (Mac, enforce mode
  only), and the application-level SSRF guard in `external_http.py` (Deep).
- **C155.** Inference: Deep's first-party in-process agents and the Deep node process have no
  OS-level egress boundary. The guard sits in the calling code path (inferred).

### AstralDeep Docker and runtime assumptions

- **C156.** `AstralDeep/Dockerfile:1-3` is the single backend image, serving on port 8001.
- **C157.** Lines 5-36 hold the `component-builder` stage, which builds local component wheels for
  AstralProjection, AstralPlane, AstralPrimitives and LETS through
  `scripts/install_local_components.py build`.
- **C158.** Line 38 starts the runtime stage on python:3.11-slim. Line 54 runs `pip install -r
  backend/requirements.txt -r requirements-document-worker.txt`. Line 69 runs `python -m spacy
  download en_core_web_lg`.
- **C159.** Line 72 imports `agents.gaiakeep.catalog` at build.
- **C160.** Lines 74-80 copy `backend/start-docker.sh`, expose 8001 (line 78) and set the CMD (line
  80).
- **C161.** `AstralDeep/Dockerfile.voice` builds a separate voice-worker image: `FROM
  ${PYTHON_SLIM_IMAGE}`, `/opt/voice-venv`, `voice_agent.main`, and user uid 10001 (lines 7, 28,
  169, 248-249).
- **C162.** `Dockerfile.voice.dockerignore` and `.dockerignore` set context exclusions.
- **C163.** `AstralDeep/docker-compose.yml` defines these services: `postgres`
  (`postgres:17-alpine`, container `astraldeep-postgres`, lines 2-19), `livekit` (livekit-server
  pinned by digest, read-only, with cap_drop, lines 20-44),
- **C164.** `astraldeep` (container `astraldeep`, env_file, depends_on postgres, lines 45-106),
  `voice-worker` (lines 107-155) and `voice-worker-test` (lines 156-179).
- **C165.** Volume `pgdata` is at 180-181.
- **C166.** Other compose files in AstralDeep: `docker-compose.staging.yml`,
  `docker-compose.voice-integration.yml` and `deploy/lets/docker-compose.lets.yml`. The notes did
  not read these in detail.
- **C167.** Makefile targets: `up` (line 69) builds and starts all containers and depends on
  `composition-preflight` (62). `down` (72) runs docker compose. `restart` (75) restarts the
  `astraldeep` container. `apply-config` (78) recreates the app container.
- **C168.** `build` (82) builds the image.
- **C169.** Lines 85-97 cover `ps`, `logs`, `logs-db`, `shell` and `psql`. `sync-backend` (100) tars
  the backend through an alpine container, copies it into `astraldeep` and restarts.
- **C170.** More Makefile targets: `sync-components` and `sync` (107-111) rebuild or recreate the
  app so component wheels stay in sync. `test-backend` (113) runs pytest inside the `astraldeep`
  container. `test-060` (119) is a focused suite.
- **C171.** `test-web` (132) runs Playwright in a pinned image.
- **C172.** `test-projection` (145) runs the Projection Python suite in the product image.
  `prepare-release-evidence` (150) is a local diagnostic. `lint-backend` and `lint-web` (153-161)
  run Ruff and ESLint. `secret-scan` (166) runs gitleaks in a container.
- **C173.** `scripts/node/install-personal-node.sh:103` prints `docker run --env-file ... -v ... -p
  8001:8001 <image>` as the next step. The notes say the personal-node installer already assumes
  Docker is the operator path.
- **C174.** Its `--database-url` option (line 15) takes a PostgreSQL DSN.
- **C175.** `AstralDeep/backend/start-docker.sh:1-4` and lines 11-22 are the container entrypoint.
  It runs `cd /app/backend`, refuses to start with exit 78 if `/app/backend/data/astral.db` or
  `test_audit.db` exists, then runs `exec python start.py`.
- **C176.** The notes classify it as container-only runtime.
- **C177.** Operator text that mentions Docker: `backend/orchestrator/connection_guide.py:133` shows
  "docker compose ps postgres," and line 244 shows "docker compose up voice-worker." These are
  runtime text only.
- **C178.** `backend/orchestrator/orchestrator.py:5968` mentions "a Docker host alias."
- **C179.** `backend/orchestrator/agent_peer_auth.py:18-26` has a loopback allowlist that includes
  `host.docker.internal` and `gateway.docker.internal`. The notes call this a Docker assumption in
  an auth check.
- **C180.** `orchestrator.py:23932` and `:23964` write `/app/frame_trace.jsonl`, gated on
  `os.path.exists("/app/.frame_trace")`. This is a debug-gated container path.
- **C181.** CI and qualification scripts use Docker: `scripts/backend_web_image_gate.sh` (docker
  run, exec and network create for boot-smoke), `scripts/initialize_backend_web_services.py` (75 and
  270-732, which needs a POSIX Docker host),
- **C182.** `scripts/run_backend_web_qualification.py` (65-530, with docker run, create, exec
  pg_dump and cp), and `scripts/migration/rehearse_089_upgrade.sh` (13-81, an ops rehearsal with
  docker exec psql and docker run of `astraldeep:latest`).
- **C183.** More Docker-touching scripts: `scripts/openai_agents_example/server_identity.py` (28-61,
  example script, docker context and inspect),
- **C184.** `scripts/verification/typesafe_canary_scan.py:150` and `typesafe_turn_timeline.py:145`
  (docker logs), and `scripts/rehearse_backend_web_state.py:235` (inserts `/app/backend` into
  sys.path).
- **C185.** `backend/devtools/codex_security_review.py` (191 and 1515) expects a `bwrap` sandbox
  program. This is dev tooling.
- **C186.** Grep of `backend/` outside tests found no subprocess call to Docker in the
  orchestrator's normal dispatch path. The Docker-specific runtime code is the entrypoint, two text
  messages, the allowlist entries and the `/app/.frame_trace` hook.
- **C187.** Container-only runtime paths: `/app/backend/data` (the SQLite legacy guard),
  `/app/backend/tmp/attachments` (qualification, `run_backend_web_qualification.py:446`),
  `/app/config/astral-composition.json` (read in a container run),
- **C188.** and `/opt/astral-component-wheels/` (the component wheels in the Dockerfile, around line
  34 and line 57).
- **C189.** Dev and CI only: the Makefile targets above, except `up`, `build` and `apply-config` if
  they serve production. The staging, voice-integration and LETS compose files. The qualification
  scripts. `phase-r-plan.md:61` (docker exec pytest).
- **C190.** `phase-r-plan.md:17` ("The container has no pytest-cov").
- **C191.** The AGENTS.md build section. `lih:101-137` (isolated PostgreSQL task containers).

### What a user-installed node needs from containers today

- **C192.** Python 3.11 runtime: `python:3.11-slim` in `AstralDeep/Dockerfile:5` and `:38`, and in
  `AstralVessel/Dockerfile:6`. Vessel's egress image uses `python:3.12-slim`. The plan for the Mac
  is Deep's node as a supervised background process (`spec.md:919`).
- **C193.** The packaging method is not stated.
- **C194.** Python dependencies: `pip install -r backend/requirements.txt` in the Deep image
  (`AstralDeep/Dockerfile:54`), and `pip install .` in Vessel (`AstralVessel/Dockerfile:18`). The
  notes say the app bundle's plan is not stated.
- **C195.** Component wheels exist (`scripts/install_local_components.py`,
  `config/astral-composition.json`).
- **C196.** Native libraries: Deep installs poppler-utils, libmagic1, build-essential, cmake,
  libssl-dev and git (`AstralDeep/Dockerfile:41-47`). Vessel installs ripgrep
  (`AstralVessel/Dockerfile:9-10`). No native replacement is stated.
- **C197.** The spaCy model `en_core_web_lg` is downloaded at image build
  (`AstralDeep/Dockerfile:69`). No replacement is stated.
- **C198.** PostgreSQL: the `postgres:17-alpine` service (`AstralDeep/docker-compose.yml:2-19`). The
  personal-node installer takes `--database-url`. The plan is SQLite through Plane, with no database
  server (`spec.md:245-253`).
- **C199.** Plane's SQLite backend is not enabled in Deep (`lih:69-71`).
- **C200.** Keycloak is not in any compose file. The issuer is external (`iam.ai.uky.edu`,
  `spec.md:87`). The plan keeps Keycloak only at enrollment (`spec.md:33`), and says a personal node
  "trusts one issuer and one owner" (`how-it-works.md`).
- **C201.** Delegation mints locally with an asymmetric key (`plan.md:143`).
- **C202.** The notes do not state how a personal node obtains its first issuer when Keycloak is
  unreachable.
- **C203.** Model weights: the Vessel egress container forwards to an OpenAI-compatible endpoint
  with a key it holds (`docs/run/models-and-egress.md:11`). Deep uses the LLM Factory at UK
  (`spec.md:984`).
- **C204.** The spec says any OpenAI-compatible endpoint works (`spec.md:984`), and a desktop can
  run "with a local model" (`spec.md:162`).
- **C205.** Local weights on the desktop are not stated.
- **C206.** Phones have no on-device model in containers. The plan names Apple Foundation Models, an
  Android on-device model and a Windows local runtime (`plan.md:209`). These are planned.
- **C207.** Speech: Deep uses a `voice-worker` container and LiveKit
  (`AstralDeep/docker-compose.yml:20-44` and `:107-155`, and `Dockerfile.voice`). The spec says
  voice runs on the device (`spec.md:104`, 105-C8).
- **C208.** Apple uses the system streaming transcriber (`spec.md:778`), and Windows a bundled
  streaming recognizer (`spec.md:780`). LiveKit is to be retired (`plan.md:246`). Speech on the Mac
  node is not stated.
- **C209.** The egress proxy: Vessel's egress container becomes the executor's egress
  (`plan.md:144`, `spec.md:966`). Its form is not stated.
- **C210.** Tool sandbox: Vessel's `sandbox-<agent>` container. The spec says "In its sandbox"
  (`spec.md:164`) with no mechanism named. Vessel's container becomes the executor (`spec.md:931`).
  The 105 documents do not name the Mac app's Seatbelt path.
- **C211.** How a Docker-free node sandboxes tools is not stated.
- **C212.** Per-agent sandbox images are built from each agent's Dockerfile (`compose.py:136-141`).
  No replacement is stated.
- **C213.** Redis and an object store: none appear in compose. The blob store belongs to AstralPlane
  (`AGENTS.md`, `components/AstralPlane` blob stores), which the notes did not read in detail.

### LiveKit and debug hooks

- **C214.** LiveKit runs as the `livekit/livekit-server` container
  (`AstralDeep/docker-compose.yml:20-44`). The plan retires `backend/voice_agent/` and LiveKit "once
  clients no longer use them" (`plan.md:246`).
- **C215.** The frame and debug hook `/app/.frame_trace`
  (`AstralDeep/backend/orchestrator/orchestrator.py:23932`) is a dev path, not relevant to users.

### Summary inferences

- **C216.** The Seatbelt enforce mode with a loopback egress proxy is the closest existing native
  sandbox.
- **C217.** It has been used only for governed shell commands, and its default configuration is
  shadow mode, which does not block (inferred from `ComputerSandboxPolicy.swift:124,140` and
  `MacComputerHost.swift:672`).
- **C218.** Native dependencies that the containers install (poppler, libmagic, the spaCy model,
  ripgrep) have no native replacement stated in spec 105.

## D. Protocols and platform facts

The notes tag primary reads with [P]. Entries marked UNVERIFIED come from secondary sources, search
snippets or recall. Each entry gives its URL, or says the note gave none.

### MCP authorization

- **D1.** MCP revisions are 2025-06-18, 2025-11-25 and 2026-07-28. The 2026-07-28 revision is marked
  "Current" on the versioning page. No other dated revision was seen. The notes did not check
  whether a newer draft is open (UNVERIFIED). [P]
  https://modelcontextprotocol.io/specification/versioning
- **D2.** Changes from 2025-06-18 to 2025-11-25: OpenID Connect Discovery is added for AS discovery
  (PR 797). Incremental scope consent comes through WWW-Authenticate (SEP-835).
- **D3.** OAuth Client ID Metadata Documents (CIMD) become a recommended registration (SEP-991).
- **D4.** PRM discovery is aligned with RFC 9728, with WWW-Authenticate optional and a `.well-known`
  fallback (SEP-985). URL mode elicitation is added (SEP-1036). [P]
  https://modelcontextprotocol.io/specification/2025-11-25/changelog
- **D5.** Auth changes from 2025-11-25 to 2026-07-28: DCR (RFC 7591) is deprecated in favor of CIMD
  (PR 2858) and kept for backward compatibility.
- **D6.** Clients MUST validate `iss` (RFC 9207) if present, against the recorded issuer, before
  redeeming a code (SEP-2468). Clients MUST set `application_type` in DCR (SEP-837).
- **D7.** Client credentials MUST be keyed by AS issuer and never reused across AS (SEP-2352). [P]
  https://modelcontextprotocol.io/specification/2026-07-28/changelog
- **D8.** Two official auth extensions are now stable: enterprise-managed authorization, and OAuth
  client credentials (see D29 and D31). The servers SHOULD send `iss`, and a future revision is
  expected to make it MUST. [P] https://modelcontextprotocol.io/specification/2026-07-28/changelog
- **D9.** Non-auth changes in 2026-07-28 that matter to a client: the protocol is stateless, with no
  initialize handshake and no Mcp-Session-Id, and per-request `_meta` carries protocolVersion and
  clientCapabilities.
- **D10.** A `server/discover` RPC is added (SEP-2575, SEP-2567).
- **D11.** Multi Round-Trip Requests replace server-initiated requests with `InputRequiredResult`,
  and the client retries with `inputResponses` (SEP-2322). `notifications/elicitation/complete` and
  `elicitationId` are removed. [P]
  https://modelcontextprotocol.io/specification/2026-07-28/changelog
- **D12.** More 2026-07-28 changes: tasks move into the extension `io.modelcontextprotocol/tasks`.
  Roots, Sampling and Logging are deprecated. The HTTP+SSE transport is deprecated, and SSE
  resumability is removed. List results gain `ttlMs` and `cacheScope`. [P]
  https://modelcontextprotocol.io/specification/2026-07-28/changelog
- **D13.** Client requirements (2026-07-28): authorization is OPTIONAL, and HTTP transports SHOULD
  conform. STDIO servers SHOULD NOT follow the spec and instead take credentials from the
  environment.
- **D14.** The spec builds on OAuth 2.1 draft-ietf-oauth-v2-1-13, RFC 6750, 8414, 7591, 8707, 9728,
  9207, CIMD draft-00 and OIDC Discovery. [P]
  https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization
- **D15.** PKCE: clients MUST implement PKCE and MUST use S256 when capable. They MUST verify
  support through `code_challenge_methods_supported` in AS metadata, and refuse to proceed if it is
  absent (also for OIDC metadata). [P]
  https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/security-considerations
- **D16.** PRM (RFC 9728): servers MUST implement it. Clients MUST parse `resource_metadata` from
  WWW-Authenticate on a 401. Otherwise they fall back to
  `/.well-known/oauth-protected-resource/<path>`, then to the root.
- **D17.** The PRM MUST contain `authorization_servers`, and the client chooses the AS. [P]
  https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/authorization-server-discovery
- **D18.** AS metadata: clients MUST support both RFC 8414 and OIDC discovery. With path issuers,
  they try, in order, the oauth-authorization-server path-insertion URL, the openid-configuration
  path-insertion URL, and the openid-configuration path-append URL.
- **D19.** Without a path, they try oauth-authorization-server, then openid-configuration. The
  issuer in the document MUST equal the issuer used to build the URL. [P]
  https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/authorization-server-discovery
- **D20.** Registration priority (SHOULD): (1) pre-registered client info, (2) CIMD if AS metadata
  has `client_id_metadata_document_supported`, (3) DCR if there is a `registration_endpoint`, (4)
  prompt the user. [P]
  https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/client-registration
- **D21.** CIMD: the client_id is an HTTPS URL with a path. The document must have client_id,
  client_name and redirect_uris, and the AS fetches it. The example document lists
  http://127.0.0.1:3000/callback and localhost.
- **D22.** CIMD cannot stop localhost impersonation.
- **D23.** The AS should warn, MAY require attestation, and MUST show the redirect host. [P]
  https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/client-registration
- **D24.** Pre-registration: clients SHOULD support static credentials, with a UI to enter them.
  Credentials MUST be bound to the issuer. [P]
  https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/client-registration
- **D25.** Resource Indicators (RFC 8707): `resource` MUST appear in both the authorization and
  token requests. It MUST be the canonical MCP server URI, and it MUST be sent even if the AS does
  not support it. Servers MUST validate token audience. [P]
  https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization
- **D26.** Token use: `Authorization: Bearer` goes on every request, never in the query string.
  Clients MUST NOT send tokens to an MCP server other than ones issued by that server's AS.
- **D27.** Servers MUST NOT accept or transit other tokens, and MUST NOT pass the client's token to
  upstream APIs (token passthrough is forbidden). [P]
  https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization
- **D28.** Redirect URIs MUST be localhost or HTTPS. Clients MUST register exact redirect URIs, and
  SHOULD use and verify `state`. [P]
  https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization
- **D29.** Scope selection (SHOULD): use `scope` from the 401 WWW-Authenticate. Otherwise use all of
  the PRM's `scopes_supported`. Otherwise omit scope. Step-up: the server returns 403 with
  `error="insufficient_scope"`, `scope=...` and `resource_metadata`.
- **D30.** A client acting for a user SHOULD re-authorize with the union of previous and challenged
  scopes, with a retry limit. Client-credentials clients MAY abort. [P]
  https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization
- **D31.** Refresh: clients MUST keep refresh tokens confidential, MUST NOT assume they were issued,
  and MAY request `offline_access` if it is in `scopes_supported`. The AS MUST rotate public-client
  refresh tokens and SHOULD issue short-lived access tokens.
- **D32.** A table covers RFC 9207: clients MUST validate a present `iss`, and must reject a
  response that lacks it when the AS says it is supported. [P]
  https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization
- **D33.** Enterprise-Managed Authorization (stable, revision 2026-07-28) is an enterprise profile
  of the Identity Assertion JWT Authorization Grant (ID-JAG). The client does SSO to the IdP through
  OIDC or SAML.
- **D34.** RFC 8693 token exchange at the IdP yields an ID-JAG, which is then used in an RFC 7523
  jwt-bearer grant at the resource AS. Discovery uses `authorization_grant_profiles_supported`,
  which contains `urn:ietf:params:oauth:grant-profile:id-jag`.
- **D35.** The page does not use the words "cross-app access," and the equivalence is UNVERIFIED.
  [P]
  https://github.com/modelcontextprotocol/ext-auth/blob/main/specification/stable/enterprise-managed-authorization/2026-07-28/enterprise-managed-authorization.mdx
- **D36.** OAuth Client Credentials (stable, revision 2026-07-28) is for machine-to-machine use. It
  is pre-registered only, with no DCR, and uses private_key_jwt (RFC 7523, RECOMMENDED) or a client
  secret. [P]
  https://github.com/modelcontextprotocol/ext-auth/blob/main/specification/stable/oauth-client-credentials/2026-07-28/oauth-client-credentials.mdx
  .
- **D37.** The extension repository is at https://github.com/modelcontextprotocol/ext-auth [P].
- **D38.** DPoP is not in the MCP spec. SEP-1932, the DPoP profile for MCP, is an open draft
  extension PR opened 2025-12-05 and still unmerged, with activity into November 2026. [P]
  https://github.com/modelcontextprotocol/modelcontextprotocol/pull/1932 .
- **D39.** The TypeScript SDK 2.1.0 ships DPoP support ahead of the spec, which "doesn't mandate
  DPoP yet." This is from a secondary blog dated 2026-09-29. [P, secondary]
  https://workos.com/blog/mcp-sdk-dpop-and-scope-challenges
- **D40.** URL elicitation is for sensitive or third-party interactions that must NOT pass through
  the client. It is not for authorizing the client to the MCP server.
- **D41.** The MCP server acts as an OAuth client to the third party and stores those tokens bound
  to the user's identity.
- **D42.** Third-party tokens MUST NOT transit the client, and MUST NOT be used upstream with the
  client's token. [P] https://modelcontextprotocol.io/specification/2026-07-28/client/elicitation
- **D43.** URL elicitation client rules: show the full URL, get consent before opening it, do not
  prefetch, and open it in a way the client and the LLM cannot inspect. SFSafariViewController is
  acceptable, and WKWebView is not.
- **D44.** The client SHOULD highlight the domain and warn on punycode.
- **D45.** The server MUST NOT send pre-authenticated URLs or user secrets in the URL, and MUST
  verify that the user who opens the URL is the one who started the elicitation.
- **D46.** Under MRTR, `accept` only means consent, and the client retries the original request to
  learn the outcome. [P] https://modelcontextprotocol.io/specification/2026-07-28/client/elicitation
- **D47.** Design note from the research (not spec text): for an app with no identity server, the
  app is the OAuth public client. The best fit is CIMD where the AS supports it, which needs the app
  to host an HTTPS metadata document.
- **D48.** Otherwise use a pre-registered per-service client id, then DCR with application_type
  native, then a user-entered client id. Source: the MCP authorization pages in D13 to D24.
- **D49.** Design note: store tokens keyed by (AS issuer, resource URI). Validate `iss`, send
  `resource`, and never forward a token to a different MCP server or AS.
- **D50.** STDIO servers have no OAuth and take credentials from the environment, so the app must
  inject per-server secrets from the keystore into the child environment, ideally scoped. Source:
  the MCP pages in D13 and D28.

### A2A

- **D51.** The A2A spec page at https://a2a-protocol.org/latest/specification/ lists 1.0.0 as the
  "Latest Released Version." GitHub releases show v1.0.1 released 2026-05-28 (changelog dated
  2026-05-26), v1.0.0 released 2026-03-12 and v0.3.0 released 2025-07-30.
- **D52.** No v1.1 was seen.
- **D53.** Whether 1.0.1 changed the security text is UNVERIFIED. [P]
  https://github.com/a2aproject/A2A/releases
- **D54.** Agent card: `securitySchemes` (a map from name to SecurityScheme) and
  `securityRequirements` (an array, also per skill as `AgentSkill.securityRequirements`).
- **D55.** The spec text in sections 3.1.11 and 13.3 still says `AgentCard.security`, which is a
  naming inconsistency inside the document.
- **D56.** The AND and OR semantics come from a third-party source only: top-level entries are
  alternatives and schemes inside an entry are all required. This is UNVERIFIED from primary text.
  https://api7.ai/blog/a2a-protocol-gateway-layer
- **D57.** A SecurityScheme is exactly one of: API key (location query, header or cookie, and a
  name), HTTP auth (scheme and bearerFormat), OAuth2 (flows, with optional oauth2MetadataUrl),
  OpenID Connect (openIdConnectUrl), or mutual TLS (section 4.5).
- **D58.** OAuth flows are authorizationCode (with `pkceRequired`, where PKCE SHOULD be used for
  public clients), clientCredentials and deviceCode. Implicit and password are deprecated. [P]
  https://a2a-protocol.org/latest/specification/
- **D59.** A2A section 7: TLS is required in production, with TLS 1.3 or newer SHOULD be used. The
  server MUST authenticate every request. Credentials are obtained out-of-band. Authorization is
  implementation-specific. [P] https://a2a-protocol.org/latest/specification/
- **D60.** Extended card: `GetExtendedAgentCard` applies only if `capabilities.extendedAgentCard` is
  true. Before 1.0 it was the top-level `supportsExtendedAgentCard`.
- **D61.** The extended card MUST require authentication with schemes listed in the public card, MAY
  vary by caller, and SHOULD NOT hold secrets.
- **D62.** The client SHOULD replace its cached public card for the session. Errors:
  UnsupportedOperationError and ExtendedAgentCardNotConfiguredError. [P, section 13.3]
  https://a2a-protocol.org/latest/specification/
- **D63.** Card signing (section 8.4, optional): a JWS under RFC 7515 over the JCS (RFC 8785)
  canonical card, with the `signatures` field excluded. The protected header MUST have `alg` and
  `kid`, and MAY have `jku`.
- **D64.** Clients MUST canonicalize and then verify, SHOULD verify at least one signature, SHOULD
  fetch keys over HTTPS, and MUST NOT use expired or revoked keys. The spec does not say who is a
  trusted signer, so the trust policy is the app's job. [P]
  https://a2a-protocol.org/latest/specification/
- **D65.** In-task auth (section 7.6): the agent MUST use a Task and set `TASK_STATE_AUTH_REQUIRED`
  with a status message. Credentials are out-of-band unless negotiated in-band. In-band credentials
  SHOULD be bound to the agent and encrypted.
- **D66.** The agent MUST NOT treat the transition itself as authorization.
- **D67.** A credential from this state is not assumed to authorize later messages. [P]
  https://a2a-protocol.org/latest/specification/
- **D68.** Push notifications (sections 13.2 and 4.3.2): `PushNotificationConfig.authentication`
  holds {scheme, credentials}. The agent MUST put the credentials in the webhook Authorization
  header. The client MUST validate them.
- **D69.** The agent SHOULD validate webhook URLs against SSRF. Tokens are single-purpose and
  rotated. [P] https://a2a-protocol.org/latest/specification/
- **D70.** Governance: the notes say the spec moved to the Agentic AI Foundation, per a blog link
  dated 2026/08/27. The details are UNVERIFIED, and the notes give no URL for this entry.
- **D71.** Design note: A2A has no registration or discovery of the app as a client. The app is a
  plain OAuth or API-key client of each agent, per that agent's card. The card's scheme list tells
  the UI which auth flow to start.
- **D72.** A card fetched over TLS from the agent's URL is only as trusted as DNS and TLS, unless it
  is signed.

### OAuth for native apps

- **D73.** RFC 8252 (BCP 212, October 2017) [P] https://www.rfc-editor.org/rfc/rfc8252.html .
  Clients MUST use an external user agent, preferably the system browser, and MUST NOT embed
  webviews (sections 5 and 8.12).
- **D74.** Redirect options (section 7) are a private-use scheme, a claimed https URL, or a loopback
  IP literal (`127.0.0.1` or `[::1]`) with an ephemeral port, which the AS MUST accept on any port.
  `localhost` is NOT RECOMMENDED (section 8.3).
- **D75.** PKCE MUST be used by public native clients, and state SHOULD be used.
- **D76.** Embedded client secrets prove nothing (section 8.5). A claimed https redirect gives
  OS-verified app identity (section 7.2), while loopback and custom schemes do not.
- **D77.** The MCP rule is that redirects must be localhost or HTTPS (see D28). A strict reading of
  that excludes private-use custom schemes. The CIMD example uses http://127.0.0.1 and localhost.
- **D78.** RFC 8252 says `localhost` is not recommended, so the two rules differ on this point.
- **D79.** OAuth 2.1 is draft-ietf-oauth-v2-1-17, dated 2026-10-31. It is an active WG
  Internet-Draft with IESG state "I-D Exists." The WG milestone is to submit to the IESG in December
  2026, and the draft expires 2027-05-04.
- **D80.** It would obsolete RFC 6749 and 6750. It is not an RFC. [P]
  https://datatracker.ietf.org/doc/draft-ietf-oauth-v2-1/ .
- **D81.** MCP's 2026-07-28 revision cites draft-13.
- **D82.** macOS and iOS: ASWebAuthenticationSession is the system API. A custom-scheme callback
  works from iOS 12 and macOS 10.15.
- **D83.** An HTTPS (universal link) callback uses `Callback.https(host:path:)`, from iOS 17.4 and
  macOS 14.4, and needs Associated Domains and an AASA file.
- **D84.** The notes report flakiness and simulator differences. `prefersEphemeralWebBrowserSession`
  controls cookie sharing with Safari. Apple's own doc text could not be retrieved, only its title,
  so this is UNVERIFIED primary. Sources: https://developer.apple.com/forums/thread/769749 ,
  https://developer.apple.com/forums/thread/805416 and
  https://support.auth0.com/center/s/article/auth0-https-scheme-does-not-work-for-ios-universal-link
  .
- **D85.** MCP's elicitation spec says SFSafariViewController is acceptable and WKWebView is not
  (see D43).
- **D86.** Android: AppAuth-Android follows RFC 8252. It prefers Custom Tabs and explicitly does not
  support WebView. Redirects use a custom scheme (all versions) or App Links (API 23 and up). PKCE
  is supported. [P] https://github.com/openid/AppAuth-Android .
- **D87.** Android's own auth page covers only Credential Manager and AuthorizationClient, for
  Google accounts, and says nothing about generic OAuth. [P]
  https://developer.android.com/training/id-auth/authenticate
- **D88.** Windows: MSAL.NET docs, from a secondary search result, say the browser path opens the
  default browser and redirects to a loopback listener.
- **D89.** A custom scheme in the browser flow errors with "only loopback redirect URIs supported"
  (GitHub issue 3821, UWP). The WAM broker is the alternative.
- **D90.** WebAuthenticationBroker is the older UWP API. Source:
  https://learn.microsoft.com/entra/msal/dotnet/acquiring-tokens/desktop-mobile/wam . Microsoft's
  WebAuthenticationBroker page returned 404.
- **D91.** For a generic app, the notes recommend the system browser with an
  `http://127.0.0.1:<ephemeral>` loopback.
- **D92.** DPoP (RFC 9449) sender-constrains access and refresh tokens to a client key. It is not in
  the MCP core (see D38). Adoption by major providers is not confirmed beyond the MCP TypeScript SDK
  (UNVERIFIED).
- **D93.** A keystore-held DPoP key fits the goal of keeping tokens in the keystore, but only where
  the server supports DPoP.
- **D94.** The notes say DPoP does not help if an attacker runs code in the client (RFC 9449 section
  11.4, from a search snippet, UNVERIFIED primary).

### Untrusted tool code without Docker

- **D95.** macOS, sandbox-exec and Seatbelt: the man page says DEPRECATED, and the whole sandbox.h
  is deprecated.
- **D96.** The SBPL language is never publicly documented, but it still works and is widely used by
  Chrome (historically, per Chromium source comments from 2016 and 2017), Codex CLI, Cursor, Gemini
  CLI and Claude Code.
- **D97.** The Cursor blog says it is "deprecated but still used by third-party apps like Chrome."
  [P] https://cursor.com/blog/agent-sandboxing (2026-02-18). A forum thread at
  https://developer.apple.com/forums/thread/777509 is a snippet (UNVERIFIED).
- **D98.** No Apple-supported replacement was found.
- **D99.** The risk is that it could break in any macOS release.
- **D100.** macOS App Sandbox is entitlement-based and needs signed binaries. A child can inherit it
  through `com.apple.security.inherit`. Cursor rejected it because every binary the agent runs would
  need to be signed. [P] https://cursor.com/blog/agent-sandboxing .
- **D101.** Apple's doc text was not retrievable, so these details are UNVERIFIED (recalled).
- **D102.** Virtualization.framework and `container`: https://github.com/apple/container [P] runs
  Linux containers as lightweight VMs on Apple silicon, on macOS 26 or later, under Apache-2.0,
- **D103.** "under active development." It runs only Linux binaries, as Cursor also notes.
- **D104.** It is usable only if a VM or Linux is acceptable, and not for native macOS tools.
- **D105.** Network Extension content filters and Endpoint Security need a system extension,
  entitlements, user approval and a Developer ID flow. Forum reports describe problems on macOS 26
  (https://developer.apple.com/forums/thread/820254).
- **D106.** The Endpoint Security entitlement needs an Apple grant.
- **D107.** The notes call this too heavy for an app-level sandbox, and UNVERIFIED beyond the forum
  threads.
- **D108.** macOS Seatbelt egress control: the profile permits network only to a localhost proxy
  port, as srt does (see D118).
- **D109.** Linux, Landlock [P] https://docs.kernel.org/userspace-api/landlock.html : unprivileged
  self-restriction. Filesystem rules since 5.13 (ABI 1). TCP bind and connect by port from ABI 4.
  Scoped signals and abstract unix sockets from ABI 6.
- **D110.** UDP from ABI 10. The page lists up to ABI 12.
- **D111.** Limits: no DNS or domain filtering, several syscalls (stat, chmod and others) are not
  restrictable, /proc fds are a gap, chroot is not denied, and no_new_privs must be set.
- **D112.** Linux, bubblewrap, seccomp and user namespaces are used by Claude Code (through srt) and
  Codex. They need unprivileged user namespaces. Ubuntu 24.04 needs an AppArmor adjustment,
  according to the srt and Codex docs.
- **D113.** Nested user namespaces are disabled by default because they weaken guarantees.
- **D114.** The notes give no URL for this entry.
- **D115.** Cursor uses Landlock and seccomp directly, with bwrap as a fallback. The notes give no
  separate link for this bullet. The Cursor source is https://cursor.com/blog/agent-sandboxing [P].
- **D116.** Windows, AppContainer (low-box token plus capability SIDs) isolates files, registry,
  network, process, window and device access. Network access is granted through capabilities such as
  internetClient, so granularity is per capability, not per domain.
- **D117.** It needs ACLs on any folder it must touch. [P]
  https://learn.microsoft.com/en-us/windows/win32/secauthz/appcontainer-isolation
- **D118.** Windows, restricted tokens and Job Objects: Job Objects limit memory, CPU, process count
  and UI, and can kill on close. They do NOT restrict file or network access. [P]
  https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects .
- **D119.** The notes read no primary doc for restricted tokens, so their behavior is UNVERIFIED
  beyond the srt use ("write-restricted token").
- **D120.** Windows Sandbox is a disposable Hyper-V VM. It needs Pro, Enterprise or Education (not
  Home), runs one instance at a time, has networking ON by default, and is ephemeral. The notes say
  it is not suitable as an embedded tool runner. [P]
  https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/
- **D121.** Microsoft Execution Containers (MXC) were announced at Build 2026 and reached GA per the
  Windows blog of 2026-10-07. [P]
  https://blogs.windows.com/windowsdeveloper/2026/10/07/microsoft-execution-containers-policy-driven-containment-for-ai-agents/
  .
- **D122.** The process container uses AppContainer on Windows, Seatbelt on macOS and Bubblewrap on
  Linux. It has one JSON policy schema and an SDK, a session container, WSLc, and an experimental
  MicroVM.
- **D123.** MXC's repository is github.com/microsoft/mxc. The notes did not open it, and its license
  is UNVERIFIED. Codex's native Windows sandbox recommends MXC when it is available, and needs
  Windows 11 24H2 build 26100.9278 or 25H2 build 26200.9278. [P]
  https://learn.chatgpt.com/docs/windows/windows-sandbox .
- **D124.** The notes suggest MXC is worth evaluating as a cross-platform sandbox policy layer.

### What coding agents ship

- **D125.** Claude Code [P] https://code.claude.com/docs/en/sandboxing : off by default. It uses
  Seatbelt on macOS and bubblewrap plus socat on Linux and WSL2. Native Windows is not supported. On
  Linux, the network runs in a separate netns with no route.
- **D126.** On macOS, Seatbelt allows only the proxy connection.
- **D127.** The proxy checks the hostname against `allowedDomains`, which starts empty. It does not
  terminate TLS by default (an experimental `tlsTerminate` option exists). Tools that ignore proxy
  env vars, such as ssh and DB drivers, cannot connect.
- **D128.** UDP, QUIC and ICMP are blocked.
- **D129.** Claude Code reads broadly by default, including ~/.ssh, unless `denyRead` is set. It
  protects paths such as .git/hooks, settings and shell rc files. It has an escape hatch,
  `dangerouslyDisableSandbox`, used on retry.
- **D130.** The notes list these weak spots: excludedCommands uses textual matching,
  `allowLocalBinding` on macOS exposes localhost, and `allowAllUnixSockets`,
  `allowNestedUserNamespaces` and `filesystem.disabled` exist.
- **D131.** Anthropic sandbox runtime `srt` [P]
  https://github.com/anthropic-experimental/sandbox-runtime : npm package
  `@anthropic-ai/sandbox-runtime`, Apache-2.0, labeled a "Beta Research Preview." It uses
  sandbox-exec with generated profiles on macOS.
- **D132.** On Linux it uses bwrap, with no netns network, unix-socket bridging, and seccomp that
  blocks unix sockets on x64 and arm64. It runs an HTTP proxy and a SOCKS5 proxy with allow and deny
  lists.
- **D133.** The proxy resolves the hostname and refuses loopback, link-local and metadata ranges,
  which resists DNS rebinding.
- **D134.** srt on Windows (alpha) uses a dedicated local user, a write-restricted token, a WFP
  filter to a loopback proxy and NTFS ACEs, and needs a one-time elevation.
- **D135.** srt's documented weaknesses: Windows Schannel TLS fails, system-service traffic is not
  covered, thread impersonation can evade the fence, Linux glob denyRead is expanded once at wrap
  time,
- **D136.** `enableWeakerNetworkIsolation` opens a trustd exfiltration path on macOS,
  `allowAppleEvents` breaks isolation, and 1:1-NAT addresses are not caught by the resolved-address
  check. Source: the srt URL in D131.
- **D137.** Domain fronting: the claim was not found in the part of the srt docs the notes could
  read (UNVERIFIED). Because the proxy filters on the requested hostname without TLS inspection,
  fronting or an SNI mismatch is plausible but unconfirmed.
- **D138.** OpenAI Codex CLI [P] https://learn.chatgpt.com/docs/sandboxing : Seatbelt on macOS, and
  bubblewrap on Linux with a bundled helper fallback, which needs unprivileged user namespaces. The
  page does not mention Landlock or seccomp.
- **D139.** WSL2 uses Linux, and native Windows has its own sandbox.
- **D140.** The modes are read-only, workspace-write (the default locally) and danger-full-access.
  Approval policies are on-request and never.
- **D141.** The default network posture for the local sandbox is not stated on that page, and is
  commonly said to be off (UNVERIFIED).
- **D142.** The Codex repository has crates named linux-sandbox, network-proxy, execpolicy, bwrap
  and mxc-sandbox. The notes read the names only.
- **D143.** Codex on Windows has three implementations, per
  https://learn.chatgpt.com/docs/windows/windows-sandbox [P]. MXC is recommended. The elevated mode
  uses dedicated low-privilege sandbox users and firewall rules.
- **D144.** The unelevated mode uses a restricted token and ACLs, with weaker network isolation and
  no deny-read.
- **D145.** Gemini CLI [P] https://geminicli.com/docs/cli/sandbox/ : macOS Seatbelt profiles, where
  permissive-open is the default, the `-proxied` variants route network through a proxy, and
  restrictive and strict variants exist.
- **D146.** It also supports Docker and Podman, gVisor (runsc) on Linux, LXC (experimental), and
  native Windows through icacls Low Mandatory Level, which persists after the session. The default
  Seatbelt profile allows broad reads and network.
- **D147.** The page says Gemini CLI was replaced by Antigravity CLI on 2026-06-18 for unpaid and
  Google One users.
- **D148.** Cursor [P] https://cursor.com/blog/agent-sandboxing (2026-02-18): macOS Seatbelt through
  sandbox-exec, with a dynamic policy that includes `.cursorignore`. Linux uses Landlock and
  seccomp, with an overlay that replaces ignored files.
- **D149.** Windows runs the Linux sandbox inside WSL2, since a native sandbox is lacking.
- **D150.** The blog does not describe the network egress mechanism. The docs mention a sandbox.json
  domain allowlist, from a secondary snippet (UNVERIFIED):
  https://cursor.com/docs/agent/tools/terminal.md
- **D151.** Common egress pattern without containers, as the notes synthesize it from the sources
  above (no single URL): deny direct network at the OS level (netns on Linux,
- **D152.** Seatbelt network-outbound allowing only a localhost proxy port on macOS, WFP or firewall
  on Windows), then force traffic through a local HTTP or SOCKS proxy that enforces a hostname
  allowlist and refuses private or link-local resolved IPs.
- **D153.** The gaps are hostname-only filtering with no TLS inspection, tools that ignore proxy
  variables fail closed, and UDP is unsupported.

### iOS

- **D154.** iOS apps cannot create child processes, since fork and posix_spawn fail. The source is
  an Apple forum answer, so the claim is UNVERIFIED primary:
  https://developer.apple.com/forums/thread/747499 . So there is no subprocess sandboxing on iOS at
  all.
- **D155.** App Store guideline 2.5.2 says apps may not "download, install, or execute code which
  introduces or changes features or functionality of the app," with a limited exception for
  educational code apps.
- **D156.** Guideline 4.7 allows HTML5 and JavaScript mini apps, chatbots and plug-ins, subject to
  4.7.1 to 4.7.5.
- **D157.** Guideline 4.7.2 forbids exposing native platform APIs to them without Apple's
  permission. [P] https://developer.apple.com/app-store/review/guidelines/
- **D158.** Implication stated in the notes: on iOS, tool code must be interpreted inside the app
  (JavaScript in JavaScriptCore or WKWebView, or a WebAssembly interpreter), or run remotely. JIT is
  restricted (UNVERIFIED, not retrieved).

### Identity between people without an identity provider

- **D159.** Signal safety numbers [P] https://signal.org/blog/safety-number-updates/ : 60 digits in
  12 groups of 5, made from a sorted concatenation of two 30-digit fingerprints.
- **D160.** The hash covers the identity public key plus the phone number, which binds the
  identifier and defeats unknown-key-share.
- **D161.** The result is one string per conversation, compared by in-person QR scan or out-of-band
  comparison, with advisory key-change notices.
- **D162.** Signal X3DH specification, sections 4.1 and 4.8 [P]
  https://signal.org/docs/specifications/x3dh/ (Revision 1, 2016-11-04): authentication through
  fingerprint or QR comparison is out of scope,
- **D163.** and the spec warns about identity misbinding and unknown key share.
- **D164.** Borrowing idea from the notes: a comparison code built from both public keys and both
  handles, sorted so both sides see the same string, plus a key-change warning and QR for in-person
  use.
- **D165.** did:key [P] https://w3c-ccg.github.io/did-key-spec/ (v0.9 draft, no date). The
  identifier is multibase of a multicodec prefix plus the public key. Ed25519 keys start with
  `z6Mk`. It resolves locally, with no registry.
- **D166.** It has no rotation and no revocation, so a compromise is permanent, and the spec
  discourages long-term use.
- **D167.** Borrowing idea: a self-certifying identifier format, which should not be the only
  long-lived identity, and should be paired with a rotation and recovery layer.
- **D168.** Matrix cross-signing: the spec v1.19 header was confirmed [P]
  https://spec.matrix.org/latest/client-server-api/ , but the cross-signing section was not
  retrievable.
- **D169.** From secondary sources (UNVERIFIED primary): a master key signs the self-signing key and
  the user-signing key.
- **D170.** The trust path runs from Alice's user-signing key to Bob's master key, to Bob's
  self-signing key, to Bob's device. Sources:
  https://jcg.re/blog/quick-overview-matrix-cross-signing/ and
  https://git.sudo.is/matrix/matrix-doc/src/branch/main/proposals/1756-cross-signing.md .
- **D171.** Borrowing idea: verify a person once through the master key, so their devices inherit
  trust.
- **D172.** The master key can be kept offline, and devices can be added or revoked without
  re-verifying people.
- **D173.** Keybase, from secondary search results: a public, append-only sigchain of signed links
  per user (follow, add key, proofs). Identity proofs are posted on external services. Merkle roots
  are periodically anchored to Bitcoin.
- **D174.** A proof asserts the whole sigchain, so keys can rotate. Sources:
  https://book.keybase.io/docs/server , https://book.keybase.io/docs/teams/details and the NCC audit
  at
  https://api.keybase.io/docs-assets/blog/NCC_Group_Keybase_KB2018_Public_Report_2019-02-27_v1.3.pdf
  .
- **D175.** The current product status was not checked (UNVERIFIED). Borrowing idea: a signed log of
  key changes, "I follow you at sigchain head X" statements that detect changes, and external proofs
  as optional evidence.
- **D176.** It needs a log host, which could be peer-to-peer.
- **D177.** Biscuit [P] https://doc.biscuitsec.org/reference/specifications : a public-key bearer
  token. Offline attenuation adds blocks signed with ephemeral keys. It has Datalog checks and
  policies, revocation ids per block, and sealed tokens.
- **D178.** Third-party blocks are signed with a third party's key (external signatures need Datalog
  v3.2 or later, and v5 or later for external signatures), and `trusting` annotations control whose
  facts count.
- **D179.** The holder gets the block through a request and response exchange, so the third party
  never sees the full token.
- **D180.** Datalog goes up to v3.3. The page has no spec version number.
- **D181.** Borrowing idea: delegable capability tokens that a user attenuates offline for a peer or
  agent, and a third-party block as a vouching signature from a contact or authority the verifier
  trusts by key.

### Device attestation without an identity server

- **D182.** Apple App Attest and DeviceCheck prove that a key lives in a Secure Enclave on a genuine
  Apple device and was generated by an unmodified app instance. The App ID hash is Team ID plus
  bundle ID.
- **D183.** The attestation chains to Apple's App Attest root and can be verified offline by your
  own server.
- **D184.** Generating it needs a network call to Apple at attest time. The optional risk-metric or
  receipt query is an Apple server API call. A per-key counter in assertions blocks replay. Mac app
  availability is UNVERIFIED.
- **D185.** The Apple doc text was not retrievable, so these details are recalled and UNVERIFIED:
  https://developer.apple.com/documentation/devicecheck/establishing-your-app-s-integrity
- **D186.** Android Key Attestation [P]
  https://developer.android.com/privacy-and-security/security-key-attestation : an X.509 chain runs
  from the hardware keystore to Google's hardware attestation roots.
- **D187.** A new root signs chains from 2026-02-01, and RKP is mandatory on Android 16 launch
  devices.
- **D188.** It proves the key is in a TEE or StrongBox, and the extension carries verified boot,
  patch and app-signing data. Verification can be offline on your own server, not on the device.
- **D189.** Revocation status needs Google's CRL at
  https://android.googleapis.com/attestation/status , which is cacheable through Cache-Control.
- **D190.** No per-request Google call is needed at attest time.
- **D191.** Play Integrity API [P] https://developer.android.com/google/play/integrity/overview :
  verdicts for app integrity, device integrity (BASIC, DEVICE or STRONG), account licensing and
  optional risk signals. It requires Google Play.
- **D192.** The token comes from Play services and is checked through Google, which is a vendor
  network call, with a default quota of 10,000 requests per day. It is not a sole anti-abuse
  measure, and verdicts should not be cached.
- **D193.** Sideloaded or non-Play builds fail.
- **D194.** TPM 2.0 attestation (secondary sources, UNVERIFIED primary): an AK-signed PCR quote plus
  the TCG event log. The EK certificate lets the verifier tie the AK to a genuine TPM.
- **D195.** The verifier must check the EK certificate itself, because the AK is not signed by the
  EK.
- **D196.** Verification can be fully offline against manufacturer CA roots, with no vendor call. It
  proves measured boot state, not app identity.
- **D197.** A forged-event-log weakness is documented for Microsoft DHA:
  https://google.github.io/security-research/pocs/bios/tpm-carte-blanche/writeup.html .
- **D198.** Also:
  https://git.broken-by-design.fr/fmaury/tpm.dev.tutorials/raw/branch/master/Attestation/README.md
- **D199.** Design note from the notes: all three prove that a key is hardware-bound in a genuine
  device running an unmodified app. They can bind a user's device key to a hardware root without an
  identity provider. But vendor roots become a trust dependency.
- **D200.** Play Integrity and App Attest both need vendor calls.
- **D201.** Desktop coverage (macOS and Windows) is weak, apart from TPM.

## E. Unverified or open items

Each item names the reference it comes from. Items are grouped roughly by section.

- **E1.** Whether the deployed Keycloak returns the nonce in refresh-token ID tokens. Plan phase 0
  is meant to confirm it. Tasks 36-37 do not record the result (`spec:352-354`, `plan:111-112`,
  `tasks:36-37`).
- **E2.** Whether a mesh can exist without an identity provider is open. `spec:886` says a new
  device can join unattested. `roster.md:156` and `tasks:26` say creating a mesh needs one
  attestation. `spec:1014-1016` asks the question and leaves it open.
- **E3.** What happens to a device's Keycloak session tokens when the issuer's key rotates, and to
  an attestation once the issuer's signing keys are gone, is not stated.
- **E4.** The roster issuer record keeps keys, but rotation is not described (`roster.md:72`,
  `spec:348-360`).
- **E5.** The LETS policy input has no attested or unattested field. The smaller allowance for
  unattested devices in `roster.md:156` has no contract behind it (`policy.md:81-83`).
- **E6.** The 30-day re-attestation interval is a starting value. The notes did not record whether
  the owner has confirmed it (`spec:348-351`, `spec:1017`).
- **E7.** Which institutions sign PHI approvals, and by what process, is open (`spec:1029-1031`,
  `tasks:29-30`).
- **E8.** How an institution issues or revokes an approval, how an approval reaches an offline
  device, and how a model endpoint is named beyond the `subject` field are not stated
  (`roster.md:162-164`, `spec:561-564`).
- **E9.** The effect input's `endpoint_phi_approved` boolean has no contract that says how it is
  established (`labels.md:36`).
- **E10.** What happens to PHI content already on a device when its approval is revoked, and whether
  a PHI epoch is rotated on revocation as device removal is, is not stated. `keys.md:32` covers
  device removal only (`roster.md:62`, `keys.md:32`).
- **E11.** The device-side PHI classifier is not defined, and the notes disagree about it.
  `phi_gate.py` uses Presidio, according to the log text (inferred,
  `AstralDeep/backend/personalization/phi_gate.py:90`).
- **E12.** Issue-triage item 100 says a local filter is needed to run PHI checks without Presidio
  (`it:100`).
- **E13.** The spec says the classifier runs on nodes with Presidio (`spec:533`). See also E42.
- **E14.** Which PHI classifier checks run on a device, and which on a node, beyond "Presidio on
  nodes," is not stated. Whether the device filter's accuracy is measured against the node's filter
  is not stated (`spec:533`, `spec:166`).
- **E15.** Whether `spec:898`'s claim that PHI rules "require a known operator, an agreement and an
  audit trail" has a source is not stated. The notes cite no source (`spec:898`).
- **E16.** The descriptor that carries PHI approval for an institution node has no contract in
  `contracts/`. Its contents are not stated (`plan:330-331`).
- **E17.** How institution owner partitioning works (the key, the storage layout, the authorization
  between owners), how an institution is onboarded, which agreement is required, which gates an
  institution sets,
- **E18.** how many owners it may serve, and how it is audited beyond "an audit trail," are not
  stated (`spec:253-255`, `spec:898`, `plan:330-332`).
- **E19.** Whether the production executor runs in Docker, Podman, a VM or a native process, which
  container images are used, what orchestration applies, and what the Vessel container's base image
  is, are not stated (`spec:931`, `tasks:182`).
- **E20.** What the sandbox is built on (process, container, VM or Seatbelt), its network policy,
  whether egress is a proxy, an allow list or a separate container,
- **E21.** and how Vessel's sandbox and egress are verified against tickets on a device that is not
  Vessel, are not stated (`spec:931`, `spec:966`).
- **E22.** The egress policy itself is not stated (`rc:116`, `lih:88`).
- **E23.** Whether a sandboxed node's executor on the Mac uses the Seatbelt path is not named in the
  105 documents (`spec:164`, `spec:931`).
- **E24.** The container inventory did not read the 105 `research.md` or `tasks.md`
  (`AstralDeep/specs/105-decentralized-personal-mesh/`).
- **E25.** The Vessel executor, node and tools internals were read by grep only, and were not read
  in full (AstralVessel `executor.py`, `node.py`, `tools.py`).
- **E26.** The Windows client's sandboxing was not searched in depth. The Vessel worktrees
  (AstralVessel-wt and others) were not inventoried. The `.claude/worktrees/spec-106/` copies of the
  105 documents were not checked.
- **E27.** Deep's `backend/shared/net_guard.py` was not read in detail. `external_http.py` uses it
  at lines 91-107.
- **E28.** Whether the MCP OAuth tokens and OAuth refresh tokens use the `key.credential` sync path
  is not stated (`keys.md:69`, `spec:948`).
- **E29.** How a remote MCP server's authorization server is trusted, and how OAuth flow, token
  refresh and token expiry are handled on each device, are not stated (`spec:445-447`).
- **E30.** The place where a tool-definition pin is stored, who approves it on a device, and what a
  pin looks like for a remote server that reorders fields, are not stated.
- **E31.** The hash covers only name, description, input_schema and annotations (`tools.md:24`,
  `tools.md:32`).
- **E32.** How an MCP tool's result label is computed beyond the untrusted default is not stated.
  `rc:76` asks to preserve input confidentiality in result labels (`spec:532`, `rc:76`).
- **E33.** An allow list, deny list, size limit and TLS policy for remote MCP and A2A calls are not
  stated in spec 105. The notes do not confirm that the approved egress path
  `backend/shared/external_http.py` applies to them (`plan:207`, `spec:900`).
- **E34.** Which key signs an A2A agent card, how a card's signer is trusted, and how a card is
  revoked, are not stated (`spec:586`, `spec:865`, `plan:302`).
- **E35.** Whether A2A 1.0.1 changed the security text is UNVERIFIED (D51). The AND and OR semantics
  of `securityRequirements` are UNVERIFIED from primary text (D54). The governance details and their
  URL are UNVERIFIED, and the note gives no URL (D70).
- **E36.** The MCP revision after 2026-07-28 was not checked. Whether a newer draft is open is
  UNVERIFIED (D1). The enterprise extension's equivalence to "cross-app access" is UNVERIFIED (D33).
- **E37.** DPoP adoption by major providers is UNVERIFIED beyond the MCP TypeScript SDK (D38, D92).
  The claim that DPoP does not help when an attacker runs code in the client rests on a search
  snippet (D92).
- **E38.** Apple's doc pages for ASWebAuthenticationSession, App Sandbox and App Attest returned
  only titles.
- **E39.** The details of `Callback.https`, `prefersEphemeralWebBrowserSession`, App Sandbox
  inheritance and App Attest were recalled or taken from forums (D82, D100, D182). App Attest
  availability on Mac is UNVERIFIED (D182).
- **E40.** The MXC repository's license is UNVERIFIED (D123). Restricted-token behavior on Windows
  is UNVERIFIED (D118). TPM primary documents were not read (D194). Keybase's current status was not
  checked (D173).
- **E41.** The Matrix cross-signing section was not read from the spec (D168).
- **E42.** Tension: the brief's on-device PHI design uses `phi_gate.py` as the floor, while the
  notes say `phi_gate.py` relies on Presidio and that device-side PHI checks need a local filter
  that is not yet qualified (A73, B114). This needs an owner decision.
- **E43.** Tension: the realm advertises RS256, ES256 and EdDSA (`tasks:36-37`), while roster
  verification step 6 allows only RS256 and ES256 (`roster.md:143-150`).
- **E44.** Tension: RFC 8252 says `localhost` is not recommended for loopback redirects, while the
  MCP rule allows localhost and the CIMD example uses it (D73, D77).
- **E45.** Drift: the MCP 2026-07-28 revision cites OAuth 2.1 draft-13, while the current IETF draft
  is -17 (D13, D79). This is a version difference between sources, not a contradiction within one
  source.
- **E46.** The credential_manager fallback: the notes report a possible issue, unverified.
  `CREDENTIAL_ENCRYPTION_KEY` falls back to reading or generating `.credential_key` in the data
  directory when unset.
- **E47.** The notes did not confirm an ASTRAL_ENV gate on that fallback, and they flag it for
  review against the fail-closed rule (B100).
- **E48.** The exact Keycloak token-exchange request body in
  `AstralDeep/backend/orchestrator/delegation.py` was not read. The call site is at line 112 (B48).
- **E49.** Whether AstralKernel is loaded by AstralDeep (a Python binding or `astral-net`) was not
  verified. The handoff reports mesh creation with attestation implemented in the kernel facade
  (B73, B80, `lih:115`).
- **E50.** Whether `remote_mcp_connectors` writes tokens through `credential_manager` is inferred
  from the `orch.credential_manager` reference at line 158, and was not read (B85).
- **E51.** Whether the actor chain is carried by an `act` claim was not verified (B59).
- **E52.** No general stdio MCP client exists (inferred from a grep, B87). External A2A agents'
  security schemes are not modeled (inferred, B97). The unattested state is not implemented in Deep
  (inferred, B71).
- **E53.** The scope source of truth is `is_tool_allowed`, consistent with the team memory note on
  empty-scope derivation (inferred, B55). The `clinical_extraction.py` and `evidence_context.py` key
  handling is decrypt on use (inferred, B112).
- **E54.** Mesh sharing beyond the listed files is inferred (B124).
- **E55.** The LETS checkout was searched only as part of the global Keycloak counts, and was not
  inventoried in detail (the not-verified list of the identity inventory).
- **E56.** The Mac app's shipped default: whether enforce mode is the shipped default is not
  established by the files read. The default config is shadow mode (C97, C115, C216).
- **E57.** The Mac app's packaging and supervision of Deep's node: the packaging method, and the
  supervision method, are not stated (C78, C81).
- **E58.** Speech on the Mac node is not stated. Local weights on the desktop are not stated (C207,
  C203).
- **E59.** The per-agent sandbox image and its replacement are not stated (C212). The replacement
  for native dependencies is not stated (C196, C197, C218).
- **E60.** Redis and an object store were not checked in detail. AstralPlane's blob stores were not
  read (C213).
- **E61.** The compose files `docker-compose.staging.yml`, `docker-compose.voice-integration.yml`
  and `deploy/lets/docker-compose.lets.yml` were not read in detail (C166).
- **E62.** Whether the Vessel native sandbox is available on Windows with Docker Desktop is
  unverified. The docs mark Windows as unverified (C40).
- **E63.** Whether the probes and the CI smoke test need containers, and whether isolation claims
  are exercised only inside containers, are inferences (C59).
- **E64.** Whether the Mac app's governed commands use only the Seatbelt path is inferred from code
  reads (C115).
- **E65.** Whether Deep's bwrap pilot is Linux only is inferred (C129). Its vendored `apply-seccomp`
  helper is not tracked in git (C125).
- **E66.** Two egress implementations and two Linux isolation forms exist, per the inferences in
  C136 and C137. A native design needs one of each, which the notes count as an inference, not a
  finding.
- **E67.** Codex's default network posture for the local sandbox is UNVERIFIED (D138). Cursor's
  network egress mechanism is not described in its blog (D148). The Cursor sandbox.json allowlist is
  UNVERIFIED (D148).
- **E68.** Domain fronting in srt is UNVERIFIED (D137).
- **E69.** Missing source URLs: D70 (A2A governance), D112 (bubblewrap, seccomp and user
  namespaces), D151 (common egress pattern) and D115 (the Cursor bullet, linked to the Cursor blog
  above) have no URL recorded in the notes.
- **E70.** iOS JIT restrictions were not retrieved (D158). Whether iOS forks fail was taken from a
  forum answer (D154).
- **E71.** How another person is identified and verified, including their identity key and how their
  device enrolls, is not stated (A103).
- **E72.** What a shared chat includes (events, surface and tool state, run state, memory), whether
  a recipient gets feed access, and whether a recipient belongs to any key group, are not stated
  (A101, `keys.md:20-23`).
- **E73.** How a grant to a person is revoked, and what happens when a grantee has already read a
  chat, are not stated (A101, A102).
- **E74.** Which PHI checks need a large model, what the threshold is, and what the node does with
  the result, are not stated (A92).
- **E75.** Status not recorded in the handoff: `reattest`. The owner's sign-off on section 17 rule
  changes is not recorded (`plan:126-127`). Hardware-key custody is not recorded as merged
  (`plan:146`).
- **E76.** The presence-key-signed consent is not recorded as done (`plan:209`).
- **E77.** Refusal of tickets from a removed device is not recorded (`plan:224`). The phase 1 exit
  criteria are not recorded as passed (`plan:151-156`).
- **E78.** Separately, the handoff lists these as not done: the restore marker after pairing join,
  the recovery-word add flow, `preview_pairing`, `record_spoken`, and the mailbox client in the sync
  loop (`lih:115`).
- **E79.** How a personal node with no reachable Keycloak obtains its first issuer is not stated
  (C200, `how-it-works.md`).
