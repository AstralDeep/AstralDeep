# Spec 105: Astral without a central server

**Date:** 2026-11-11. **Status:** design for owner review. Nothing here is implemented, tested or
deployed. **Companions:** [plan.md](plan.md) (phases and exit criteria), [research.md](research.md)
(evidence, with file and source references), [issue-triage.md](issue-triage.md) (open issues and pull
requests this would close).

**Scope:** the five core repositories (AstralDeep, AstralProjection, AstralPlane, AstralPrimitives,
LETS) and one proposed new component. The two Desktop documents on decentralization and stack
integration were read as background only. Section 18 says where this design departs from them.

---

## 1. Summary

Today every Astral client is a thin window onto one orchestrator at `sandbox.ai.uky.edu`. If that host
is unreachable, no client can show a chat, run a turn, speak, or adapt a layout.

This spec replaces the central server with a **personal mesh**: the set of devices one person has
enrolled. Each device holds an encrypted, signed copy of that person's event history and can keep
working alone. Devices exchange events whenever they can reach each other, so a chat started on an
iPhone appears on the Mac, and the reverse.

Five ideas carry the design:

1. **A signed event log replaces the server database as the thing devices share.** Each device appends
   to its own log. Every other device copies it. Chats, generated UI, permissions and audit are all
   events.
2. **One shared kernel runs in every client.** It holds the log, sync, the security gates, and the
   agent turn loop, so the same deterministic code decides on a phone, a Mac and a server.
3. **Authority travels with the request.** Each effect carries a ticket that any device can verify
   with no network call. Keycloak proves who a person is when a device is enrolled, and is not asked
   again for each request. LETS bounds what a cut-off device may do.
4. **The device adapts and renders UI for itself.** The log stores one device-neutral description of
   a generated surface. Each device fits it to its own screen, watch face or voice.
5. **A few shared services remain, and they are blind.** A relay, a mailbox and a push gateway carry
   ciphertext between devices. Section 15 lists everything that stays centralized and why.

The existing orchestrator does not disappear. It becomes a **node**: an optional always-on member of a
mesh that runs the heavy agents, and the form an institution deploys.

---

## 2. Goals, non-goals, anti-goals

**Goals**

- No client depends on `sandbox.ai.uky.edu`.
- A chat on any of iPhone, macOS, Windows or Android appears on the owner's other devices.
- Every client tolerates being cut off: it can read history, accept input, and run turns within a
  bound, then reconcile when it reconnects.
- The security properties in `AGENTS.md` hold on every device, and get stronger where the research
  shows how.
- Fewer failures from model timeouts and malformed tool arguments.
- Long-running and self-improving loops can run in a client.
- Conversational voice with interruption, running on the device where the hardware allows.
- Standard MCP and A2A stay first-class.
- The exam paper's two ideas (agent security and generative UI) are kept and advanced by methods the
  paper did not use.

**Non-goals**

- Migrating existing sandbox data. The owner chose a clean break.
- A web client in the first phases. It returns when a UofL host exists.
- Sharing between different people beyond an explicit grant. The mesh is one person's.
- A distributed database, a blockchain, or a public peer-to-peer network.
- Byzantine fault tolerance between a person's own devices.

**Anti-goals: the design has failed if any of these happens**

- A device silently does something its owner did not authorize because it was offline.
- A consent prompt is drawn from model output.
- Two devices that worked apart produce one interleaved conversation that neither had.
- A shared service can read content.
- A gate is weakened or skipped to make offline use convenient.

---

## 3. Decisions

### 3.1 Decided by the owner (2026-11-11)

| # | Decision |
|---|---|
| O1 | No web client for now. It returns when a VM is available at UofL. |
| O2 | Keep the Keycloak instance at `iam.ai.uky.edu` for now. It will also have to move (possibly to UofL), so the design must not depend on where it lives. |
| O3 | The personal mesh is the primary design. An institution may run a node. PHI stays on nodes that declare approval. Sharing between people is by explicit grant. |
| O4 | Clean break from existing sandbox accounts and history. |
| O5 | Documents live under AstralDeep `specs/`, on a local branch. GitHub is list-only. |
| O6 | Something must sync data across devices. Its shape was left to this design. Each client must tolerate being cut off, and LETS should supply that logic. |

### 3.2 Calls made in this design, for the owner to check

| # | Call | Alternative that lost, and why |
|---|---|---|
| C1 | A phone works by itself. The owner did not answer this directly. "each client needs to be tolerant of being cut off" was read as yes. | Phone as a thin client to a home device: simpler, but the phone is useless when the Mac is asleep. |
| C2 | One shared kernel, written in Rust, bound into Swift, Kotlin and Python. | Three native implementations against shared test vectors: no new toolchain, but three copies of the cryptography, sync and gate code to keep identical. The sync, transport and capability libraries this design uses are Rust already. Phase 0 tests this call with a build spike before anything depends on it. |
| C3 | Sync is signed per-device append-only logs, not a merge-anything document library. | Automerge with Keyhive: built for this problem, but its access-control layer says not to use it in production. |
| C4 | Encryption keys are per-epoch keys wrapped to each device and recorded in the membership list. | Messaging Layer Security: standard, but it needs all membership changes in one agreed order, which a partitioned mesh cannot promise, and the decentralized variants are drafts. |
| C5 | Transport is iroh (connections addressed by device key, direct when possible, relayed when not). | libp2p's main funder withdrew in 2026, by one report. A VPN-style overlay adds a coordination server. |
| C6 | Capability tokens use Biscuit (public-key tokens a holder can narrow offline). | UCAN has no Swift or Kotlin binding and is still a release candidate. RFC 8693 needs a server round trip per hop. |
| C7 | Device-side gates are on by default, including policy and flow labels, which are off by default on the server today. | Keeping today's defaults would leave a device with fewer active controls than the server it replaces. |
| C8 | Voice runs on the device. This reverses the feature 075 decision to keep the server authoritative and not ship speech models to clients. | Keeping server voice keeps a server. |
| C9 | macOS is built first, then iOS, then Android and Windows. A phase is finished when macOS and iOS pass. Android and Windows keep working as thin clients of a node until their own port lands, so a node keeps serving thin clients throughout. | The owner's merge scope is macOS and web. Web is paused. The phone-and-Mac pair is the case that proves the design. Gating every phase on four clients would stall it. |
| C10 | Multi-owner hosting is not kept working as a second mode between phases 2 and 7. It returns as the institution profile. | Keeping the shared sandbox qualified through the whole transition doubles the work. Question 11 asks whether that is acceptable. |

Section 21 lists the questions only the owner can settle.

---

## 4. The picture

### 4.1 Words used in this spec

| Word | Meaning |
|---|---|
| Mesh | One person's enrolled devices and nodes. |
| Peer | Any member of a mesh. |
| Desktop peer | A Mac or Windows PC. It can run a full agent loop, local tools, a sandbox and larger local models. |
| Pocket peer | An iPhone or Android phone. It runs the same kernel with phone-safe tools and a small local model. |
| Satellite | A watch, or later a browser tab. It shows and speaks through a peer and holds no log of its own. |
| Node | An optional always-on peer with no screen: today's orchestrator, repackaged. |
| Kernel | The shared library inside every peer. |
| Event | One signed, encrypted record in a device's log. |
| Roster | The signed list of who is in the mesh. |
| Ticket | The proof that authorizes one effect. |
| Allowance | The bounded amount of authority a peer may spend in one period. |
| Label | A mark on an event saying how far its content can be trusted and where it may go. |

### 4.2 Topology

