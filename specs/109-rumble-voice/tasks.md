# Tasks for spec 109

**Date:** 2026-11-20. **Status:** phase 0 is done, see [phase-0/README.md](phase-0/README.md). The owner picked Phonon 31m on
2026-11-20 (T013, O13). Phase 1 is built and tested on three local Projection branches merged into
`feat/109-rumble-voice`, not pushed. Nothing from phase 2 on is built.
**Companions:** [spec.md](spec.md), [plan.md](plan.md), [research.md](research.md).

Each task names its repository. Projection paths are relative to `apple-clients/`. A task marked (P)
can run beside the others in its phase. Every code task includes its tests. Ids from T130 on were added
after phase 0 and sit in the phase they belong to.

## Phase 0. Find out

- [x] T001 (P) Projection. Measure Rumble partial transcription every 0.5 s on the iPhone 13 Pro with
  the lab app. Record time per partial for 2, 5 and 10 second segments, and whether an LFM2.5 draft and
  one synthesized sentence fit between partials. R1.
- [x] T002 (P) Projection. Check the pinned llama.cpp revision for the short-clip padding fix. Name the
  revision to pin. R2.
- [x] T003 Kernel. Document how the host learns that the provider is unreachable and how a turn is
  served by the on-device model in the same chat. Propose a change if one is needed. R3.
- [x] T004 (P) Projection. Build llama.cpp with `libmtmd` for macOS arm64 and run Rumble and LFM2.5 in a
  hardened, signed host. R4.
- [x] T005 (P) Projection. Confirm FluidAudio loads a voice from a local folder with no network
  request, and that each candidate's files download without a terms prompt. R5.
- [x] T006 (P) Projection. Establish what the paired-watch relay needs from the phone app's state. R6.
- [x] T007 (P) Projection. Check voice processing on watchOS. R7.
- [x] T008 (P) Projection. Review the phone memory gate against measured peaks. R8.
- [x] T009 Deep. Collect the licence files for LFM2.5, FluidAudio and the three voice candidates.
  LFM2.5 is cleared by O12. R9.
- [x] T010 (P) Projection. Score LFM2.5 acknowledgments in eight languages against section 8.2. R10.
- [x] T011 (P) Projection. Measure Rumble's false words on coughs, room noise and clips under one
  second. R11.
- [x] T012 Projection. Build a small test app that runs Kokoro-82M, Phonon 31m and Pocket TTS small on
  the iPhone 13 Pro with Rumble and LFM2.5 loaded. Measure first audio and memory for ten lines. R12.
- [x] T013 Owner. Listen to the recordings from T012 and pick a voice, or choose the system voice. Done
  2026-11-20: Phonon 31m (O13).
- [x] T014 Deep. Write the phase 0 results into `specs/109-rumble-voice/phase-0/`.

## Phase 1. Runtime and pack

- [x] T020 Projection. Add `scripts/build-voice-runtime.sh`, pinned at llama.cpp
  `8af68d486a9473820d24d00af8f33334ca80124d`, producing the llama.cpp xcframework for iPhone, simulator
  and Mac with `MTMD_VIDEO` off. Ignore the outputs in Git.
- [x] T021 Projection. Create the `AstralVoiceRuntime` package with binary targets and a C shim.
- [x] T022 Projection. Recognition: load Rumble and its encoder, transcribe 16 kHz audio with a
  language, stream tokens, cancel. Pad each clip with zeros to `16000*k - 160` samples, where k is the
  smallest whole number of seconds that fits, and test the padding on short clips.
- [x] T023 (P) Projection. Create the `AstralVoiceSpeech` package for Phonon 31m on FluidAudio `v0.18.4`,
  pinned by commit `e0c6103b`. Behind a speech protocol: load from the verified folder with
  `ModelHub.offlineMode` on, set before any FluidAudio call, and only the load-from-folder API. Tests
  point the registry URL at a loopback listener and assert zero connections. Synthesize one sentence to buffers, stream, cancel. Report unavailable below iOS 18 and macOS 15.
