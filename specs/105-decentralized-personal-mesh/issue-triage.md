# Issue and pull-request triage for spec 105

**Date:** 2026-11-11. **Source:** `gh issue list` and `gh pr list`, state open, for AstralDeep,
AstralProjection, AstralPlane, AstralPrimitives and LETS. Nothing on GitHub was closed, edited or
commented on. This is a recommendation list.

**Basis.** Each call below follows from [spec.md](spec.md): no central orchestrator, every device holds
a signed event log and syncs it, the agent loop runs on the device or on a node the owner runs,
authority is carried in offline-verifiable tickets, voice and device adaptation run on the device, and
the web client is paused. I read each title and the first paragraph of each body. I did not read full
bodies or comment threads, so a "close" here should be confirmed against the full issue before acting.

## Summary

| Repo | Open issues | Close | Keep | Open PRs | Close | Hold | Keep | Unaffected |
|---|---|---|---|---|---|---|---|---|
| AstralDeep | 62 | 23 | 39 | 4 | 1 | 0 | 3 | 0 |
| AstralProjection | 35 | 4 | 31 | 7 | 0 | 1 | 6 | 0 |
| AstralPlane | 8 | 3 | 5 | 0 | 0 | 0 | 0 | 0 |
| AstralPrimitives | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| LETS | 1 | 0 | 1 | 5 | 0 | 0 | 0 | 5 |
| **Total** | **106** | **30** | **76** | **16** | **1** | **1** | **9** | **5** |

What the dispositions mean:

- **Close (superseded):** the design replaces what the issue asks for. Nothing needs re-filing.
- **Close (re-file):** the need survives but the issue's design assumes the central server. Close it and
  open the named replacement under spec 105.
- **Keep (retarget):** still wanted. Where it runs changes. The note says how.
- **Keep:** unaffected by this spec.
- **Raise:** keep, and it now blocks a spec 105 phase.

**Close or delete?** I recommend closing, not deleting. Most of the closable issues carry bounty points,
and a closed issue keeps that record and the reason. If you want the tracker shorter, the ones safe to
delete outright are the children whose text only repeats a parent that is also closing: Deep #265,
#274, #275, #286.

## Close: 30 issues and 1 pull request

### AstralDeep (23 issues, 1 PR)

| # | Title (short) | Disposition | Why, and what replaces it |
|---|---|---|---|
| 220 | LETS receipt enforcement through real MCP, A2A and voice entry routes | Close (re-file) | The entry routes change. Voice becomes on-device and enters as text. MCP and A2A ingress move to the node and kernel. Re-file as "receipt enforcement through kernel dispatch and node ingress" (phase 3). |
| 221 | Qualify mobile voice reconnect across the composed server routes | Close (superseded) | The server voice routes (worker, LiveKit room) are removed in phase 4. Client lifecycle fencing continues in Projection #31 and #32. |
| 236 | [Tracking] Resume work from durable operation and job completion events | Close (superseded) | The durable turn engine writes every step to the log, and a completion is an event any peer can wake on (phase 5). |
| 265 | Resume waiting work when an internal operation finishes | Close (superseded) | Child of #236, same reason. |
| 267 | Expose completion subscriptions to SDK and framework callers | Close (re-file) | Replace the bespoke subscription API with MCP Tasks on a node's endpoint (phase 6). |
| 237 | [Tracking] Route notifications by owner preference and device state | Close (re-file) | No server knows device state any more. Each device decides from the synced event. Re-file as "notification policy evaluated on the device" (phase 2). |
| 268 | Route foreground notifications and expose a durable inbox | Close (superseded) | The inbox is a view over the log. |
| 270 | Deliver mobile notifications through bounded provider adapters | Close (re-file) | Becomes the content-free push gateway, one of the listed centralized services (phase 2). |
| 239 | [Tracking] Versioned harness profiles with rollout and rollback | Close (re-file) | There is no server population to roll out across. Re-file as "one turn engine with shared conformance vectors" (phases 0 and 3). |
| 274 | Adapt existing runners to a versioned harness contract | Close (superseded) | Child of #239. |
| 275 | Select qualified harness profiles and roll back new-work rollout | Close (superseded) | Child of #239. |
| 242 | [Tracking] Governed Cresco personal-mesh bridge between deployments | Close (re-file) | The mesh has its own identity and transport. Re-file as "Cresco as an optional transport and compute adapter". |
| 243 | [Tracking] Keep personal-mesh work recoverable when a node fails | Close (superseded) | This is spec 105 itself: every peer holds the log, and executor leases handle takeover. Spec 100 is amended. |
| 286 | Recover orchestrator and client work continuity after node loss | Close (superseded) | Child of #243. |
| 246 | [Tracking] Guided production-safe installer and first-run verification | Close (re-file) | A "production deployment" with operator secrets is replaced by a personal-node installer plus device pairing (phase 1). |
| 293 | Bootstrap a valid production deployment safely | Close (re-file) | Re-file as "bootstrap a personal node" (phase 1). |
| 295 | Guide first client connection and configured-service verification | Close (superseded) | Pairing replaces "connect to the configured server". |
| 321 | Qualify an isolated OpenAI Agents SDK consumer of Astral Work | Close (superseded) | The bespoke Work REST surface on a central server is not carried forward. Outside consumers use MCP or A2A at a node. |
| 322 | Serve MCP resources for an optional ChatGPT Work panel | Close (superseded) | Needs a publicly reachable Astral endpoint, which a personal mesh does not have by default. Re-file only if a node operator asks. |
| 323 | Qualify the OpenAI MCP tunnel against Astral authentication | Close (superseded) | Same as #322. The authentication it would qualify is being replaced. |
| 549 | Publish an owner-scoped live status feed for every chat and child run | Close (superseded) | Every peer already has every chat's events through sync. |
| 550 | Let scheduled jobs target the Mac under an exact-job standing grant | Close (re-file) | A schedule is owned by the peer that runs it. The standing grant becomes a capability with a LETS allowance (phase 5). |
| 561 | Thread a turn id through audit rows and export a verifiable per-chat bundle | Close (superseded) | The signed per-device log is the verifiable bundle and the turn id is its key (phase 2). |
| PR 571 | Add fork, edit-and-resend and regenerate through normal dispatch (draft, +2806) | Close | It builds branching on the server history service and integer message ids, both replaced in phase 2. Redo it on the log model, where a chat is already a graph of turns. Issue #542 stays open. |

