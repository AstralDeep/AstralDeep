# Research: evidence behind spec 105

**Date:** 2026-11-11. **Read at:** AstralDeep `a3eb724a`, AstralProjection `ca7ba29`, AstralPlane `4447aac`,
AstralPrimitives `fe1ce16`, LETS `c7b1e56` (all `main`), plus the kos-wiki vault and the public sources
listed in Part B.

**How to read the labels.** *In code* means the claim was read in source at the path given. *Spec only*
means it is written down but not implemented. *Inference* is a conclusion drawn from the code. Nothing
here was established by running a build, a test or a live system. Claims marked "checked" were re-read
by hand after the first pass. The rest come from one reading and should be re-verified before a change
depends on them.

---

## Part A. What exists today

### A1. Everything that assumes one server

| # | Assumption | Where | Status |
|---|---|---|---|
| 1 | Clients are built against one host and one identity provider | Apple `AstralCore/Configuration.swift:13-14`. Android `app/build.gradle.kts:298,331-332`. Windows `deployment/release-profile.json:8-9`. Web builds its socket URL from `location.host` (`webrender/static/client.js:9`) | In code, checked (Apple, Android) |
| 2 | The audience of a user token is not checked, and the issuer is compared only when present | `backend/orchestrator/auth.py:303-310` | In code, checked |
| 3 | The owner key is the bare `sub` claim | `auth.py:322-325`. `mesh_api.py:57` | In code, checked |
| 4 | Every WebSocket tool dispatch performs a token exchange at Keycloak, with no cache seen | `orchestrator.py:22320`, `:22430`. `delegation.py:75-144` | In code |
| 5 | Internal and mesh-member tokens are HS256 with a server secret | `delegation.py:262-285`. `mesh_admission.py:99-107` | In code |
| 6 | The audit chain is an HMAC under one server secret, one chain per user, sequence assigned by a database trigger | `backend/audit/pii.py:159-163`. Plane `repositories/audit.py:186-189,633-637` | In code |
| 7 | LLM keys are encrypted with a server key, and Keycloak refresh tokens live in the server session store | `llm_config/user_store.py:103-118`. `web_auth.py:279-297` | In code |
| 8 | Chat state is a mutable snapshot in one PostgreSQL database. Message ids are `SERIAL` | `history.py:556-581`. Plane `legacy_baseline_066.py:41` | In code, checked |
| 9 | Spec 100 selects "one durable authority per deployment" and declares multiple active orchestrators and cross-deployment merging unsupported, with a conformance test | `specs/100-mesh-availability-topology/contracts/availability-contract.md:11-19` | Spec, checked |
| 10 | LETS is reached as a remote warden. `LETS_WARDEN_URL` is required | `lets_config.py:182` | In code, checked |
| 11 | Voice needs a server worker, a LiveKit room and hosted speech services | `backend/voice_agent/session.py`. `speech_adapters.py:19-22` | In code |
| 12 | Per-device adaptation (ROTE) runs on the server, in Python | `AstralProjection/backend/rote/adapter.py` | In code |

### A2. How a second device sees a chat today

There is no ordered, replicable event log.

- A live turn streams only to the socket that started it (`chat_steps.py:278-294`).
- At commit the server sends a full snapshot to every socket of the same user that has that chat open
  (`orchestrator.py:13235-13308`), and re-sends the whole chat list to all of the user's sockets
  (`:24052`).
- Reconnect carries only `active_chat_id` and a request generation. The server answers with a full
  snapshot. There is no "since revision N" cursor. `frame_sequence` is an in-memory per-socket counter.
- The only per-user ordered log is the audit chain, which a client cannot verify.

Inference: cross-device sync needs a new log layer. It cannot be retrofitted onto `render_revision` or
integer message ids.

### A3. The clients