- [x] T024 (P) Projection. Text generation: load LFM2.5, generate with a token limit, stream, cancel.
- [x] T025 Projection. One scheduler so recognition and generation never use the GPU together, with
  recognition first.
- [x] T026 Projection. Fixture models and audio small enough to commit, with tests that run in the
  simulator on CPU.
- [x] T027 Projection. Define and parse the app's own pack manifest: repository, revision, path, bytes
  and SHA-256 for each file. It lists the three GGUF files and the Phonon Core ML files and no Piper
  archive.
- [x] T028 Projection. A sibling store, `VoicePackStore`, for two repositories, sharing the existing
  download and digest code. The existing LFM3 store is untouched until phase 2. The store is the only
  code that downloads. It skips the voice files on a device that cannot run Phonon.
- [x] T029 Projection. The partial outcome: GGUF files installed, voice pending, with later retry.
- [x] T030 Projection. Bundle the manifest. Keep the mirror setting and its HTTPS rule.

## Phase 2. Swap the models

- [ ] T040 Projection. Split `VoiceModelInference` into recognition, speech and local text.
- [ ] T041 Projection. `RumbleTranscriber` implementing `VoiceStreamingTranscriber` with partials.
- [ ] T042 (P) Projection. `LocalVoiceSpeech` playing Phonon from the `AstralVoiceSpeech` package into the
  duplex engine through one resampler. On iOS 17 and macOS 14 it reports unavailable and English uses the
  system voice.
- [ ] T043 (P) Projection. `SystemVoiceSpeech` using `AVSpeechSynthesizer.write` only. Pick the default
  installed voice for the locale. Exclude personal and novelty voices.
- [ ] T044 Projection. `ConversationLanguage` with the eight languages and their locales. Save the
  choice per device.
- [ ] T045 Projection. The automatic switch from the local voice to the system voice, with its notice.
- [ ] T046 Projection. `LFM25OnDeviceModel` replacing `VerifiedLFM3OnDeviceModel` at
  `Mesh/MeshAppMode.swift`.
- [ ] T047 Projection. Load on overlay open, warm the voice in parallel, unload after 60 s or on
  memory pressure.
- [ ] T048 Projection. Delete `AstralVoiceModel`, the MLX dependency and the LFM3 manifest.
- [ ] T049 Projection. Delete installed LFM3 weights on first launch.
- [ ] T050 Projection. Port `MLXVoiceRuntimeTests`, `OnDeviceVoicePipelineTests`,
  `OnDeviceVoiceControllerTests` and `VoiceModelStoreTests` to the new runtime.
- [ ] T051 Projection. Link `AstralVoiceRuntime` and `AstralVoiceSpeech` into the app project. The app
  jobs in Apple CI build the llama.cpp runtime before they build the app.
- [ ] T135 Projection. Route the pack store's background download session id in the app delegate, as the
  LFM3 store's id is routed today.
- [ ] T134 Projection. A build step that deletes FluidAudio's `LuxTtsG2pResources` bundle from the built
  app, and a test that asserts the bundle is absent. Check that App Store validation and signing accept
  the deletion.

## Phase 3. Natural conversation

- [ ] T060 Projection. Continuation word lists for eight languages in `VoiceEndOfTurnDetector`, plus
  the mid-number and mid-list extension.
- [ ] T061 Projection. Speaker-level onset threshold in the activity detector.
- [ ] T062 Projection. Word-confirmed interruption in `BargeInController`: duck, transcribe the
  overlap, then continue or stop. It sits behind the gate in T130.
- [ ] T130 Projection. The voiced-frame gate: at least five voiced frames of 25 ms taken every 10 ms
  (autocorrelation above 0.5 between 60 and 400 Hz, within 30 dB of the loudest frame, above minus 55
  dBFS) and a 0.4 s minimum clip before Rumble transcribes overlapping audio. Test noise, a cough, a
  sneeze and real stop words.
