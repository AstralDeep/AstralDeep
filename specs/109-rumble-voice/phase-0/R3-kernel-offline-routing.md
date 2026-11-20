# R3: Kernel offline routing

Revisions read: AstralKernel `4c43032` (`origin/main`), AstralProjection `54e7ff1` (`origin/main`). Paths are relative to each repository's root. Kernel paths are under `crates/`. Swift paths are under `apple-clients/AstralApp/AstralApp/`. Nothing was built or run. Every claim below was read from the files unless marked **assumed**.

## The answer

| Need | Verdict |
|---|---|
| 1. A clear signal to the host that the provider is unreachable, timed out or refused | **Partly exists.** The kernel classifies the failure precisely, but only the English notice sentence reaches the host. There is no structured field, no error code and no state. |
| 2. Serve a turn from the on-device model in the same chat while the provider is down, logged as ordinary chat text and marked, with nothing sent out | **Partly exists.** In-turn fallback to the host's `OnDeviceModel` exists and is automatic. The signed log already marks the answer. But the kernel always tries the provider first on every turn, so "no request carrying the person's words leaves the device" cannot be guaranteed, and the chat read model does not expose the mark. |
| 3. A content-free check that the provider is back | **Missing.** Nothing in the kernel or its FFI probes a provider without sending a chat completion. |

A kernel change is needed. It is small to medium: four additive pieces, about 300 lines of Rust plus tests, no change to existing behavior when the new route is left at its default. Section "Proposed change" gives the detail.

## How routing works today

### The on-device model is a last slot in every turn's chain

The host passes an optional `OnDeviceModel` when it opens the kernel. The FFI trait is at `astral-kernel-ffi/src/turns.rs:72` and `Kernel::open` wraps it in `HostOnDevice` at `astral-kernel-ffi/src/lib.rs:156`. The kernel carries it as `TurnParts.on_device` (`astral-kernel/src/turns.rs:90`). The engine-side trait is `astral-engine/src/host.rs:259` with `available()` and `complete()`.

`ModelClient::new` builds the candidate chain. It adds one slot per configured provider, model and credential, then adds `Slot::OnDevice` last if a host model was supplied (`astral-engine/src/provider/client.rs:240-241`). With no providers configured, the on-device model is the only slot.

The Apple host always supplies one. `MeshAppMode.swift:104` passes `PreferredOnDeviceModel()`. That type tries Foundation Models first and then the installed LFM3 weights (`OnDevice/OnDeviceFallbackModel.swift:7-31`). The Swift bridge `KernelOnDeviceModel` (`Mesh/KernelHostBridges.swift:101-147`) turns Swift outcomes into `HostCallError.Failed` strings such as `on_device.schema_unsupported`.

### A new client is built for every turn, so there is no sticky offline state

`TurnService::claim_with` constructs a fresh `ModelClient` for each turn (`astral-kernel/src/turns.rs:537-556`). Each client starts at slot 0 through `begin_turn` (`client.rs:271`, `engine.rs:525`). The per-turn `Health` counters (`provider/health.rs:32-95`) die with the client. `Health::snapshot` exists (`client.rs:275`) but nothing in `astral-kernel` or `astral-kernel-ffi` calls it. So every turn starts by trying the real provider again.

### What happens when the provider fails

`ModelClient::complete` walks the slots (`client.rs:345-448`). For a remote slot it runs `remote_with_retries` (`client.rs:456`).