| Client | Shows anything offline | Stores on device | Runs tools locally | On-device speech | Device key |
|---|---|---|---|---|---|
| iOS | No | Keychain tokens, one unsent message, active chat id | No | OS recognizer and synthesizer, half-duplex, final results only | Software Ed25519 seed in the Keychain |
| macOS | No | Same, plus file checkpoints and project grants | Yes (Seatbelt shell, browser pane, accessibility), but only on server tool calls. The host disconnects when the socket drops (`AppModel.swift:2869`, checked) | Same as iOS | Same as iOS |
| watchOS | No | Tokens, chat id | No | Speaks text. Microphone audio is relayed to the server | None found |
| Android | No | Encrypted preferences (grant, cookie, mesh seed), chat id. No Room or SQLite | No. `FOREGROUND_SERVICE` is removed from the manifest | OS on-device recognizer (API 33+) and TTS, half-duplex | Ed25519 seed in encrypted preferences |
| Windows | No | QSettings, a local audit log, a LETS receipt replay store (SQLite) | Yes (BYO agent host, tools, computer use), but bundles, dispatch and receipts come from the server | `System.Speech` helper and Qt TTS, half-duplex | DPAPI-wrapped Ed25519 |
| Web (paused) | Public shell only | localStorage, a non-extractable WebCrypto key in IndexedDB | No | Web Speech where the browser has it | WebCrypto Ed25519 |

Other client facts, all in code:

- No client persists a transcript. `ConversationResumeStore.swift:1-3` says so explicitly (checked).
- A dropped socket ends the turn on the client: the message is restored with "Review it and send it
  again" (`AppModel.swift:2885`, checked).
- No client contains an on-device model. A search for Core ML, Foundation Models, MLX, llama, ONNX,
  MediaPipe and Whisper across client sources found nothing.
- No client has voice-activity detection or barge-in on the local speech path. Both local engines
  declare `full_duplex: false`.
- The conversation snapshot (`ConversationMessage`, a strict list of text and component parts) is
  self-contained and could be re-rendered from a local store. Streaming and chrome frames could not.
- `contracts/ui_protocol.json` records, per component type and per client, `supported` or
  `omitted_by_server` with a named fallback. The server applies the fallback. No client can.
- Rough size, non-test: Apple about 63k lines of Swift, Android about 27k of Kotlin, Windows about 31k
  of Python, web about 19k.

### A4. The orchestrator's turn loop and its reliability

- The loop lives in `orchestrator.py` (about lines 15,880 to 17,300) and is keyed on a live socket, a
  PostgreSQL task admission and a server-resolved model client.
- The harness contract (`harness/contract.py:250-281`) has four seams (lifecycle, tool, provider,
  accounting). It wraps one model invocation. It does not own the loop or tool dispatch.
- Turn limits: 10 for chat, 24 for computer use, 128 by default for goals.

| Reliability item | Today | Where |
|---|---|---|
| Request timeout | One 60-second value for every phase. No first-token deadline. No total deadline for OpenAI-style calls | `llm_config/client_factory.py:14,106-111` (checked) |
| Stream liveness | The stream pump waits on a queue with no timeout | `orchestrator.py:18300` (checked) |
| Retries | 3 attempts, backoff `min(2^(n-1), 8)` seconds with ±20% jitter. 408, 409, 425, 429, 5xx, transport errors | `orchestrator.py:18835`, `:18111`, `:17651-17696` |
| Fallback | None to another model or provider after a failure | `model_router.py:35` (router off by default, never used for failover) |
| Invalid tool JSON | Refused, with an error telling the model to re-emit. No repair attempt | `orchestrator.py:19977-19994` (checked) |
| Schema validation | Only for the computer-use `write_file` tool at the orchestrator. Other tools rely on the agent | `orchestrator.py:19996-20041` |
| Strict or constrained output | Used for internal calls (UI design, goal verdict), not for chat tool calls | `orchestrator.py:18515-18520` |
| Repeated-call guard | Only inside the goal harness (three identical failures or results) | `goal_progress.py:28,186-197` |
| On-device model | A router flag exists and is only logged | `orchestrator.py:17827` |