```
        one person's mesh
 ┌──────────────────────────────────────────────────────────────────────┐
 │                                                                      │
 │   iPhone ◀────── same Wi-Fi: direct ──────▶ Mac ◀──────▶ Windows PC  │
 │  (pocket)                                (desktop)        (desktop)  │
 │     ▲  ▲                                    ▲                        │
 │     │  └── watch (satellite)                │                        │
 │     │                                       ▼                        │
 │     │                          node (optional, always on)            │
 │     │                          orchestrator + AstralPlane            │
 └─────┼────────────────────────────────────────────────────────────────┘
       │ away from home: ciphertext only
       ▼
  blind relay ── blind mailbox ── push gateway          Keycloak
  (finds a path)  (holds events   (wakes a phone,       (proves identity when
                   for a peer      carries no content)   a device is enrolled)
                   that is off)
```

Every peer has the kernel, a local store, its own keys, and the owner's event log. No peer is
required to be online for another to work.

### 4.3 What each kind of peer can do alone

| Capability | Pocket peer | Desktop peer | Node |
|---|---|---|---|
| Read all synced history | Yes | Yes | Yes |
| Run a chat turn with a cloud model | Yes, if the internet is reachable | Yes | Yes |
| Run a chat turn with no network | Yes, with the on-device model, at lower quality | Yes, with a local model | Only if it hosts a model |
| Remote MCP tools | Yes | Yes | Yes |
| Shell, files, browser, computer use | No | Yes | In its sandbox |
| Heavy first-party agents (Python) | No. Asks a desktop peer or node | If it embeds the agent pack | Yes |
| PHI-labeled work | Only if the device is declared approved | Same | Yes, if declared approved |
| Long-running loop | Bounded by the operating system. Hands off or pauses | Yes | Yes |
| Voice with interruption | Yes | Yes | No |

When a pocket peer needs something only another peer can do, it writes a signed request event. The
request runs when a capable peer receives it, through that peer's own gates.

---

## 5. The event log

### 5.1 Why a log

Research found no ordered, replicable history in the product today: a chat is a mutable snapshot, live
steps go only to the socket that started the turn, and message ids are database serial numbers that
collide across devices ([research.md](research.md) A2). Sync therefore needs a new layer, and a clean
break allows one.

An append-only log of signed events is that layer. It suits this product for four reasons: chat and
tool activity is append-mostly. A signed chain is also the audit trail. A device that was offline only
has to send what it wrote. The kernel can rebuild all state by replaying events, which makes a
crashed or switched device resumable.

### 5.2 Shape of an event

```
header (signed)                      body (encrypted, addressed by its hash)
  feed      which device wrote it      the message text, tool arguments,
  seq       position in that feed      tool result, UI tree, and so on
  prev      hash of the feed's previous header
  after     hashes of events from other feeds this one follows
  clock     hybrid logical clock
  scope     a chat id, or "mesh" for settings and the roster
  type      what kind of event
  labels    trust and confidentiality (section 9)
  key       which content key encrypts the body (section 7.4)
  body      hash of the encrypted body
  sig       signature by the feed's key
```

- Each device writes one feed. A feed is a hash chain, so a gap or an edit is detectable.
- Ids are hashes, so they are unique across devices with no coordinator.
- A peer keeps every header and fetches bodies by policy: recent and pinned chats eagerly, old chats
  and attachments on demand. A phone does not need the whole history on disk.
- Attachments are encrypted, content-addressed chunks.
- Headers and bodies are both encrypted before they leave a device. A relay or mailbox files an event
  under its feed and sequence number and can read nothing else.
- For a PHI-labeled event, a peer without PHI approval receives the header only. It can still verify
  the chain, and it shows that the content is held on approved devices.

### 5.3 Event families

| Family | Examples | Merge rule |
|---|---|---|
| Chat | user message, assistant message, model request and response, tool proposed, tool authorized, tool result, turn start, turn end, turn outcome unknown | None needed: events are facts. Each turn names the turn it continues. |
| UI | put a surface, patch a component, a user action on a component | Patches apply in causal order. Concurrent patches to one property take the later clock. |
| Authority | grant, revoke, narrow a policy, the budget policy, consent, receipt | Revoke beats a concurrent grant. Narrowing applies at once. Widening needs a consent event. |
| Mesh | add a device, remove a device, new key epoch, stop | Remove beats a concurrent add. The stop counter only rises. |
| Run | executor lease taken or transferred, goal state, schedule | One executor per run. Taking over a run is always an owner action (section 11.1). |
| Learning | proposal, adoption, reversal | Adoption needs the gate in section 11.3. |
| Memory and settings | remember, supersede, set a preference | Last writer wins by clock, then by feed id. |

Three rules follow from the anti-goals:

- **State is a fold.** Nothing is edited in place. Any peer that has the same events computes the same
  state.
- **Branches, never interleaving.** If two devices each run a turn that continues the same earlier
  turn while apart, the chat has two branches and shows them as branches. The owner keeps both or
  continues one. This also gives fork, edit-and-resend and regenerate (Deep #542) for free. A message
  that was only queued offline, with no turn run, is not a branch: it is placed after the latest turn
  when the device reconnects.
- **Authority merges toward less.** Two devices that disagree about a permission end up with the
  narrower one.

Settings that merge by "last writer wins" depend on device clocks. A peer rejects an event whose clock
is more than a few minutes ahead of its own, so one device with a wrong clock cannot win every merge.

### 5.4 Storage

- **On devices:** SQLite inside the kernel, holding headers, bodies, derived views and keys. No
  device needs PostgreSQL.
- **Plane on SQLite (owner, 2026-11-12).** AstralPlane gains a SQLite backend beside PostgreSQL, so
  Deep's Python node runs with no database server: inside the Mac app and in a personal AstralVessel
  install. The research found that Plane's repositories embed PostgreSQL SQL ([research.md](research.md)
  A7), so this is a port of every repository and a separate migration registry for SQLite, tested
  against both backends with one contract suite. It starts from a clean schema at the current revision,
  because no data is migrated (O4).
- **PostgreSQL stays for institution nodes**, which serve many owners. There, Plane holds the events in
  new tables and keeps its existing tables as views derived from them.
- Spec 100's "one durable authority" stays true for each node, whichever backend it uses.

---

## 6. Sync

### 6.1 Protocol

Two peers compare, for each feed, the highest sequence number they hold, then send what the other
lacks. Because every feed is a chain, this is exact and needs no set-reconciliation machinery. Bodies
follow by policy.

**A restored device starts a new feed.** A phone restored from a backup would otherwise reuse sequence
numbers it had already used. The kernel keeps a marker that backups do not carry. When the marker is
missing it creates a new operations key, and so a new feed, and re-enrolls. If two different events
ever appear at the same place in one feed, peers keep both as evidence, stop accepting that feed, and
ask the owner to re-enroll the device.

**A shortened feed.** A device cannot prove that another device has withheld its newest events. What
peers can detect is a feed shorter than a checkpoint they have already countersigned (section 9.6).

### 6.2 Three paths, tried in this order