- **Connect failure or connect timeout.** `map_send_error` maps `is_connect() || is_timeout()` to `ProviderError::Unreachable` (`provider/http.rs:54-63`). In the retry loop, if on-device is allowed and `available()` is true, `Unreachable` returns immediately to the chain walker (`client.rs:581-588`). If on-device is not available, the client waits and probes the network for up to `network_wait_budget`, which is 3600 s (`client.rs:73`), and emits `WaitingForNetwork` notices.
- **Timeouts after connect.** `Timeout(FirstToken | Idle | Attempt)` is retryable (`error.rs:74-80`). Defaults are 10 s connect, 60 s first token, 120 s idle for cloud, and 180 s and 300 s for self-hosted (`provider/deadlines.rs:23-35`). Up to `max_attempts` = 3 tries with 1 s and 2 s jittered backoff (`client.rs:73`, `provider/retry.rs:29-35`). **Assumed from the constants, not run:** a provider that accepts the connection and then stalls costs about three minutes before fallback.
- **HTTP status.** `fallback_eligible` is true for 404, 408, 429 and 5xx, and for `Unreachable`, `Timeout`, `Stream` and `Withheld` (`error.rs:82-92`). It is false for 400, 401, 403 and 422, so a refusal or bad credential ends the turn with code `provider.refused` or `provider.auth` and the on-device model is never called. The test `the_on_device_model_never_answers_a_refusal` pins this (`provider/client_tests.rs:619`).
- **Condition for calling the on-device model.** The chain walker moves to the next slot when `e.fallback_eligible()` holds and the continuation limit is not exceeded (`client.rs:423-428`). `on_device_once` (`client.rs:644`) then checks admission, checks `available()` again (`client.rs:656`), builds the prompt from the full message history, and calls `complete`. Slots denied by the PHI gate are skipped (`client.rs:364`, filter built at `engine.rs:960`, set computed by `allowed_model_set` at `engine.rs:536`). Tests: `the_on_device_model_is_last_and_comes_with_a_notice` (`client_tests.rs:572`), `an_unavailable_on_device_model_is_skipped` (`client_tests.rs:634`), `disallowed_remote_candidates_are_skipped_in_favor_of_an_allowed_on_device_model` (`client_tests.rs:666`).
- **A question that was waiting is answered locally.** The fallback happens inside the same turn with the same request, so the waiting question is answered by the on-device model without a repeat. This already satisfies that sentence of spec 5.3.
- **Tools.** If the request has tools and no response schema, the kernel asks the on-device model for a constrained tool plan (`provider/on_device.rs:34-36`, `client.rs:672-680`). If the host answers `on_device.schema_unsupported`, the kernel retries as plain text and emits `OnDeviceTextOnly` (`client.rs:681-690`). `VerifiedLFM3OnDeviceModel` returns that failure for any schema (`OnDeviceFallbackModel.swift:55`). So "no tool runs" holds today only because the host model refuses schemas. The kernel does not enforce it. If a host model accepts the schema, its tool proposals run through the normal gates.

### What reaches the log and the host

- **Fallback notice.** Before the on-device call, `on_notice(Fallback{kind: OnDevice, from, to})` fires (`client.rs:366-382`). Its text is `"{from.model} did not respond. This turn is using on-device."` (`client.rs:116-130`, English, built from `Candidate`, with no error code). `TurnObserver::on_notice` writes it as a non-final `chat.model_response` event with a `notice` string (`engine.rs:669-686`, `journal.rs:164-172`). The test `the_fallback_notice_is_journaled` pins the shape (`engine_tests.rs:1330`).
- **Event to the host.** `ObservingSink::announce` turns that event into `TurnEvent::Notice{chat_id, turn_id, text}` (`astral-kernel/src/turns_events.rs:145-162`). The FFI forwards it as `TurnProgress::Notice` with the same three fields (`astral-kernel-ffi/src/turns.rs:265-280, 334-344`), delivered through `TurnListener::on_event` (`turns.rs:417`). Order is preserved: the notice is appended before the on-device call, so the host sees it before any `TextDelta`.
- **Answer and mark.** The answer streams as `TurnProgress::TextDelta`. The turn writes a final `chat.model_response` whose body carries `provider` and `model` of the winning candidate (`engine.rs:1019`, `journal.rs:174-196`), which is `"on-device"` and `"on-device"` for a local answer (`client.rs:38, 337-343`). It then writes a `chat.assistant_message` with only `{turn, text}` (`journal.rs:239`, `engine.rs:720-733`). So the signed log already records which model answered. The assistant text is ordinary chat text, and later provider turns read it back as history.
- **Failure with no local model.** The turn ends with `TurnProgress::Ended{reason:"failed", code}` and a code such as `provider.unreachable`, `provider.timeout.first_token`, `provider.auth` or `provider.no_candidate` (`error.rs:51-72`, `engine.rs:983`).
- **Chat read model.** `ChatMessage` and `TurnInfo` in the FFI have no provider or model field (`astral-kernel-ffi/src/chats.rs:139-167`, built from `astral-log/src/fold/chat.rs:184-192, 625-643`). The fold already visits `ChatModelResponse` events (`fold/chat.rs:507-520`) but does not keep the provider.
- **Routing control.** There is none. `run_turn`, `run_message` and the other entry points take only a chat id and message id (`astral-kernel-ffi/src/turns.rs:562-645`). `claim_with` has one existing branch that builds a client with an empty provider list, used for cancel-only (`astral-kernel/src/turns.rs:547-551`). Providers can be added or removed but that is configuration, not a per-turn route.