Worst case from these numbers (inference): a stalled provider holds a turn for about three minutes
before the user sees an error.

Recorded incidents (vault, dated): a single non-JSON `function.arguments` poisoning every later request
(2026-11-02, Deep #515). Raw control characters in `write_file` arguments (2026-11-02, #534).
Compaction silently doing nothing for an unknown model (2026-11-02, #513). Thirty identical calls after
one refused malformed call (2026-11-09, #619). Speech services out of memory with a 30-second failure
path (2026-08-28).

### A5. Long-running work and learning

- Goal loop with verification, budgets, a plan of at most 12 steps, and steering delivered at the next
  tool boundary. `turn_steer.py:58` states there is no durable server queue.
- Scheduler and persistent agents are PostgreSQL lease loops on the server. They survive a server
  restart, not a missing server.
- Compaction keeps tool-call and tool-result pairs together (budget ratio 0.80).
- Learning: `memory_chat.py` (the model decides what to remember. On by default),
  `knowledge_synthesis.py` (off by default), `skill_memory.py` (off by default. Recipes are held in
  memory and lost on restart), hand-written user skills, and `prompt_optimization/` (evaluation only.
  The product may not import it). There is no loop that proposes a change, tests it and adopts it.

### A6. Security controls as built

The gate stack for one tool call is `_run_project_bound_gate_stack` (`orchestrator.py:19239`). Every
entry route (chat, MCP, A2A, component actions) goes through it.

| Control | Default | Depends on a trusted server? |
|---|---|---|
| Hard security flags (regex at registration) | On | No |
| Tool-to-scope declaration | On | No |
| `is_tool_allowed` | On | Needs a store. The logic is pure |
| Policy chain | Off (`FF_POLICY_ENGINE`) | No |
| Owner review of every non-read effect | On | Needs a connected UI |
| Taint tracking | Off | No |
| Supervisor and high-risk review | Off | No |
| Delegation mint (Keycloak exchange, HS256 children) | On in production | Yes |
| LETS | Off (`lets_config.py:436`) | Remote warden |
| PHI redaction | Per-user opt-in. Fail-closed gate on durable memory writes | Presidio and spaCy models |
| Sandbox | rlimits and environment scrub | OS-specific |
| Egress guard | On | No |

Gaps found while reading (in code unless marked):

- Agents do not verify the delegation token they receive. `decode_token_payload` does not check the
  signature (`delegation.py:288-297`). Enforcement is on the orchestrator side only.
- No A2A agent-card signature check was found.
- In-process agents receive the plaintext LLM key (`orchestrator.py:19659`).
- Four further defects in existing controls were found. They are recorded privately until they are
  fixed, and this public document does not describe them.
- Model-authored HTML exists: `publish_artifact` (`artifact_html.py`) accepts sanitized HTML with
  scripts under a restrictive content policy. The vault's "no markup crosses the boundary" is out of
  date.

Protocols: the MCP client speaks revisions 2024-11-05 through 2025-11-25 over Streamable HTTP and SSE
with OAuth (PKCE, dynamic registration, protected-resource metadata). There is no stdio transport. The
MCP server endpoint advertises `2026-07-28` (`shared/protocol.py:45`, checked) behind `FF_MCP_SERVER`.
A2A is wire version 1.0 with a 0.3 compatibility path, JSON-RPC only. The inbound server is behind
`FF_A2A_SERVER`, off by default.

### A7. AstralPlane

- About 55 repositories behind `RepositoryCatalog`. Repositories embed PostgreSQL SQL (advisory locks,
  `FOR UPDATE SKIP LOCKED`, triggers, JSONB). Schema revision `089.020`.
- Owner scoping is an application-level column on every table. There is no row-level security.
- No change feed, no replication, no device id column, no sync cursor.
- The mesh tables hold membership only, with `membership_epoch` and `revocation_epoch` counters.
- Inference: the same facade on SQLite is a rewrite. A phone needs a separate device store.

### A8. AstralPrimitives

- 40 wire types (layout, content, input, charts, media, app-level). Version 0.12.0.
- `wire_schema()` generates JSON Schema 2020-12 from the Python models at call time. The schema is not
  a committed file, and `additionalProperties` is true because of the `attributes` escape hatch.
- Actions are a string plus a payload on `Button`, `FileUpload` and `DiffAction`. A form submit is a
  templated chat message. There is no data binding, no partial update and no event schema in the
  package. Update frames are named only in Projection's protocol file.

### A9. LETS

- Version 1.1.2. Python, one hard dependency (PyNaCl), SQLite. Ed25519 signatures over its own
  canonical JSON (LETS-CJ/1).
- A warden holds a disjoint share of an integer resource vector and issues leases and receipts from it
  without contacting peers. An executor verifies receipts offline against a replay store.
- Budget moves between wardens by prepare, accept, finalize. A timeout never refunds.
- Revocation stops new authorizations. It does not retract issued receipts, and instant offline
  revocation is a stated non-goal (`docs/threat-model.md:64-65`).
- What does not fit devices as built: membership is fixed at genesis with no live epoch rollover
  (`README.md:260`, checked). Rollback protection assumes a second independent store. A restored
  backup is treated as a stale branch. The production profile is Linux with separate mount points.
  There is no Swift or Kotlin implementation and no receipt test vectors (LETS #157 asks for them). A
  phone app would be agent, warden and executor in one process.
- The MCP profile (`lets.mcp-profile/v1`) is unreleased and pinned to MCP `2026-07-28`.

### A10. The exam paper (2026-04-01)

Contributions: the Delegated Authority Framework (RFC 8693 token exchange applied to WebSocket agent
traffic, actor claims, a three-layer model of token validation, static tool analysis and scope
attenuation, later DPoP). A custom MCP and A2A communication model. Server-driven UI primitives with
ROTE adapting them per device. WebSockets throughout.

Its stated limits: regex and AST analysis is evaded by rephrasing. Tests are deterministic with no live
model. ROTE's format is proprietary. Its future work names recursive delegation chains, mDNS-style
agent discovery, an A2UI-compatible schema and an LLM judge for tools.

Every one of its mechanisms presumes a central orchestrator that holds the secrets, talks to Keycloak
for each delegation and adapts UI for the client.

### A11. Owner decisions already on record that bear on this

- BYO agents run on the owner's device, are never shared and go offline with the client (2026-07-14).
- The Mac App Store lane is retired. macOS ships as a notarized direct download (2026-10-25). The App
  Store sandbox forbids spawning child processes.
- Merge decisions count macOS and web only (2026-11-08).
- PHI redaction is a per-user opt-in (2026-11-02). The gate on durable memory stays fail-closed.
- Feature 075 kept the server authoritative for voice and rejected shipping Whisper and Kokoro to every
  client (2026-08-28). This spec reverses that for the reasons in spec section 12.
- The product runs one open model from LLM Factory (2026-11-01), which is where most of the timeout and
  malformed-argument history comes from.

---

## Part B. What others do (public sources, read 2026-11-11)

Each claim below was taken from the page linked. Several lookups ran out of search budget, so the
"not confirmed" list at the end of this part matters. Version numbers and dates are as the sources
gave them on the day.

### B1. Agent harnesses

| Harness | Where the loop runs, and how front ends attach | What is worth borrowing |
|---|---|---|
| Claude Code | Local process or a managed VM. Remote Control is outbound-only: the session registers with a relay and the transcript is stored for reconnect. | A first-byte timeout plus an idle-stream watchdog. Never re-running a request once content has streamed. A per-turn fallback model chain used only for overload. A goal mode judged by a separate small model with retry and check-in limits. Checkpoints with rewind. A capped memory index with topic files. [Docs](https://code.claude.com/docs/en/errors) |
| OpenAI Codex | A Rust core behind an app-server speaking JSON-RPC with Thread, Turn and Item primitives. A phone pairs with a desktop by QR through a relay. | The Thread/Turn/Item protocol with steer, interrupt, fork and revert. `apply_patch` as a grammar-constrained free-form tool (no JSON escaping). Stream idle timeout 300 s, 4 request and 5 stream retries. A reviewer agent with a denial circuit breaker. Memories built in the background with secrets redacted. [Docs](https://learn.chatgpt.com/docs/app-server) |
| OpenCode | One background service with an HTTP API. TUI, web and desktop are clients. Pairing by one-time QR. An end-to-end encrypted tunnel for remote use. | Schema-guided repair of tool arguments before validation. Separate header and chunk timeouts. Retry budgets per error class, with a long budget for an unreachable network. An explicit resync marker and idempotent control requests in place of event replay. [Source](https://github.com/anomalyco/opencode) |
| Pi | In-process loop. RPC over JSONL or a TypeScript SDK. Sessions are an append-only tree of entries. | The append-only session tree with typed entries. Tool arguments streamed as progressively complete objects. Invalid arguments returned as a tool error with the path and message. An experimental durable layer where a tool reruns after a crash only if declared safe to replay. [Repo](https://github.com/earendil-works/pi) |
| Hermes Agent | A long-running gateway is the only writer of sessions. Other front ends attach to it. | One writer per conversation with a durable input queue. A turn in flight at a crash becomes "unknown" and is never replayed automatically. Credential rotation then a fallback provider chain. Agent-written skills that can be staged for approval, aged and locked. Cheap scheduled wake-ups that can skip the model. [Docs](https://hermes-agent.nousresearch.com/docs/) |
| OpenClaw | One gateway per host. Phones and desktops connect as nodes with a device identity. | Two-layer device trust (pairing, then approval of each node's command surface). Idle, attempt and run timeouts (120 s idle for cloud models, 300 s for self-hosted). Failover scoped to the turn. An always-on guard that ends a turn after three identical failing calls. A phone that keeps a read-only cache and a bounded outbox. [Docs](https://docs.openclaw.ai/) |

Mistakes on record: OpenClaw's control UI trusted a URL parameter with a stored token and allowed
one-click code execution ([advisory](https://github.com/openclaw/openclaw/security/advisories/GHSA-g8p2-7wf7-98mq),
2026-01-31). Tens of thousands of gateways were found exposed to the internet. An open skill registry
carried 341 malicious skills out of 2,857 sampled
([report](https://thehackernews.com/2026/02/researchers-find-341-malicious-clawhub.html), 2026-02-02).
Permissive defaults (sandbox off, no prompts) are common to Pi and OpenClaw.

### B2. Reliability practice

- **Separate clocks.** First chunk, between chunks, per step and total are distinct settings in the AI
  SDK ([settings](https://ai-sdk.dev/docs/ai-sdk-core/settings)) and in LiteLLM.
- **Retry at one layer.** Three retries at each of five layers multiplies load 243 times
  ([AWS](https://builder.aws.com/content/3EumjoZascWd1oZiEgL8ORlv3qE/timeouts-retries-and-backoff-with-jitter)).
  Use capped exponential backoff with jitter, honor `retry-after`, and keep a retry budget.
- **A 200 response can still fail.** A stream may end in an error event. Once content has arrived,
  continue instead of retrying.
- **Strict schemas.** OpenAI and Anthropic both offer grammar-constrained tool arguments, with limits
  (OpenAI advises fewer than 20 tools. Anthropic allows 20 strict tools). vLLM constrains named and
  required tool choice but says `auto` "may occasionally be malformed" unless strictness is set
  ([vLLM](https://docs.vllm.ai/en/latest/features/tool_calling/)). llama.cpp converts JSON Schema to a
  grammar and silently skips unsupported features.
- **Measured effect.** OpenAI reported 100% schema adherence with constrained decoding against under
  40% without, on complex schemas. A vLLM write-up on one open model reached 99.9% request success after
  template fixes but only 76% schema accuracy without an enforcer
  ([vLLM blog](https://vllm.ai/blog/2025-10-28-kimi-k2-accuracy), 2025-10-28). Examples in tool
  definitions raised accuracy on complex parameters from 72% to 90% in Anthropic's test.
- **Repair before refusing.** `json_repair`, the AI SDK's `repairToolCall`, and OpenCode's repair plugin
  all coerce or regenerate arguments before returning an error.
- **Loop detection counts failed calls.** OpenRouter's detector observes at 2 identical calls, blocks
  at 3 and stops at 6, and counts invalid JSON as a repeat
  ([docs](https://openrouter.ai/docs/agent-sdk/call-model/doom-loop-detection.md)).
- **Durable execution.** Server engines (Temporal, Restate, DBOS) journal each step and replay
  completed ones. Anthropic's managed agents keep an append-only session log outside a stateless
  harness ([post](https://www.anthropic.com/engineering/managed-agents), 2026-04-08). Nothing published
  covers these engines on a phone. An embedded SQLite step journal is the inference drawn here.

### B3. Sync, transport and device identity

| Option | State | Fit for this stack (Swift, Kotlin, Python, later JavaScript) |
|---|---|---|
| Signed per-device append-only logs | A pattern, built in-house | Best fit for chat and tool events. Needs one shared core for identical behavior |
| Automerge with Keyhive and Subduction | Keyhive and Subduction are pre-alpha and say not to use them in production | Designed for exactly this problem. Too early. Swift and Python bindings are stale |
| Loro, Yjs | Active | Good for rich mutable documents. No Kotlin binding listed for Loro |
| PowerSync, ElectricSQL, Zero, Jazz 2, LiveStore | Active | Each needs a central service or is TypeScript-only |
| iroh | 1.0 on 2026-06-15, 1.4.1 on 2026-10-22. Relays cannot read traffic and are self-hostable | Official Swift, Kotlin and Python bindings expose endpoints and streams only. Android needs a source build ([docs](https://docs.iroh.computer/concepts/relays)) |
| libp2p | Main funding ended in 2026 | Lost to iroh |
| Messaging Layer Security (RFC 9420) | OpenMLS 0.9.0. `mls-rs` has UniFFI bindings and no full third-party audit | Commits need one agreed order. Decentralized variants are still drafts |
| Signal's device linking | In production | The model to copy for pairing, history transfer and a recovery key |
| Biscuit, UCAN | Biscuit has Rust, Wasm and Python. UCAN is at a 1.0 release candidate | No Swift or Kotlin binding for either. Usable through a shared core |

Platform limits that shape the design:

- iOS has no general way to run continuously in the background or to wake on a network request.
  `BGContinuedProcessingTask` (iOS 26) must start from a user action and shows a Live Activity.
  `LongRunningIntent` (iOS 27) extends an intent only while it reports progress. Background pushes are
  low priority and limited to two or three an hour.
- Android gives `dataSync` foreground services six hours in twenty-four.
- Only a holder of an Apple or Google push credential can wake a phone.
- The Secure Enclave holds only P-256 keys. Android StrongBox and the Windows TPM also support P-256.
- CloudKit needs Apple sign-in and does not work cross-platform. iCloud Keychain syncs only within
  Apple devices.
- iroh's documentation says about nine in ten network conditions allow a direct path. The rest need a
  relay.

### B4. Agent security

| Method | Guarantee | Standing |
|---|---|---|
| CaMeL ([2503.18813](https://arxiv.org/abs/2503.18813)) | A privileged model plans from the trusted request. A quarantined model with no tools reads untrusted data. An interpreter checks capability policies before each call | 77% of AgentDojo tasks with provable security. No adaptive break found |
| Multi-agent CaMeL ([2610.05640](https://arxiv.org/abs/2610.05640), 2026-10-05) | Carries provenance between agents on a separate channel | Per-agent guarantees do not compose without it |
| FIDES ([2505.23643](https://arxiv.org/abs/2505.23643)) | Confidentiality and integrity labels propagate through the plan. Consequential actions need a passing label policy | Deterministic. Over-tainting is the cost |
| Progent ([2504.11703](https://arxiv.org/abs/2504.11703)) | Symbolic rules over tool names and arguments. Narrowing a policy is automatic, widening needs approval | Deterministic. One small adaptive study |
| Design patterns ([2506.08837](https://arxiv.org/abs/2506.08837)) | After reading untrusted input the agent must be unable to trigger consequential actions from it | A principle with six patterns |
| Rule of Two (Meta, 2025-10-31) | At most two of: untrusted input, sensitive access, state change or external communication, without human supervision | A framing, usable as a computed budget |
| Spotlighting, classifiers, hardened models | Reduce attack success in static tests | All broken above 90% by adaptive attacks ([2510.09023](https://arxiv.org/abs/2510.09023)) |

Authority without a server:

- An individual IETF draft defines Ed25519 tokens whose holder derives a narrower child locally and
  signs a per-call proof over the canonical tool and arguments, verified with no network call. It notes
  that RFC 8693 needs a server round trip for each hop
  ([draft](https://www.ietf.org/archive/id/draft-niyikiza-oauth-attenuating-agent-tokens-01.html),
  2026-06-15).
- "Loopjacking" showed approvals being swapped after the user approved, and recommends binding the
  approval to a digest of the canonical action and re-checking at use
  ([2609.21081](https://arxiv.org/abs/2609.21081), 2026-09-17). "Consent Integrity" argues the approval
  display must come from a trusted mediator, not the agent
  ([2606.02668](https://arxiv.org/abs/2606.02668)).
- Witness cosigning specifications (C2SP) are small enough for devices to cosign each other's log
  checkpoints.

Protocol status: MCP `2026-07-28` is stable and stateless, with authorization optional and HTTP-only,
Tasks in beta and Apps stable. A2A 1.0 has been stable since March 2026, with JSON-RPC, gRPC and HTTP
bindings, and agent cards that may be signed. The Agent Client Protocol is local-only. None of them
defines authorization between devices that share no authorization server.

Gaps the survey found, which is where this spec aims: no system carries flow labels inside attenuated
tokens so a second device can enforce them offline. No MCP or A2A profile exists for authorization
without a server. Approvals bound to an action digest are standardized only for payments. Devices
cosigning each other's audit logs is open. Nobody has evaluated these defenses with small on-device
planner models.

### B5. Generative UI

| Protocol | Who defines components | Native rendering | Status |
|---|---|---|---|
| A2UI (Google) | The app developer, through catalogs the renderer advertises | SwiftUI, Compose (alpha), Flutter, web | v1.0 marked current on 2026-11-06 |
| AG-UI | None. It is an event transport | Client SDKs, no Swift listed | 1.0 |
| MCP Apps | The server author. Arbitrary HTML in a sandboxed frame | None | Final. Part of MCP `2026-07-28` extensions |
| OpenAI Apps SDK | The app developer (HTML widget) | None | Live. Implements MCP Apps |
| json-render, OpenUI | The developer | React Native or web only | Pre-1.0 |

Findings:

- Two camps have formed: model-written HTML in a frame (MCP Apps, Apps SDK) and typed catalogs (A2UI).
  None of the catalog protocols defines authorization, voice or cross-device state.
- A fine-tuned 4-billion-parameter model reached about 98% of a frontier model's quality on catalog UI
  ([2609.04184](https://arxiv.org/abs/2609.04184)). Constrained decoding raises schema validity from
  79–93% to 100% on small models, with a semantic gap remaining
  ([2609.23742](https://arxiv.org/abs/2609.23742)).
- Apple's Foundation Models framework does constrained generation against a schema built at run time.
- Approval dialogs are the main attack surface: model-written HTML can imitate a host's native prompt,
  and images and links remain an exfiltration path.
- No published work was found on a device adapting a UI description for itself, on one description
  rendered as screen, glance and speech, or on a generated surface replicated between devices.

### B6. Voice and on-device models

| Platform | Speech to text | Fast on-device model | Text to speech |
|---|---|---|---|
| iOS and macOS 26+ | SpeechAnalyzer with SpeechTranscriber | Foundation Models (3B on recent iPhones. Larger on the newest hardware), constrained sampling and tool calling | A compact neural voice (Pocket TTS, Kokoro) with the system synthesizer as fallback |
| watchOS | None on the watch | None on the watch | System synthesizer. The watch is a thin audio client |
| Android | ML Kit GenAI speech recognition (alpha). sherpa-onnx as fallback | Gemini Nano through ML Kit (function calling in preview). LiteRT-LM | ML Kit GenAI TTS (alpha). sherpa-onnx |
| Windows | sherpa-onnx or Moonshine | Foundry Local or llama.cpp | Pocket TTS, Kokoro |

- Shared front end: Silero or TEN voice-activity detection, and an audio-only end-of-turn model (Smart
  Turn v3, 8 MB, 12–95 ms on CPU).
- No full-duplex speech-to-speech model is phone-ready with dependable tool calls. The cascade
  (detect, transcribe, generate, speak) is the on-device answer.
- A sub-second first response needs speculative generation on partial transcripts, a prewarmed
  recognizer and model, and pre-synthesized acknowledgements. The budget from end of speech to first
  audio adds up to about 600–1,050 ms without them.
- Interruption: keep echo cancellation on, open a short decision window before stopping, cancel model
  and speech on a real interruption, and commit assistant text to history only as its audio plays.
- Small models route and acknowledge well. They are slow and weak at multi-step tool use. On a current
  iPhone a 2.6B model took 8 s for a 1,024-token prompt
  ([analysis](https://artificialanalysis.ai/articles/mobile-phone-intelligence-inference), 2026-08-24).
- Audio injection works against audio-language models (79–96% success in one study,
  [2604.14604](https://arxiv.org/abs/2604.14604)). Spoken consent can be replayed or cloned.

### B7. Long-running and self-improving agents

- Long runs: an initializer session writes a feature list and a progress file, and worker sessions
  each finish one item behind a test
  ([Anthropic](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents),
  2025-11-26). A separate skeptical evaluator with hard thresholds. Compaction with a verbatim recent
  tail and notes files.
- Self-improvement with a measured effect: evolving playbooks by small deltas (ACE,
  [2510.04618](https://arxiv.org/abs/2510.04618)), prompt search against a metric (GEPA), skill
  libraries, and background memory consolidation that writes to a separate store for review.
- Self-modifying agents have cheated their own checks: one system faked test logs and removed
  detector markers ([2505.22954](https://arxiv.org/abs/2505.22954)). Memory can be poisoned through
  ordinary queries, and public skill registries carry malicious skills.
- The gating pattern that answers these: the model proposes into a staging area. Deterministic checks
  and a replay evaluation decide. Learned items never carry authority. Every adoption is versioned and
  reversible.

### B8. Not confirmed

- Exact dates for AG-UI 1.0, AP2 0.2 and A2A 1.0.
- Whether Claude Code's built-in tools use strict schemas. Codex model fallback. OpenCode's current
  event wire format.
- Latency of Apple's SpeechTranscriber and of the compact neural voices on phones. Battery cost of
  continuous listening. Whether voice processing works on Apple Watch hardware.
- A reported hang of iroh's Kotlin artifact on Android. Whether iroh in a browser is relay-only.
- Any measurement of loop-detector effectiveness, or of durable execution on iOS or Android.
- Native-app support for the passkey PRF extension.