- [ ] T136 Projection. Treat the last word of a partial transcript as provisional. A draft, a
  continuation check or an interruption check that rests on it waits for the next partial or the final
  transcript.
- [ ] T131 Projection. Skip a partial tick while the previous partial is still running, so partials
  never queue. Test it with a fake clock and a slow recognizer.
- [ ] T063 (P) Projection. Backchannel and stop-word lists for eight languages. Matching ignores case,
  punctuation and spacing, so "M.M.H.M." counts as "mm-hm".
- [ ] T064 Projection. Resume from the interrupted sentence after noise or a backchannel.
- [ ] T065 Projection. Replace the six fixed phrases in `VoiceFastPath` with model drafts. The draft
  starts at the speculation point and runs in the silence window. Model drafts ship in English, Spanish,
  French and German only.
- [ ] T132 Projection. The opener stem list: code picks the stem from a rotated list per language and
  passes it to the model. An allow-list of stems rejects answers in disguise. Measure it on a device
  before T065 and T066 are built on it, because phase 0 did not test it.
- [ ] T066 Projection. The acknowledgment checker with every rule in section 8.2, including the stem
  allow-list from T132.
- [ ] T067 Projection. The quick-answer window and the small-talk rule.
- [ ] T068 (P) Projection. Generic fallback lines for eight languages, rotated. They are the only
  fillers in Mandarin, Arabic, Japanese and Korean until a native speaker has reviewed the Mandarin and
  Arabic drafts.
- [ ] T069 Projection. Progress lines from kernel events, at most one every 15 seconds.
- [ ] T070 Projection. Send the short-answer and reply-language preference with voice turns.
- [ ] T071 Projection. Speak a component's `spoken` form and strip markup from text.
- [ ] T072 Kernel. The change from T003, in four pieces with their tests. Built on the Kernel branch
  `feat/109-offline-route`, open as draft Kernel PR 22, unmerged. It needs the owner's review before it merges and before
  phase 3 starts (spec section 9.3).
  - (a) A structured fallback event: a reason code on the fallback notice, an additive `fallback` journal
    key, `TurnEvent::ModelFallback` and `TurnProgress::ModelFallback`. Tests: the reason for 503,
    unreachable, first-token timeout and 429, no event for a 400, and journal replay that ignores the new
    key. The Swift `switch` in `KernelMapping.turn` changes in the same change.
  - (b) An on-device-only model route: `ModelRoute { Auto, OnDeviceOnly }` with an FFI setter and getter
    and an empty tool list in that route. Tests: the transport is never called, the exchange lands in the
    log with provider `on-device`, a following `Auto` turn sees it as history, PHI denial still fails
    closed, and cancel and resume still work.
  - (c) A provider probe with no body and no log write: `GET {base_url}/models` through the guarded
    client, 5 s limit. A 408, a 429 and any 5xx count as not back, any other response as reachable. Tests:
    reachable, 401, 408, 429, 503, connect refused,
    timeout, no provider, no log append, no metering and no chat data in the request.
  - (d) An `answered_by` field on the turn in the chat read model, passed through FFI. Test: it reads
    `on-device` after an offline turn.
- [ ] T073 Projection. Speech during a running job becomes the next provider turn. Stop cancels a
  cancel-safe job.
- [ ] T074 Projection. Going offline: detect the kernel fallback event of kind `on_device`, speak "Your
  LLM model provider is offline." once, set the on-device-only route, show Offline. Only unreachable,
  timeout, stream failure, 404, 408, 429 and 5xx count. A 400, 401, 403 or 422 shows as an error.
- [ ] T076 Projection. While offline: serve each turn from the on-device model through the kernel so it
  lands in the chat as text, marked as answered on this device from `answered_by`. Replace the label
  text at `MeshChatViews.swift:440`. Test that no request carries the person's words.