### What the Apple host sees today

- The host maps `TurnProgress.notice` to `MeshTurnEvent.notice(String)` (`Mesh/KernelMapping.swift:284`). `MeshRuntime.apply` stores the text in `state.notice` while a turn runs (`Mesh/MeshRuntime.swift:2262-2263`) and clears it at the end. The chat view shows it as a status line (`Mesh/MeshChatViews.swift:416-420`). Nothing parses it.
- `turn.answeredOnDevice` is declared at `MeshRuntime.swift:203` and rendered at `MeshChatViews.swift:440`, but no production code sets it. Only tests assert it is `false`. So today a locally answered turn shows no on-device mark.
- A voice utterance goes to `submitVoice` (`MeshRuntime.swift:1946`) and then `send(text, speaking: true)` (`MeshRuntime.swift:1480`). Assistant deltas go to the speech sink at `MeshRuntime.swift:2257-2259`. `recordSpoken` writes `chat.spoken` at `MeshRuntime.swift:1965`.
- Failure codes arrive as `.failed(text)` or `.refused(code, message)` after mapping (`KernelMapping.swift:298-322`). Note `isPhiRefusal` treats `provider.no_candidate` as a PHI refusal (`KernelMapping.swift:324`), which would misread "provider down and no local model" as a PHI refusal. **Assumed** to matter only when no local model is installed.

### Event contract

There is no `contracts/event.md` in either tree. The event type names are Rust constants in `astral-engine/src/journal.rs:13-23`. The final `model_response` carries `provider` and `model` (above), so the mark exists in the signed log. A notice event carries `notice` text only.

## Gaps against the spec

1. **Signal (9.4 "Going offline").** The host can only match English text. The text names the failed model, not the cause. A slow answer does produce the same notice after the retries above, so the host cannot tell a timeout from an unreachable host from a 503. The three causes the spec names are already distinguished inside the kernel (`ProviderError::code`, `error.rs:51-72`) and then dropped.
2. **Local serving (9.4 "While offline").** Two parts fail. First, every turn sends the request to the provider before falling back. A connect failure never delivers the body, but a 5xx or a stalled stream after connect does, so the guarantee "no request carrying the person's words leaves the device" is not met. Second, each offline turn pays the failed attempt's latency, up to a few seconds for refused connects and about three minutes for a stall. The spec's "marked as answered on this device" is in the signed log but is not readable by the app from the chat view.
3. **Checking (9.4 "Checking").** No content-free probe exists. The only way to learn the provider is back is to send a real turn.

## Proposed change

Four additive pieces. Each is independent, so they can land as one kernel PR with one Projection pin bump.

### A. A structured fallback event

- `astral-engine/src/provider/client.rs`: add `reason: String` (the `ProviderError::code()` of `last_error`) to `ModelNotice::Fallback`, filled where `announced_from` is consumed (`client.rs:366-382`). `ModelNotice::text()` is unchanged.
- `astral-engine/src/engine.rs` (`TurnObserver::on_notice`, `engine.rs:669`) and `journal.rs:164` (`partial_body`): when the notice is a `Fallback`, add `"fallback": {"kind": "credential"|"model"|"on_device", "reason": "<code>"}` to the body. The extra key is additive. **Assumed:** the replay in `journal.rs` ignores unknown keys. A journal test must confirm it.
- `astral-kernel/src/turns_events.rs`: new `TurnEvent::ModelFallback{chat_id, turn_id, kind, reason}` emitted from `announce` next to the existing `Notice`. The notice stays, so older hosts and the status line keep working.
- `astral-kernel-ffi/src/turns.rs`: new `TurnProgress::ModelFallback{chat_id, turn_id, kind: String, reason: String}` and the `From` arm. This is an enum addition, so the Swift `switch` in `KernelMapping.turn` must handle it in the same change.
- Swift: map to a new `MeshTurnEvent.modelFallback(kind:reason:)`. `MeshRuntime.apply` hands `kind == "on_device"` with a reason in {`provider.unreachable`, `provider.timeout.*`, `provider.unavailable`, `provider.rate_limited`, `provider.model_not_found`, `provider.stream`} to the voice session as "provider offline".
- Refusals. 400, 401, 403 and 422 do not fall back (`error.rs:82-92`) and end the turn with `provider.refused` or `provider.auth`. Whether spec 9.4's "refused the request" means those is a call for the owner. The recommended reading is that it means 429 and 5xx only, because falling back silently on a bad credential would hide a setup error. That needs no kernel change. The host sees the code on `Ended`.