### AstralProjection (4 issues)

| # | Title (short) | Disposition | Why, and what replaces it |
|---|---|---|---|
| 28 | Android client-local speech through the server-selected voice contract | Close (re-file) | The recognizer and synthesizer work carries into the on-device voice pipeline (phase 4). The server-selected contract does not. |
| 29 | iOS client-local speech with on-device guarantees | Close (re-file) | Same as #28. |
| 55 | Render a bounded ChatGPT MCP status and artifact panel | Close (superseded) | Goes with Deep #322. |
| 125 | Report retained host jobs on reconnect and show an outcome-unknown card | Close (re-file) | With the loop on the Mac, a dropped connection no longer orphans host jobs. Outcome-unknown remains only for calls to another peer. Re-file for that case. |

### AstralPlane (3 issues)

| # | Title (short) | Disposition | Why, and what replaces it |
|---|---|---|---|
| 116 | Store exact-job standing grants and host-offline skips for scheduled Mac runs | Close (superseded) | Goes with Deep #550. "Host offline" is a server's view of a client. |
| 120 | Record chat-to-worktree bindings for Mac recovery | Close (re-file) | A worktree path is state of one Mac. It belongs in that device's store, not in a shared database. |
| 122 | Store MCP connector records with encrypted OAuth credentials | Close (re-file) | Credentials move to device keystores and sync as sealed items, not as rows encrypted with a server key. |

## Keep: 76 issues and 9 pull requests

### Raise: these now block a spec 105 phase

| Repo | # | Title (short) | Blocks |
|---|---|---|---|
| LETS | 157 | Publish receipt verification and claim conformance vectors for non-Python executors | Phase 0. Every non-Python verifier depends on it. |
| Deep | 245, 289, 291 | Emergency stop: tracking, local stop, propagate and fence peers | Phases 1 and 2. This is the stop epoch in the device roster. |
| Deep | 558 | Require verified human presence before high-risk approvals | Phase 3. Generalizes to device-signed consent on every platform. |
| Projection | 138 | Sign high-risk Mac approvals with a Touch ID-gated Secure Enclave key | Phase 3, same. |
| Deep | 560 | Govern Mac host effects with LETS receipts bound to the execution plan | Phase 3. Executor-side verification. |
| Projection | 139 | Verify LETS receipts and refuse executable or folder drift on the Mac host | Phase 3, same. |
| Deep | 553 | Let owners review, undo and forget what the agent learns | Phase 5. This is the adoption gate of the learning loop. |
| Plane | 119 | Stage learning proposals and keep a reversible memory mutation ledger | Phase 5. Make the ledger log-native. |
| Deep | 318 | Qualify local contextual privacy filtering for free-form tool text | Phase 3. A local filter is what lets PHI checks run on a device without Presidio. |
| Deep | 331 | Qualify a native Claude Messages adapter | Phase R. Native adapters make strict tool schemas possible. |

### Keep (retarget)