| Path | When | What carries it |
|---|---|---|
| Direct | Peers on one network, or a direct path can be opened across networks | A connection addressed by device key, found on the local network by Bonjour or Android NSD |
| Relayed | No direct path (about one network condition in ten, by the transport's own figures) | A relay that forwards encrypted packets and cannot read them |
| Mailbox | The peers are never online at the same time | A store that holds ciphertext events until the other peer collects them |

A relay or mailbox accepts a deposit or a fetch only when it is signed by a key on the mesh's roster.
It therefore learns the mesh's device public keys and when membership changes, and nothing about
content.

A node, if the owner has one, is the relay and mailbox for its own mesh. Otherwise a shared blind
service does the job (section 15). Until one exists, two phones-and-laptops that are never awake
together sync when they next are. The product must say so plainly.

### 6.3 Mobile reality

- iOS stops network activity when an app is backgrounded and offers no way to wake on a peer's
  connection. A phone syncs when opened, and when a content-free push wakes it briefly.
- Background pushes are low priority and limited to a few an hour, so push is a hint, never a
  guarantee.
- Android allows a foreground service for sync for six hours in twenty-four.

### 6.4 Honest state

Every client shows what it knows: synced, waiting for a named device, or last seen at a time. A request
sent to an absent peer is shown as waiting, never as done. This extends the rule spec 100 already set
for mesh presence.

---

## 7. Identity and devices

### 7.1 Keys on a device

| Key | Kept in | Used for |
|---|---|---|
| Identity key (P-256) | Secure Enclave, StrongBox or TPM. Cannot be exported | Proving the device is itself. Signing roster entries and log checkpoints |
| Presence key (P-256) | Same hardware, released by biometrics or a passcode for each use | Signing consent for one specific action |
| Operations key (Ed25519) | Keychain or keystore. Certified by the identity key. Rotated | Signing events, tickets and LETS records at speed |

Today the mesh key is a software seed on every native client ([research.md](research.md) A3). Moving
the identity and presence keys into hardware is part of this work (Projection #138, Deep #558).

### 7.2 The roster

The roster is the signed record of who is in the mesh. The first device creates it. The hash of its
first entry is the mesh id. Adding a device needs approval on an existing device, signed with that device's
presence key. Removing a device needs the same. Roster changes require biometrics, not a passcode,
wherever the hardware can enforce that, so that someone who has a device and its passcode cannot take
over the mesh. The recovery key (section 7.5) outranks any device.

Two devices can change the roster while apart, so the roster is a set of signed entries that each name
the entries they follow, not a single line. It merges by two rules. A removal beats an addition made
at the same time. An entry written by a device that has been removed is void unless the removal itself
came after it, so a stolen device cannot add an accomplice on its way out.

Pairing copies Signal's flow: the new device shows a QR code carrying its identity key. An existing
device scans it, approves, and sends the roster and the history keys over an encrypted channel.

### 7.3 What Keycloak does now

Keycloak proves who the person is **when a device is enrolled**, and again on a slow schedule. It is
not consulted per request.

- At enrollment the device signs in with PKCE as today, with one change: the request's nonce is a hash
  of the device's identity key and the mesh id. The ID token that comes back is therefore a statement,
  signed by Keycloak, that this person authorized this key. The roster stores it.
- Any peer or node can check that statement offline against the issuer's published keys.
- The mesh principal is the mesh id, with one or more attested `(issuer, subject)` pairs attached.
  Nothing is keyed on a bare `sub`.
- Re-attestation repeats the bound sign-in every 30 days when the issuer is reachable (a starting
  value). It is silent while the Keycloak session is alive. Otherwise the owner is asked to sign in.
  A device that cannot reach the issuer keeps working. An institution node may demand a fresh
  attestation and refuse otherwise.
- Keycloak 24 and later leave the nonce out of ID tokens issued by a refresh, so re-attestation is a new
  authorization request, not a refresh. Phase 0 confirms this against the deployed version and decides
  between that and a realm mapper.
- A device added while Keycloak is unreachable is marked unattested. It gets a smaller allowance and no
  access to institution nodes until it attests.

**Moving Keycloak.** When the realm moves to UofL, the roster gains an "accepted issuer" entry approved
on an existing device, and devices re-attest against the new issuer. Continuity rests on the mesh's own
keys, so no data is orphaned whether or not subjects are preserved.

This keeps the rule that Keycloak is the only source of user identity. What changes is that the proof
is carried by the device instead of being fetched for each request.

### 7.4 Encryption

- **Each chat has its own content key.** Event bodies are encrypted under the key of their scope, and
  the header names it. Content keys are wrapped under the epoch key of a **key group**.
- **There are two groups to start with.** The personal group is the owner's own devices and nodes. The
  PHI group is the members that hold a PHI approval (section 9.3). The key of a PHI-labeled scope is
  wrapped only to the PHI group, so a device outside it cannot decrypt PHI even if it obtains the
  ciphertext.
- **An institution's node and another person are never in the personal group.** They receive
  individual chat keys by grant, re-wrapped to them, and can read only those chats.
- Each group's epoch key is wrapped separately for each member and recorded in the roster.
- Removing a device starts a new epoch. The removed device cannot read anything written afterwards. It
  keeps what it had already synced. Nothing can take that back.
- A new device receives earlier epoch keys from the device that approved it, so it can read history.
  The owner can decline this for a device they trust less.
- Two devices may each start a new epoch while apart. Both keys are valid. Every event names the epoch
  it was encrypted under. Any member that holds an epoch key wraps it for a member that lacks it.

### 7.5 Loss, theft and recovery

| Event | What the owner does | What happens |
|---|---|---|
| Lost or stolen device, another device at hand | Remove it on the other device | A removal entry, a new epoch and a raised stop counter are written and pushed to the mailbox. Every peer and node that sees them refuses the lost device's tickets and feed. |
| Lost device, no other device online | Sign in on a new device with the recovery key | The new device enrolls, then removes the old one as above. |
| All devices lost | Recovery key | Restores membership and whatever history a node or mailbox still holds. Without a recovery key the data is gone. This is stated at setup. |

**The recovery key** is a long random secret shown once when the mesh is created and held by the
owner. Nothing escrows it. Two keys are derived from it. Its public signing key is in the roster as a
member of last resort that may add a device and outranks any device. Every epoch key is also wrapped to
its decryption key, so it can open history.

A recovering device still needs the roster and the wrapped keys from somewhere: a surviving device, a
node, or a mailbox. For an owner with none of those, the app can write an **encrypted backup file**
(roster, wrapped keys and recent history, sealed to the recovery key) to any place the owner chooses.
Without a surviving copy of any kind, the recovery key restores nothing but the right to start again.

**Credentials on a removed device.** Removing a device does not cancel third-party tokens or model keys
in its keystore. They are sealed to that device's hardware and usable only after the owner signs in to it. On
removal, another device revokes the lost device's connector grants where the provider allows it, and
lists the model keys that had been synced to it so the owner can rotate them.

**Limits.** A stolen device that is never online again cannot be told it was removed. Its hardware
keys need the owner's biometrics for consent, its allowance drops to the solo share within a day
(section 8.3), and no peer that has seen the removal will accept what it writes. A peer that has not
yet seen the removal may still sync with the removed device until it does. Instant offline revocation
is not possible, and LETS says so too.

---

## 8. Authority without a server

### 8.1 What the exam paper did, and why it cannot survive

The Delegated Authority Framework exchanged the user's token at Keycloak for a narrow token before each
tool call, and nested actor claims recorded who acted for whom. The live code still does this: one
Keycloak round trip per WebSocket tool dispatch ([research.md](research.md) A1). That cannot work on a
device with no network, and it makes the identity server a per-request dependency.

The properties are worth keeping: authority only narrows as it is passed on, every hop names its actor,
and a compromised tool cannot exceed what it was handed. This design keeps those properties and changes
the mechanism so a verifier needs nothing but public keys.

### 8.2 The ticket

An **effect** is a tool call that changes something, or that sends content chosen by the model outside
the mesh. The second part matters: fetching a URL or running a search is a read, but its arguments can
carry data out, so it is an outward flow and is judged by the flow rules in section 9.3.

Every effect carries a ticket with four parts. Whoever executes the effect verifies all four offline.

| Part | Says | Mechanism |
|---|---|---|
| Capability | Who is acting for whom, on which tool, with which argument limits, until when | A public-key token that each holder can narrow and pass on but never widen. The chain starts at a device on the roster and runs device → turn → agent → this call, and each link names its actor. The last link signs a digest of the exact tool and arguments. |
| Receipt | This effect was counted against a budget, once | A LETS receipt (section 8.3). |
| Consent | The owner approved this exact action | A signature by the presence key over the digest of the action as the host displayed it. Required when policy says so. |
| Labels | What the inputs to this decision were | The flow labels of section 9, carried in the capability so the executor can apply its own flow rules. |

The executor may be the same device, another of the owner's devices, or an institution's node. An
institution's node never trusts a device's gates: it verifies the ticket and then runs its own.

Where a remote service only understands OAuth (a third-party MCP server), the device holds that
service's OAuth tokens in its keystore and presents them as today. RFC 8693 token exchange remains for
talking to an institution's Keycloak-protected resources. It stops being the core of delegation.

### 8.3 LETS as the offline bound

LETS already lets a holder keep spending rights it holds while cut off, and refuses anything beyond
them. That is the behavior the owner asked for. Applied to devices:

- **A budget policy.** The owner sets, for each period (a day to start with) and each device, a share
  for every effect class and for model spend. The policy is a signed mesh-level document. Narrowing it
  takes effect on every device that sees it. Widening it needs the owner's consent.
- **Each period's budget is derived, not requested.** At the start of a period every device derives its
  own share from the policy and the roster. Two devices that do this apart arrive at the same budget
  with the same disjoint shares, so no peer has to be reachable and the total is bounded by
  construction. This also gets around two limits of LETS as built: it divides a fixed budget and never
  refills it, and it fixes membership when a budget is created. A new budget per period, built from the
  current roster, needs neither.
- **An allowance** is a device's share for the current period. A cut-off device spends within it and
  stops at its edge.
- **More, when a peer is reachable.** A device that needs more than its share asks another device to
  transfer part of its own, using LETS's existing prepare, accept and finalize.
- **Freshness.** The full share applies only while the device has heard from another member of the mesh
  (a peer, a node, or the removal notice at the mailbox) within the last day. That contact is its
  evidence that it has not been removed. After a day with no contact it drops to a **solo** share: a
  smaller cap that allows nothing outside the mesh without the owner's consent for that exact action.
  A consent signature needs the owner's biometrics, so it is itself strong evidence that the owner is
  present.
- A phone that is the only device in its mesh is always on the solo share. In that case the bound
  protects against a runaway model, not a thief.

The solo share keeps the promise that a phone works by itself. Without it, a phone would stop a day
after leaving home.

Starting values, for the owner to tune:

| Effect class | Pocket peer, fresh | Pocket peer, solo |
|---|---|---|
| Reads inside the mesh | Unlimited, rate limited | Same |
| Outward reads (fetch, search) | Unlimited while the turn holds no private data. Otherwise by the flow rules (section 9.3) | Same |
| Writes to the owner's own notes and memory | 200 a day | 50 a day |
| Effects outside the mesh (send, post, buy, delete elsewhere) | 20 a day without consent where policy allows. More with consent | Only with consent, each time |
| Model spend | A daily token share | A smaller daily token share |

**A thief and the clock.** Periods and freshness are measured on the device's own clock, which someone
holding the device may control. The kernel keeps a floor so the clock cannot go backwards. Holding the
clock still earns no new period, so no new share. Moving it forward ends freshness and drops the device
to solo. Either way the exposure is one period's fresh share plus the solo caps.

LETS as built does not fit a device ([research.md](research.md) A9). The work it needs:

1. Freeze the receipt and lease formats and publish test vectors (LETS #157).
2. Period budgets derived from a signed policy and the roster, so that two holders derive the same one.
3. A device profile for rollback protection: a restored phone is a new holder with a new share, and the
   old one lapses with its period.
4. A second implementation of the holder and verifier in the kernel, tested against the Python
   reference with the shared vectors.

Live top-up of a lease, which LETS has only as a proposal (ADR 0003), is not required by this design.

**What the bound does and does not do.** On one device, the agent and the receipt checker are in the
same process. The bound stops a misbehaving model, an injected instruction or a loop. It does not stop
someone who controls the device, because the device's owner is the principal. For a stolen device the
protections are the freshness rule, the small solo caps, hardware keys, biometric consent and removal
from the roster. Real separation exists when the effect runs on a different peer or a node, which verifies
the receipt itself.

---

## 9. Flow labels and the gates

### 9.1 Why labels

The paper's second layer was static analysis of tool descriptions by regular expression, and it named
rephrasing as its limit. Since then, every defense that tries to detect an attack inside the model's
input (classifiers, marking, hardened models) has been broken by adaptive attacks at rates above 90%.
The defenses still standing work outside the model: they track where data came from and let
deterministic code refuse a flow ([research.md](research.md) B4).

Astral already has a taint tracker and source marking, both partial and the tracker off by default.
This design makes flow labels a property of every event, so they are durable, they sync, and they
travel inside tickets to other devices.

### 9.2 The labels

| Axis | Values | Assigned by |
|---|---|---|
| Integrity | owner (typed or spoken by the owner), tool-trusted (a first-party tool's own computation over data the owner wrote), untrusted (anything the owner did not write, whichever tool fetched it: web pages, email, remote MCP output, files from elsewhere, other agents, audio that is not the owner) | The kernel, from the event's source. A tool cannot label its own output. |
| Confidentiality | public, private, PHI, secret | The source's declared class, raised by content checks (a local privacy filter. Presidio on nodes) |

A turn carries a running label. It starts from the labels of everything loaded into the model's
context (earlier messages, memory, notes, skills), not only what arrives during the turn. Once
untrusted content is in the context, what the model produces afterwards is treated as influenced by it.
An event derived from others (a memory, a compaction note, a learned item) inherits the least trusted
and most confidential of its sources, so untrusted text cannot be laundered by being remembered.

To avoid tainting everything, untrusted
content can be read by a **quarantined** model call that has no tools and must answer in a fixed schema
(a date, a number, a choice). The answer keeps its untrusted label as a value, so it can fill an
argument but cannot steer the turn. The small on-device model suits this job.

### 9.3 Rules the kernel enforces on labels

| Flow | Rule |
|---|---|
| Untrusted-influenced turn proposes an effect outside the mesh | Consent required, shown with the source of the influence |
| A turn that has all three of: untrusted input, access to private data, an outward channel | Consent required for the outward step (the "rule of two", computed from labels). This includes outward reads such as a fetch or a search. To keep researching without a prompt each time, the engine runs the outward work in a sub-turn whose context holds no private data and takes its results back as untrusted content. |
| An untrusted value used as an argument to an effect outside the mesh | Consent required, showing the value |
| PHI-labeled content to a model endpoint | Refused unless that endpoint is declared PHI-approved. The on-device model always qualifies. |
| PHI-labeled events in sync | Bodies go only to members of the PHI key group (section 7.4). Other peers receive the header only. |
| Secret-labeled values (credentials) | Never enter a model context. Injected at dispatch |
| Anything to a remote tool | The executor sees the labels in the ticket and may refuse |

Sync is a flow, and so is choosing a model. Treating both as flows under one rule set is what makes
"PHI stays on approved nodes" enforceable and not just promised.

**Who grants a PHI approval.** An approval for a device, a node or a model endpoint is a statement
signed by an institution's key. The owner adds that institution to the roster as an authority for PHI,
with consent. An owner cannot approve their own device for an institution's PHI. Which institutions
sign, and by what process, is a question for the owner (section 21).

### 9.4 Gate order for one effect

1. The mesh is not stopped, and the proposing device is on the roster.
2. Hard security flags, and the tool's definition matches the hash pinned when it was approved.
3. Permission: the tool is allowed for this owner, agent and scope (today's `is_tool_allowed`).
4. Policy rules, including argument-level rules. Rules merge across devices by narrowing only.
5. Flow-label rules (9.3).
6. Consent, when any earlier step asked for it. The prompt is drawn by the host from the canonical
   action, never from model output, and the signature covers that action's digest. The digest is
   checked again at dispatch.
7. Allowance: a LETS receipt is issued and counted.
8. Dispatch with an idempotency key.
9. The result is written as a signed event with its labels.

Steps 4 and 5 are off by default on the server today. They are on by default in the kernel (call C7).

### 9.5 Supply chain

- An MCP tool definition is pinned by hash at approval. A changed definition is a new tool that needs
  approval again.
- An A2A agent card must be signed to be registered. The current code does not check card signatures.
- Learned skills are data, never code, unless first-party signed (section 11.3).

### 9.6 Audit

Each device's feed is the audit record for what that device did: signed, chained, and verifiable by any
peer with the roster. Peers countersign each other's checkpoints when they sync, so a device cannot
rewrite its history without the others noticing. This replaces the server-held HMAC chain that only an
operator could verify.

### 9.7 Emergency stop

The roster carries a stop counter that only rises. Raising it on any device halts new effects on every
peer that sees it, and a ticket from before the stop is refused. This is Deep #245, #289 and #291.

---

## 10. The turn engine and reliability

### 10.1 One engine

The kernel owns the turn loop. It is an explicit state machine whose every step is an event: model
requested, tokens received, tool proposed, gates passed, tool dispatched, result recorded. Three things
follow.

- **Resumable.** After a crash, a restart or a move to another device, the engine replays the turn's
  events and continues from the last recorded step.
- **Never replays an effect.** A step that was dispatched with no recorded result becomes "outcome
  unknown". The engine stops and asks the owner, as spec 100 and `work_continuations.py` already
  require.
- **Attachable.** A user interface subscribes to a chat and receives its events. Steering, interrupting,
  forking and reverting are requests to the engine. Any number of windows, on any peer, can watch one
  turn.

The Python orchestrator keeps its own loop on nodes until the kernel engine reaches parity, then adopts
it ([plan.md](plan.md), phase 5). Both must pass one set of conformance vectors from the day the kernel
engine exists.

### 10.2 Model calls

Today one 60-second value covers every phase, the stream reader waits without a limit, and there is no
fallback ([research.md](research.md) A4). The engine uses separate clocks and a fallback chain.

| Mechanism | Rule | Starting value |
|---|---|---|
| Connect timeout | Fail fast when the host is unreachable | 10 s |
| First-token deadline | Per model and provider, learned from observed latency | 60 s cloud, 180 s self-hosted |
| Idle deadline between chunks | Catches a stalled stream | 120 s cloud, 300 s self-hosted |
| Attempt deadline | Upper bound on one attempt | 10 min |
| Retries | One layer only. Capped exponential backoff with jitter. Honors `retry-after`. A retry budget so a bad hour cannot multiply load | 3 attempts |
| After content has streamed | Never re-run the request. Keep the partial output and continue from it. | Always |
| Unreachable network | A separate, long budget: the turn waits and says so, and does not fail | 1 hour |
| Fallback chain, per turn | Rotate credentials, then try the next model, then the on-device model with a visible notice. The primary is tried again next turn. Used for overload, timeout and unavailability only, never for a refusal or an authorization error. | Owner-configured |
| Health | Tracked per model and provider pair | Always |

Voice mode uses much tighter deadlines and falls to the on-device model first (section 12).

### 10.3 Tool arguments

Today invalid JSON is refused and the model is asked to try again. Nothing repairs it, only one tool is
schema-checked by the orchestrator, and repeats are caught only inside the goal harness. The engine
applies one pipeline to every tool call in every turn.

1. **Prevent.** Use the provider's strict tool schemas where they exist, and grammar-constrained
   decoding for local models. Keep tool schemas flat, with enumerations and few optional fields, and
   include an example. Large text payloads (file contents, patches) go through free-form tools defined
   by a grammar, so the model never has to escape them as JSON.
2. **Parse progressively.** Streamed arguments are parsed as they arrive and treated as complete only
   at the end of the call.
3. **Repair.** Fix what is mechanical: unescaped control characters, trailing commas, a number sent as
   a string, an object sent as a string.
4. **Validate** against the tool's schema.
5. **Regenerate once.** If still invalid, ask for the arguments alone under the strict schema.
6. **Tell the model precisely.** If that fails, return a tool error naming the field, the problem and a
   valid example.
7. **Detect repeats.** Count identical calls, including unparseable ones. Warn at two, block at three,
   end the turn at six.
8. **Change strategy in code** when a call is blocked: a simpler variant of the tool, a different
   model, or stop and say why.

A call that cannot be parsed is never stored in a form that will be replayed to the model later. That
one rule would have prevented the incident where a single bad call poisoned every later request.

### 10.4 Reliability does not wait for the kernel

Everything in 10.2 and 10.3 can be applied to the existing Python loop now. [plan.md](plan.md) puts it
first, as phase R, because it helps the product today and none of it depends on decentralization.

### 10.5 What is borrowed from other harnesses

| Idea | Seen in | Where it lands here |
|---|---|---|
| A first-token deadline and an idle-stream watchdog as separate clocks | Claude Code, Codex, OpenCode, OpenClaw | 10.2 |
| Never re-run a request once output has streamed | Claude Code | 10.2 |
| A per-turn fallback chain: credentials, then models, primary restored next turn | Claude Code, OpenClaw, Hermes | 10.2 |
| A long, separate retry budget for an unreachable network | OpenCode | 10.2 |
| Schema-guided repair of tool arguments before validation | OpenCode | 10.3 |
| Free-form tools defined by a grammar for large text | Codex (`apply_patch`) | 10.3 |
| Arguments streamed as progressively complete objects | Pi | 10.3 |
| An always-on guard that ends a turn after repeated identical failing calls | OpenClaw | 10.3 |
| An append-only session tree with typed entries | Pi | 5 |
| One writer per conversation, and a turn in flight at a crash becomes "unknown" | Hermes | 10.1, 11.1 |
| A thread, turn and item protocol any front end can attach to, with steer, interrupt, fork and revert | Codex | 10.1 |
| Pairing a phone to a desktop by QR through an outbound-only relay | Claude Code, Codex, OpenCode | 6, 7.2 |
| Two-layer device trust: pairing, then approval of what each device may run | OpenClaw | 7.2, 8.3 |
| A phone that keeps a readable cache and a bounded outbox | OpenClaw | 5, 6 |
| Goal mode judged by a separate small model with round and stall limits | Claude Code, OpenCode | 11.1 |
| Cheap scheduled wake-ups that can skip the model | Hermes | 11.1 |
| Agent-written skills that are staged, aged and locked. Memory built in the background with redaction | Hermes, Codex | 11.3 |
| An approval reviewer that never sees tool output, with a circuit breaker | Claude Code, Codex | 9.4 (Deep #547) |
| Barge-in that tells the agent what was heard | OpenClaw, Hermes | 12.3 |

What their incidents teach: do not trust a loopback default or a URL parameter that carries a token.
Do not run an open skill registry. Do not ship with the sandbox off and prompts disabled.

---

## 11. Long-running work and learning

### 11.1 Runs

A run is work that outlives one turn: a goal, a schedule, a watch on something.

- **One executor.** A run has one executing peer at a time, recorded as a lease event. Other peers
  watch.
- **Handoff.** The executor can transfer the lease, and the next peer resumes from the log. A phone can
  start a goal and hand it to the Mac.
- **Takeover is an owner action.** If the executor has gone silent, the owner may continue the run on
  another peer, which marks the in-flight step as outcome unknown. No peer takes over by itself,
  because a silent executor may only be out of reach. If it was in fact still running, the run has two
  branches and both are shown. The budget still holds, because each peer spends only its own
  allowance.
- **Phones are honest about limits.** iOS allows continued work only when the user started it and
  progress is shown. A pocket peer runs what the operating system allows, then hands off to a desktop
  peer or node if one is reachable, and otherwise pauses and says so.
- **Goals are judged independently.** A separate small model checks progress against the goal after
  each round, with limits on rounds, stalls and waiting. The goal text is treated as data by the judge.
- **Budgets are LETS budgets.** A run cannot spend more than its lease.
- **Schedules belong to the peer that runs them.** A missed run is not replayed unless the schedule
  says so. A cheap pre-check can decide not to wake the model at all.

### 11.2 Context over long runs

Compaction keeps the most recent part of the conversation word for word and keeps tool calls with their
results. The engine keeps a notes document per run. Active skills are re-attached after compaction.

### 11.3 Learning: the model proposes, code decides

There is no loop today that proposes a change, tests it and adopts it ([research.md](research.md) A5).
Systems that let an agent rewrite itself freely have been caught faking their own checks, and public
skill registries carry malicious skills. The loop here is gated at every step.

1. **Propose.** The model writes a candidate (a memory, a skill, or a small edit to its standing
   instructions) into a staging area, with the events that justify it and the scope it wants.
2. **Static gate.** Deterministic checks: size limits, format, hidden characters, no scripts and no
   network use. The tools a skill names must already be granted. A learned item never carries authority
   and can never widen a permission. A candidate inherits the labels of the events that justify it
   (section 9.2), and one whose evidence includes untrusted content is never adopted automatically: it
   waits for the owner. Scanning text for injected instructions is kept as hygiene only, because
   section 9.1 shows that detection alone does not hold.
3. **Replay.** The kernel re-runs recorded turns from the local log with tool results stubbed, with and
   without the candidate. It is adopted only if a must-pass set still passes and the failure it targets
   improves. Pass and fail are decided by deterministic checks on the tool calls and outcomes, not by a
   model's opinion. The checks and their data are outside anything the model can write.
4. **Trial.** The item applies only to the project and tool it came from at first, and is withdrawn
   automatically if error or repeat rates rise.
5. **Ledger.** Every adoption is a signed event with a content hash. Undo is an event too.
6. **Review.** Anything wider than its original scope, and every instruction edit, needs the owner
   (Deep #553, Plane #119).

The log is the test set: the device learns from its owner's real history without that history leaving
the device. Replay runs when the device is idle and charging, and heavy suites go to a node.

---

## 12. Voice

Voice today needs a server worker, a LiveKit room and hosted speech services, transcribes whole
utterances, and cannot be interrupted on the local path ([research.md](research.md) A1, A3). This
design runs the pipeline on the device.

### 12.1 Pipeline

```
microphone ─▶ echo cancellation ─▶ voice activity ─▶ streaming transcription ─▶ end-of-turn check
                                                                                       │
speaker ◀── streaming speech ◀── small on-device model (fast path)  ◀──────────────────┤
                             ◀── owner's chosen model (full answer) ◀──────────────────┘
```

| Platform | Transcription | Fast model | Speech |
|---|---|---|---|
| iOS, macOS | The system's streaming transcriber | The system's on-device model, with constrained output | A compact neural voice. System synthesizer as fallback |
| Android | The system's on-device recognizer where present. A bundled recognizer otherwise | The system's on-device model where present | The system's on-device voice where present. Bundled otherwise |
| Windows | A bundled streaming recognizer | A local model runtime | A bundled compact voice |
| watchOS | None | None | Speaks. The watch captures and plays audio and the phone runs the pipeline |

Every device probes what it has and falls back. On hardware with no on-device model, the fast path is
skipped and the owner's model answers directly.

### 12.2 Feeling instant

Human turn gaps are about 200 ms. An untuned on-device cascade takes 600 to 1,050 ms. To close the gap:

- Start generating on the partial transcript and discard the draft if the person keeps talking.
- Keep the recognizer and the small model warm.
- The small model speaks a short acknowledgement or the first clause while the larger model works. It
  says nothing when the answer is already arriving.

### 12.3 Interruption

1. Echo cancellation stays on, and voice activity is read from the cleaned microphone while the
   assistant speaks.
2. On sound, lower the volume at once (within about 100 ms) and wait about a quarter of a second. Treat
   it as an interruption only if it lasts and contains words. A cough or "mm-hm" does not stop the
   assistant. A confirmed interruption is silent within half a second of the person starting to speak.
3. On a real interruption, stop speech, cancel the model stream, and cancel only tools marked safe to
   cancel.
4. Record in the log only the assistant text whose audio was played, so the model's history
   matches what the person heard.
5. If no words arrive, resume from the next unspoken sentence.

### 12.4 Voice and safety

- The transcript is an owner-integrity event only when it comes from the device's own microphone during
  an owner-started session. Audio from a file, a call or a video is untrusted content.
- A pending risky action is not interruptible and cannot be approved by voice alone. It needs the
  screen or the presence key, because a voice can be replayed or cloned.
- The voice path goes through the same gates as typed input. There is no shortcut.

---

## 13. Generative UI

### 13.1 What stays

The model proposes interface from a closed vocabulary of typed components. Deterministic code validates
and bounds it. Clients render it natively. That is the paper's contribution and the field has moved
toward it: Google's A2UI reached 1.0 in November 2026 on the same principle.

### 13.2 What changes, and what is new

| Change | Today | In this design |
|---|---|---|
| Where adaptation runs | The server rewrites each payload for the connected device | The log stores one device-neutral tree. Each device adapts it with the same deterministic rules, in the kernel. A chat renders correctly on a phone, a Mac and a watch from one event. |
| One source for the vocabulary | A schema generated from Python at run time, permissive, not committed | The vocabulary is published as a versioned schema. From it come the decoding grammar, the validator, and each client's support table. A model decoding under that grammar cannot emit an invalid tree. |
| Live surfaces | Update frames exist. Nothing replicates them | A surface is a document of events with stable component ids. A progress view or a form stays live and consistent on every device, and survives being offline. |
| Values | The model types numbers into components | A component can bind to a value in a tool-result event. The number shown is the number returned. |
| Actions | A string and a payload | Each action names a capability created with the surface. Pressing it writes a signed action event. The executing peer checks the capability and asks for consent if policy requires. A surface cannot grant itself more than the turn that made it had. |
| Consent | Server-held pending call | Drawn by the host from the canonical action. No component type can imitate it. |
| Links and images | Model-written URLs allowed in places | The model supplies handles. The host resolves them. This closes the exfiltration path through rendered links. |
| Completeness | Per-client fallbacks chosen by the server | The validator rejects an actionable component that lacks a spoken form, a compact form and an accessible name, so every surface works on a watch and by voice. |
| Provenance | None | Each node records whether a model, a tool or the host produced it, and the client can show that. |

Model-written HTML already exists as a sandboxed artifact type. It stays what it is: an artifact in a
frame under a restrictive content policy, never a way to draw controls or consent.

### 13.3 Interoperability

- **A2UI.** The vocabulary is published as an A2UI catalog. An A2UI surface from an outside agent is
  translated into primitives on arrival and treated as untrusted. An Astral surface can be exported to
  an A2UI renderer. This is the future work the paper named.
- **MCP Apps.** HTML from an MCP server is shown only inside the existing sandboxed artifact view, on
  clients that have one.

### 13.4 What looks new

The research found no published system that does these: a device adapting a UI description for itself.
One description required to work as screen, glance and speech. A generated surface replicated between
devices without a server. Action capabilities checked against an authority receipt as part of the UI
protocol. They are candidates for the dissertation's contribution and are marked as such, not as proven.

---

## 14. Protocols

| Protocol | Role | Notes |
|---|---|---|
| MCP (`2026-07-28`) | How every tool is reached: in-process, over stdio on desktops, over HTTP to remote servers, and between the owner's own peers | Between peers, MCP runs over the mesh transport with the ticket in request metadata. The LETS MCP profile is already pinned to this revision. Long calls use MCP Tasks. |
| A2A (1.0) | How agents outside the mesh are reached, and how a node publishes chosen capabilities | Agent cards must be signed. Provenance labels travel with each hop. Publication stays opt-in per capability (Deep #244). |
| OAuth 2.1 | Sign-in at Keycloak, and access to third-party MCP servers | Tokens are held in device keystores. |
| RFC 8693 | Exchange at an institution's Keycloak for its protected resources | An edge adapter, no longer the core of delegation. |
| A2UI | Import and export of generated UI | Section 13.3. |

No MCP or A2A profile exists for authorization between devices that share no authorization server. The
ticket is this design's answer, and it should be written up as a profile others could adopt.

Considered and not adopted now: the Agent Client Protocol, which covers a local editor talking to an
agent subprocess and has no remote transport yet. AG-UI, an event transport between an agent and a
front end, which the kernel's own attach protocol already covers. Either can be added as an adapter.

---

## 15. What stays centralized

These cannot be removed. For each, the table says why, what it can see, and what still works without
it.

| # | Item | Why it cannot be decentralized | What it sees | Without it |
|---|---|---|---|---|
| 1 | Identity proof (Keycloak, at UK now, moving later) | Someone has to vouch that a person is who they claim, once per device | Sign-in events | Enrolled devices keep working. A new device can join unattested with reduced rights. |
| 2 | Relay | Two devices behind home and mobile networks often cannot find a direct path | Encrypted packets, addresses, timing | Sync works on a shared network only. |
| 3 | Mailbox | Two devices that are never awake together need somewhere to leave events | Ciphertext, sizes, timing, device public keys | Sync waits until both are online. A node of the owner's replaces it. |
| 4 | Push gateway | Only the holder of the app's Apple and Google push credentials can wake a phone | That a wake was sent to a device token. No content | The phone syncs when opened. |
| 5 | App distribution and signing | The stores, notarization and code signing are single gatekeepers. The update feed is signed by one key | Downloads | No new installs or updates. |
| 6 | Model endpoints | A capable model needs a data center. The product's one model today is served by LLM Factory, which KOS hosts. | Prompts and outputs | On-device models, at lower quality. |
| 7 | The contracts | The component vocabulary, the event and ticket formats and the LETS format must have one source or devices stop understanding each other | Nothing | Nothing breaks. Nothing can evolve. |
| 8 | The public agent catalog | Fleet-wide agents arrive only by reviewed pull request, by an earlier owner decision | Nothing | Bundled and personal agents still work. |
| 9 | Removal notice for a lost device | If every other device is off, the notice must wait somewhere reachable | A signed removal entry | The lost device still drops to its solo share within a day. |
| 10 | Time | Lease expiry needs a clock, and device clocks can be wrong or changed | Nothing | Expiry falls back to the device's clock with a recorded floor. |
| 11 | Names and certificates | Items 1 to 6 are reached by DNS and TLS | Lookups | Those items are unreachable. Peers authenticate each other by key, not by name. |
| 12 | The web origin, when the web client returns | A browser must load code from somewhere | Requests | No web client. |
| 13 | Institution nodes | PHI rules require a known operator, an agreement and an audit trail | Whatever the owner grants | Personal use is unaffected. |
| 14 | Source hosting and CI | One place to review and build | Source | Development stops. Users are unaffected. |
| 15 | Third-party tools and agents | A remote MCP server or A2A agent is someone else's service | What is sent to it | That tool is unavailable. |

Items 2 and 3 are small, hold no user keys, and can be run by anyone: the owner's node, a UofL host, or
several operators at once. Item 4 holds no user keys either, but only whoever publishes an app build
holds its push credentials, so it has one operator per build. Until a shared host exists, the
transport's public relays can serve item 2, items 3 and 4 are simply absent, and the product must
describe sync as "when both are online".

---

## 16. Layers, and what each repository becomes

### 16.1 Two layers

The owner set the layering on 2026-11-12.

- **The base** is everything that decides, stores, authorizes and adapts: the kernel, AstralDeep, AstralPlane, LETS and ROTE.
- **Two installations** (owner, 2026-11-12):
  - **AstralVessel** is the base and its command-line tools, with no GUI. It bundles the kernel, Deep, Plane on SQLite and LETS into one node with a way to talk to it (a command line, A2A, MCP, and pairing for devices).
  - **The AstralDeep app** is everything in AstralVessel plus the UI layer. On the Mac it carries the whole base: the kernel, and Deep's Python node on Plane's SQLite backend as a supervised background process. On the iPhone it carries the kernel only, which holds the device-side parts of Deep (the turn engine and gates), LETS (the holder and verifier) and ROTE (adaptation) with SQLite storage, because iOS cannot run Deep's Python node. The phone reaches Deep's heavier agents on the owner's Mac or another node.
- **The UI layer** is AstralPrimitives (the vocabulary) and AstralProjection (the GUI clients and their renderers). It uses the base and never reimplements it.
- The base depends on the UI layer in one place only: ROTE and the validator read the committed Primitives schema as data. No base code imports Projection.

### 16.2 Repositories

| Repository | Layer | Today | After |
|---|---|---|---|
| **New: the kernel** (library name is the owner's, `AstralKernel` for now) | Base | Does not exist | A Rust library with Swift, Kotlin and Python bindings: event log and store, sync and transport, roster and keys, tickets, flow labels and gates, the LETS holder and verifier, the turn engine, device-side adaptation of UI. It also carries ROTE's Python package while thin clients still need it. Its own library, pinned by Deep and bundled by AstralVessel. |
| AstralDeep | Base | The orchestrator every client depends on | The node: a single-owner profile for personal use and a multi-owner profile for institutions. Hosts the heavy first-party agents as MCP servers, the PHI checks that need large models, and a mailbox and relay for its mesh. Its turn loop converges on the kernel's. |
| AstralPlane | Base | One database of mutable rows on PostgreSQL | The node's store, on SQLite for personal installs and the Mac app, and on PostgreSQL for institution nodes. New event tables with globally unique ids. |
| LETS | Base | A server-side budget runtime with fixed membership | Adds frozen formats and vectors, period budgets derived from a signed policy and the roster, and a device profile. The Python implementation is the reference. The kernel carries the second. |
| AstralVessel | Base, shipped | A separate Python agent server with its own loop, sandbox and egress | The base's headless installation with its command-line tools. `astralvessel up` installs a node that runs Deep's turn loop and gates on Plane's SQLite backend. Vessel's own loop is retired. Its sandbox and egress containers become the executor, which verifies tickets before it runs a tool. Its conformance suite becomes the base's acceptance test. |
| AstralPrimitives | UI | A Python package whose schema is generated at run time | The vocabulary's single source: a committed, versioned schema with a strict profile for generation, bindings for values and action capabilities, and the spoken and compact forms. |
| AstralProjection | UI | Thin clients that render server frames, plus ROTE | Peers' user interfaces. Each client embeds the kernel, renders from the local log, and owns pairing, voice, local tools and notifications. ROTE moves out to the base. Projection keeps only renderers and clients. |
| The AstralDeep app (built from Projection) | Both | A thin client of one server | The full product: the base plus the UI layer. The Mac app also runs Deep's Python node locally on SQLite. The iPhone app runs the kernel. |

---

## 17. Rules this changes

These need the owner's sign-off, because they contradict text in `AGENTS.md` or a recorded spec.

| Where | Says today | Proposed |
|---|---|---|
| `AGENTS.md`, mental model | One FastAPI orchestrator and PostgreSQL backend. Native clients are thin consumers of server-owned UI | Peers with a shared kernel. The orchestrator is a node. UI contracts are owned by the kernel and vocabulary |
| `AGENTS.md`, UI contract line | AstralPrimitives defines, AstralProjection renders and adapts, AstralDeep orchestrates | The base (kernel, Deep, Plane, LETS and ROTE, shipped as AstralVessel) decides, authorizes and adapts. AstralProjection renders. AstralPrimitives defines the vocabulary. ROTE leaves Projection. |
| `AGENTS.md`, security | Keycloak-only user IAM and RFC 8693 attenuated delegation | Keycloak remains the only source of user identity, proven at enrollment. Delegation is by offline-verifiable tickets with the same narrowing and actor-chain properties. RFC 8693 remains at the edge. |
| `AGENTS.md`, engineering | Backend code is Python | The kernel is Rust. Python remains the node's language. |
| `AGENTS.md`, credentials | LLM credentials are configured in-product and encrypted | Unchanged in intent. Custody moves from a server key to device keystores, and credentials sync as items sealed to each device. |
| Spec 100 | One durable authority per deployment. Multiple active orchestrators and cross-deployment merging are unsupported | True for each node. Above it, peers share a log whose merge rules are fixed in section 5.3. The conformance test is updated in the same change. |
| Feature 075 | The server is authoritative for voice. No speech models shipped to clients | On-device voice (call C8) |

---

## 18. Where this departs from the two reference documents

- **The decentralization draft** proposed one home node per user as the only durable authority, with
  offline clients limited to reading a cache and queueing intents. This design makes every device a
  peer with its own log and a bounded right to act alone, because the owner asked for clients that
  tolerate being cut off. It keeps that draft's per-node rule, its treatment of every boundary as
  untrusted, its `(issuer, subject)` identity and its signed audit chain. Its signed trust registry is
  the right tool for institutions that want to federate and is left for that phase.
- **The stack integration draft** treats Astral as one consumer of MCP surfaces and says nothing in the
  wider stack may depend on Keycloak. This design is compatible with both. A node's state can still be
  backed up to GaiaKeep as that draft describes.
- **AstralVessel** becomes the base's distribution (section 16). It runs Deep's turn loop, and its
  sandbox and egress become the executor. The executor must verify tickets as section 8.2 requires.

---

## 19. Risks, and what gets weaker

| Risk | Why | Answer |
|---|---|---|
| Revocation is not instant | A device that is offline cannot hear that it was removed | The freshness rule and solo caps, hardware keys, biometric consent. Stated plainly to the owner |
| A device's owner can tamper with gates on that device | The gates run on hardware the owner controls | By design the owner is the principal. Anyone else (an institution's node) verifies tickets and runs its own gates. |
| More copies of data | Every enrolled device holds history | Encryption at rest under hardware-protected keys. Per-device choice of how much history to hold. PHI never syncs to unapproved peers |
| More copies of credentials | A model key synced to five devices has five places to leak | Per-credential choice of which devices receive it. Sealed to each device's hardware key |
| A new language and build chain | The kernel is Rust across five targets, and CI jobs are capped at 30 minutes | A build spike in phase 0 decides whether call C2 stands |
| Two turn loops for a while | The node keeps the Python loop until the kernel reaches parity | One conformance suite for both. A named convergence phase |
| On-device models are weak | A 3-billion-parameter model is slow and unreliable at multi-step tool use | Used for acknowledgement, routing, quarantined reading and offline fallback, with a visible notice. Never silently substituted for the owner's model |
| Sync without a mailbox is disappointing | Two devices that are never awake together do not converge | Say so in the product. A node or shared mailbox fixes it |
| Scale of the work | This touches every repository and every client | Phases that each ship something useful. Reliability first because it is independent |
| Label over-tainting | Treating everything after untrusted input as influenced can demand too many confirmations | The quarantined-reader path. Measure confirmation rates before widening rules |
| The model endpoint is still at UK | Removing the sandbox does not remove LLM Factory | Any OpenAI-compatible endpoint already works. The fallback chain and on-device model reduce the dependency |

---

## 20. How this advances the exam paper

| Paper | This design | Method that is new to the paper |
|---|---|---|
| Delegation by token exchange at Keycloak for each call | Tickets any device verifies offline | Public-key capability chains. Per-call proof over the canonical action |
| Actor claims as the audit trail | Each device's signed log, countersigned by its peers | Witnessed hash chains |
| Static analysis of tool descriptions | Flow labels on every event, enforced by code outside the model | Information-flow control. A quarantined reader. A computed rule of two |
| Scope attenuation | Attenuation plus a spendable budget and a consent signature bound to the action | LETS receipts. Hardware presence keys |
| Server-driven UI primitives | The same vocabulary as the single source for grammar, validator and renderer | Constrained decoding. Value binding |
| ROTE on the server | Adaptation on the receiving device | Device-side adaptation. Required spoken and compact forms |
| WebSockets to one orchestrator | A replicated log between peers | Local-first sync |

Four claims look open in the literature as of this research and could carry the dissertation: flow
labels carried inside attenuated tickets so a second device enforces them offline. An authorization
profile for MCP and A2A with no authorization server. Bounded offline authority for a person's own
devices. Generated UI that is replicated, adapted on the device, and bound to authority. Each needs
an evaluation the paper did not have: adaptive attacks, a live model, and small on-device planners.

---

## 21. Questions for the owner

1. **The kernel in Rust (C2).** Accept, subject to the phase 0 build spike? If not, the fallback is
   three native implementations against shared vectors.
2. **A phone by itself (C1).** Confirm that a phone with no other device reachable should run turns
   within its allowance.
3. **A mesh with no identity provider at all.** This design requires a Keycloak attestation to create a
   mesh. A purely personal mode with none would be simpler for individuals and conflicts with the
   Keycloak-only rule. In or out?
4. **Allowance defaults (8.3).** Are the starting values the right order of magnitude?
5. **History for a new device (7.4).** Should a newly paired device read all earlier history by default?
6. **Shipping speech and language models to clients (C8).** Accept the download size and the reversal
   of the feature 075 decision?
7. **The interim relay.** Until a UofL host exists, may clients use the transport's public relays?
8. **The model endpoint.** Is LLM Factory staying available to users outside UK, or should the default
   change?
9. **The name** of the new component. The distribution is AstralVessel (owner, 2026-11-12). The kernel library's own name is still open.
10. **The rule changes in section 17.**
11. **A shared deployment during the transition (C10).** Does a multi-user server have to keep working
    between the personal node (phase 1) and the institution profile (phase 7)? If yes, the node keeps a
    qualified multi-owner mode throughout and every phase costs more.
12. **Who grants PHI approval (9.3).** Which institution signs that a device, a node or a model endpoint
    is approved for PHI, and by what process? The design needs the signer's key in the roster contract
    in phase 0.