### B. A per-device model route

- `astral-kernel/src/turns.rs`: add `ModelRoute { Auto, OnDeviceOnly }` held in `TurnService` (an `AtomicU8` or a small mutex). In `claim_with` (`turns.rs:537`), build the client from `ModelClientConfig::new(vec![], vec![])` when the route is `OnDeviceOnly`, exactly as the cancel-only branch does at `turns.rs:547-551`. The slot list is then `[OnDevice]` (`client.rs:240-241`), so no request is formed for any remote endpoint. The PHI gate, admission and journal writes are unchanged, because they run in the engine and `admit_on_device` (`client.rs:624`).
- `astral-kernel-ffi/src/turns.rs`: `Turns::set_model_route(route)` and `Turns::model_route()`. A default of `Auto` keeps every current caller unchanged.
- The route is process state, not a log event. After a restart it is `Auto`, and the first provider failure repeats the notice. That matches "each change of state is announced once" for a voice session that has just started.
- The log is unchanged for these turns: final `model_response` with `provider = "on-device"`, then `chat.assistant_message`. The next `Auto` turn reads those messages as history, as the spec requires.
- Tool use offline. To make "no tool runs" a kernel guarantee rather than a host habit, add an `OnDeviceOnly` rule in `claim_with` or the engine config that sends the engine an empty tool list. **Assumed** to be a few lines in `Runner::tool_defs`. If the owner accepts relying on LFM2.5 returning `schema_unsupported`, skip it.

### C. A content-free provider probe

- `astral-engine/src/provider/wire.rs`: extend `ModelTransport` with `async fn probe(&self, base_url, kind, credential, connect_timeout) -> Result<(), ProviderError>`. A default body returns `Ok(())` only on test transports, and test fakes override it.
- `astral-engine/src/provider/http.rs`: real implementation issues `GET {base_url}/models` through the same `guarded_client` (so the egress allow list and TLS rules apply) with the stored credential and a 5 s limit. It never sends a body or any chat data. Classify as Reachable when any response below 500 arrives (including 401 and 403, which mean the server is up but the key is wrong), `Unreachable` on connect failure or timeout, and `Status` for 5xx. **Assumed:** OpenAI-compatible endpoints answer `/models`. If a provider does not, a 404 still proves reachability, and the first real turn after "back online" is the final check.
- `astral-kernel/src/turns.rs`: `TurnService::probe_model_provider(name: Option<String>)` reads the default provider from `ProviderBook`, takes its first usable credential, calls the transport, and returns a `ProviderProbe { Reachable, Unreachable, Refused(code), NotConfigured }`. It writes nothing to the log and meters nothing against the allowance, so a 15 s timer cannot grow the log.
- FFI: `Turns::probe_model_provider(name) -> ProviderProbe` as an async uniffi call. Swift calls it before each voice turn and on the 15 s to 60 s timer.
- Spec 9.4 requires the probe to carry no conversation content. This design satisfies that by construction, since `probe` never receives a `ModelRequest`.

### D. Expose which model answered

- `astral-log/src/fold/chat.rs`: add `answered_by: Option<String>` to `TurnInfo` (`fold/chat.rs:184`). In the existing loop over `ChatModelResponse` events (`fold/chat.rs:507-520`), keep `provider` from the last event whose body has `final: true`.
- `astral-kernel-ffi/src/chats.rs`: pass it through in the `TurnInfo` record (`chats.rs:156-167, 328-340`).
- Swift: `MeshRuntime` sets `answeredOnDevice` from `answered_by == "on-device"` when it reloads the chat, which finally drives the existing label at `MeshChatViews.swift:440`. The label text there ("Add a model in Settings for fuller answers") is wrong for a provider outage and needs its own wording, which is a Projection copy decision.