| Repo | # | Title (short) | What changes |
|---|---|---|---|
| Deep | 233 | [Tracking] Governed shell and interactive terminal sessions | Sessions run on a desktop peer or node and are reached through mesh MCP, not a central API. |
| Deep | 234 | [Tracking] Project Wiki/Atlas | Pages become log-native documents so they sync. |
| Plane | 52 | [Tracking] Wiki/Atlas repository contracts | Same. Atlas schema modules already exist. Check what remains. |
| Deep | 266 | Connect remote-job completion to authorized wakeups | The completion arrives as a log event. |
| Deep | 238 | [Tracking] Evidence-preserving tool recall and safe compaction | Built in the turn engine. |
| Deep | 282 | Dispatch authorized work to an approved Cresco target | Optional remote-compute target. Low priority. |
| Deep | 244, 288 | Selective A2A publication | Per node or peer, signed by the mesh key. |
| Deep | 294 | Interrupted installation, upgrade and safe removal | Applies to the node package. |
| Deep | 326, 333 | OpenAI Responses adapter. Claude prompt-prefix caching | Built in the new provider layer. |
| Deep | 338 | Replay multi-turn escalation through real dispatch gates | Runs against the kernel's gates as conformance and adversarial replay. |
| Deep | 349 | Offline prompt-optimization pilot with held-out evaluation | Becomes the evaluation half of the learning loop. |
| Deep | 541 | Steer, queued follow-ups and side questions | The queue is log events held by the turn engine. |
| Projection | 121 | Mac composer steer and queue | Same. |
| Deep | 542 | Fork, edit-and-resend, regenerate | Native operations on the log's turn graph. Do not build on integer message ids. |
| Projection | 122 | Edit, regenerate, copy and fork from any turn | Same. |
| Deep | 551 | Goal pause, resume, budgets and gates | Goal state lives in the log. Budgets map to LETS. |
| Plane | 117 | Persist goal runs with status, budgets and gates | Goal runs are log events. Plane materializes them on nodes. |
| Deep | 559 | Connect owner-registered remote MCP servers | Connector code exists. Check what remains. Its output must be labeled untrusted (a gap found in research). |
| Deep | 563 | Tighten-only owner rules | A synced policy document whose merge can only narrow. |
| Plane | 124 | Owner policy rule sets with revision history | Same. |
| Plane | 118 | Per-project skill trust decisions | Trust decisions sync through the log. |
| Projection | 32 | Mobile voice interruption and recovery evidence | Qualifies on-device barge-in. |
| Projection | 46 | [Tracking] Interactive terminal panel | The UI contract is owned by the kernel, not a server. |
| Projection | 47, 50, 51, 52 | Background notifications and the Android and iOS adapters | The push carries no content. The device syncs, then posts a local notification. |
| Projection | 130 | Chat status badges and Activity board | Reads the local log. |
| Projection | 132 | Feed the slash menu from the server catalog | The catalog comes from the kernel. |
| Projection | 142 | Push-to-talk, wake phrase, listening overlay | Folds into the phase 4 voice work. |

### Keep (unaffected)

- **Deep:** #264, #273, #324, #336, #337, #540 (tracking. Re-baseline its children against this list),
  #543, #546, #547, #552, #554, #555, #556, #564.
- **Projection:** #31, #48, #49, #123, #124, #126, #127, #128, #129, #131, #133, #134, #135, #136, #137,
  #141, #143, #144. Note on #124: `MacComputerSandbox.swift` already wraps commands in a Seatbelt profile
  behind a loopback proxy, so this may already be met. Check and close it on its own merits.

### Pull requests

| Repo | PR | Title (short) | Call |
|---|---|---|---|
| Deep | 569 | Register Mac checkpoint verbs (draft) | Keep. Device-local feature. |
| Deep | 610 | Load AGENTS.md and CLAUDE.md from granted folders (draft) | Keep. |
| Deep | 617 | Fail-closed auto-review stage (draft) | Keep. |
| Projection | 155 | Per-chat memory switches and a Learned notice (draft) | Keep. |
| Projection | 156 | Ask, Auto-review and Bypass mode selector (draft) | Keep. |
| Projection | 157 | Plan toggle, plan card, question panel (draft) | Keep. |
| Projection | 158 | @-references and project instructions indicator (draft) | Keep. |
| Projection | 159 | Mac notifications and Dock badge (draft) | Keep. Local notifications are the right mechanism under this spec. |
| Projection | 191 | Queue by default on macOS with an app-held queue | Keep. An app-held queue fits this spec. The vault records two blocking bugs. In phase 2 its storage moves into the log. |
| Projection | 197 | Verify LETS receipts on the Mac host (draft, Swift) | Hold until the kernel decision (spec call C2. Question 1 in spec section 21). With a shared kernel this verifier is replaced. Without one, merge it against the LETS #157 vectors. |
| LETS | 158 to 162 | Dependabot bumps | Unaffected. Handle on their own merits. |

## Related findings outside the triage

Research found four defects on `main` that no open issue tracks. They are independent of this spec.
They are recorded privately until they are fixed, and this public document does not describe them.