- [ ] T077 Projection. Coming back: call the provider probe before each turn and on a backoff timer,
  speak "Your LLM model provider is back online." at the next pause, then set the route to `Auto`.
- [ ] T078 (P) Projection. Both sentences in eight languages, and as text notices in typed chat.
- [ ] T075 Projection. A timing recorder for the moments in section 8.5, off by default, used by the
  live pass.

## Phase 4. Overlay and Settings

- [ ] T080 Projection. `VoiceOverlayView` with its states and controls for iPhone and Mac.
- [ ] T081 (P) Projection. The waveform, driven by input and output levels, with a Reduce Motion form.
- [ ] T082 Projection. Present the overlay from the composer mic. Remove the inline listening state.
- [ ] T083 Projection. Open the overlay when the download finishes, if the chat and app are in front.
- [ ] T084 Projection. Error lines with their actions: microphone, missing voice, download.
- [ ] T085 Projection. Consent sheet over the overlay, with the spoken notice.
- [ ] T086 Projection. Settings: English voice with previews, language, status, Remove.
- [ ] T087 Projection. Escape closes the overlay on Mac. Wire or delete `MacPushToTalkKeyMonitor`.
- [ ] T088 Projection. VoiceOver labels, state announcements and stable identifiers.
- [ ] T089 Projection. UI tests for the flows in the phase 4 exit.

## Phase 5. Watch

- [ ] T100 Projection. Build the paired-watch pipeline on the new runtime.
- [ ] T101 Projection. Resample local-voice and system-voice speech to 24 kHz before packing.
- [ ] T102 Projection. The watch waveform screen with its states and tap to interrupt.
- [ ] T103 Projection. Notices for an unreachable phone, a missing pack, a closed mesh and a phone app
  that is not in front.
- [ ] T104 Projection. Update `PairedWatchVoiceTests`, `PhoneCompanionRelayTests` and the watch tests.
- [ ] T133 Projection. A test of the refusal: a watch session start is refused with
  `phone_voice_unavailable` while the phone app is in the background or locked, and a running session
  ends when the scene goes inactive, with the runtime's GPU work stopped first.

## Phase 6. Rules and documents

- [ ] T110 Deep. Amend the voice sentence in `AGENTS.md`.
- [ ] T111 Deep. Dated amendments to spec 105 sections 12.1 and 12.5.
- [ ] T112 Projection. Allow `AVSpeechSynthesizer` in one named file in
  `tests/test_apple_no_hosted_voice.py`. Land it with T043.
- [ ] T113 (P) Projection. Rewrite `ONDEVICE-VOICE.md` and the voice lines in `README.md`.
- [ ] T114 (P) Projection. Acknowledgments screen entries and licence files for Rumble and Qwen3-ASR,
  LFM2.5, llama.cpp and its vendored code, Phonon, and FluidAudio with NemoTextProcessing.
- [ ] T115 Projection. Remove `NSSpeechRecognitionUsageDescription`.
- [ ] T116 Deep. Bump the Projection pin in `config/astral-composition.json` and run
  `scripts/verify_composition.py`.
- [ ] T117 Vault. Update the voice synthesis page and `project-astral` at each checkpoint.

## Phase 7. Qualify

- [ ] T120 Projection. Run `swift test --package-path apple-clients/AstralCore` and the iOS, macOS and
  watchOS schemes.
- [ ] T121 Projection. The agent's live Mac conversation, with a timing report.
- [ ] T122 Projection. Build the localtest iPhone app under the `AGENTS.md` rules for a phone that
  holds mesh keys.
- [ ] T123 Owner and agent. The live iPhone pass in spec section 15, including the items phase 0 left
  unverified: echo leakage into Rumble, GPU contention, a cold first launch, background GPU behavior and
  a packet capture.
- [ ] T124 Owner. The verdict on naturalness.
- [ ] T125 Deep. Store the report and recordings under `specs/109-rumble-voice/evidence/`.