### Tests

- Engine: `Fallback` carries the right `reason` for 503, unreachable, first-token timeout and 429. A 400 produces no fallback event. The journal replay ignores the new `fallback` key. The existing `the_fallback_notice_is_journaled` (`engine_tests.rs:1330`) keeps passing.
- Kernel: `OnDeviceOnly` never calls the transport (assert `transport.models()` is empty, using the pattern at `engine_tests.rs:1382-1400`). The exchange lands as user message, final `model_response` with `provider = "on-device"`, assistant message. A following `Auto` turn includes it in the request sent to the fake provider. A PHI-denied on-device candidate still fails closed in `OnDeviceOnly`. Cancel and resume paths (`turns.rs:602, 728, 812`) still work with the route set.
- Probe: reachable, 401, 503, connect refused, timeout, no provider configured. Assert no log append and no allowance metering. Assert the request carries no body fields from any chat.
- FFI: `turns_tests.rs` gains cases for the new event, the route setter and the probe record, following `the_on_device_adapter_passes_roles_schema_and_limits_to_the_host` (`turns_tests.rs:368`).
- Swift: `KernelMapping` maps `modelFallback`. `MeshRuntime` flips voice state to offline on the first event, does not repeat on later events, and returns to `Auto` when the probe passes.

### What the Swift host would call

1. On `TurnProgress.modelFallback` with kind `on_device`: speak the offline sentence, then call `turns.setModelRoute(.onDeviceOnly)`. The waiting question is already being answered by the kernel in that same turn.
2. While offline: each utterance goes through `submitVoice` as today. No other call changes.
3. Timer: `await turns.probeModelProvider(name: nil)` every 15 s, backing off to 60 s, and once before each turn as the spec asks. On `.reachable`, wait for a pause, speak the back-online sentence, then call `turns.setModelRoute(.auto)`.
4. If the first `Auto` turn fails again, a new `modelFallback` event repeats the cycle.
5. Chat view: set `answeredOnDevice` per turn from `TurnInfo.answeredBy`.

### What does not need the kernel

- The "LFM2.5 in voice, Foundation Models in typed chat" rule (C12) is a host choice. The kernel takes one `OnDeviceModel` per kernel instance. `PreferredOnDeviceModel` would need to know whether the current turn started from voice. The turn is not labeled voice in the kernel's call (`OnDeviceRequest` carries messages, schema and token limit only, `host.rs:246-251`). **Assumed:** the host can hold a "voice session active" flag that `PreferredOnDeviceModel.complete` reads. That is a Projection change and a race to check in phase 3: a typed turn during a voice session would also see LFM2.5.
- Spoken sentences use `record_spoken_for_turn`, which already exists (`MeshRuntime.swift:1965`).

## What I could not determine

- Whether journal replay tolerates the extra `fallback` key. I read `partial_body` and the fold's `notice` check (`journal.rs:407`, `turns_events.rs:153-158`) and saw no strict-schema check, but I did not read every consumer. Not run.
- Whether `ModelClientConfig::new(vec![], vec![])` is fully safe for a normal turn rather than the cancel-only path. The slot logic and `on_device_once` do not use the provider list. The cancel-only caller may never reach `complete`. A kernel test is needed. **Assumed** safe.
- Whether the owner's provider answers `GET /models`. The provider is user-configured (any OpenAI-compatible endpoint), so this cannot be settled from the repository.
- How long the first offline turn takes against a provider that accepts connections and then stalls. Derived from constants (60 s first token, 3 attempts), not measured. If this matters for voice, a shorter first-token deadline for voice turns is a separate decision.
- Whether the Apple `FoundationModelsOnDeviceModel` accepts a tool schema (`Mesh` bridge path `OnDeviceModelAdapter.swift:203` returns `schemaUnsupported` on a schema error, but I did not read its success path). It matters for the offline "no tools" rule only in typed chat.
- Whether any other host (Android, Windows, CLI) consumes `TurnProgress` exhaustively. The Rust CLI was not checked. Adding the enum variant would break an exhaustive consumer at compile time.
