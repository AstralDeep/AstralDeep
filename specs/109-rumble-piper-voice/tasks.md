# Tasks for spec 109

**Date:** 2026-11-20. **Status:** draft. No task has started.
**Companions:** [spec.md](spec.md), [plan.md](plan.md), [research.md](research.md).

Each task names its repository. Projection paths are relative to `apple-clients/`. A task marked (P)
can run beside the others in its phase. Every code task includes its tests.

## Phase 0. Find out

- [ ] T001 (P) Projection. Measure Rumble partial transcription every 0.5 s on the iPhone 13 Pro with
  the lab app. Record time per partial for 2, 5 and 10 second segments. R1.
- [ ] T002 (P) Projection. Check the pinned llama.cpp revision for the short-clip padding fix. Name the
  revision to pin. R2.
- [ ] T003 Kernel. Document when a turn routes to the on-device model and whether a local exchange can
  be logged during a provider turn. Propose a change if one is needed. R3.
- [ ] T004 (P) Projection. Build llama.cpp with `libmtmd`, Sherpa-ONNX and ONNX Runtime for macOS arm64
  and run both models in a hardened, signed host. R4.
- [ ] T005 (P) Projection. Prototype the `tar.bz2` unpacker with path checks against the real Piper
  archive. R5.
- [ ] T006 (P) Projection. Establish what the paired-watch relay needs from the phone app's state. R6.
- [ ] T007 (P) Projection. Check voice processing on watchOS. R7.
- [ ] T008 (P) Projection. Review the phone memory gate against measured peaks. R8.
- [ ] T009 Deep. Collect licence texts and put open questions 1 and 3 to the owner. R9.
- [ ] T010 (P) Projection. Score LFM2.5 acknowledgments in eight languages against section 8.2. R10.
- [ ] T011 Deep. Write the phase 0 results into `specs/109-rumble-piper-voice/phase-0/`.

## Phase 1. Runtime and pack

- [ ] T020 Projection. Add `scripts/build-voice-runtime.sh`, pinned by revision, producing
  xcframeworks for iPhone, simulator and Mac. Ignore the outputs in Git.
- [ ] T021 Projection. Create the `AstralVoiceRuntime` package with binary targets and a C shim.
- [ ] T022 Projection. Recognition: load Rumble and its encoder, transcribe 16 kHz audio with a
  language, stream tokens, cancel.
- [ ] T023 (P) Projection. Piper: load the voice, synthesize one sentence to 22,050 Hz buffers, cancel.
- [ ] T024 (P) Projection. Text generation: load LFM2.5, generate with a token limit, stream, cancel.
- [ ] T025 Projection. One scheduler so recognition and generation never use the GPU together, with
  recognition first.
- [ ] T026 Projection. Fixture models and audio small enough to commit, with tests that run in the
  simulator on CPU.
- [ ] T027 Projection. Parse manifest format `astral.rumble-voice-pack/v2`, including `archives`.
- [ ] T028 Projection. Extend `VoiceModelStore` to two hosts and to archive download, digest check,
  safe unpack and per-file verification.
- [ ] T029 Projection. The partial outcome: GGUF files installed, Piper pending, with later retry.
- [ ] T030 Projection. Bundle the v1.1 manifest. Keep the mirror setting and its HTTPS rule.

## Phase 2. Swap the models

- [ ] T040 Projection. Split `VoiceModelInference` into recognition, speech and local text.
- [ ] T041 Projection. `RumbleTranscriber` implementing `VoiceStreamingTranscriber` with partials.
- [ ] T042 (P) Projection. `PiperSpeech` feeding the duplex engine through one resampler.
- [ ] T043 (P) Projection. `SystemVoiceSpeech` using `AVSpeechSynthesizer.write` only. Pick the default
  installed voice for the locale. Exclude personal and novelty voices.
- [ ] T044 Projection. `ConversationLanguage` with the eight languages and their locales. Save the
  choice per device.
- [ ] T045 Projection. The automatic switch from Piper to the system voice, with its notice.
- [ ] T046 Projection. `LFM25OnDeviceModel` replacing `VerifiedLFM3OnDeviceModel` at
  `Mesh/MeshAppMode.swift`.
- [ ] T047 Projection. Load on overlay open, warm the voice in parallel, unload after 60 s or on
  memory pressure.
- [ ] T048 Projection. Delete `AstralVoiceModel`, the MLX dependency and the LFM3 manifest.
- [ ] T049 Projection. Delete installed LFM3 weights on first launch.
- [ ] T050 Projection. Port `MLXVoiceRuntimeTests`, `OnDeviceVoicePipelineTests`,
  `OnDeviceVoiceControllerTests` and `VoiceModelStoreTests` to the new runtime.

## Phase 3. Natural conversation

- [ ] T060 Projection. Continuation word lists for eight languages in `VoiceEndOfTurnDetector`, plus
  the mid-number and mid-list extension.
- [ ] T061 Projection. Speaker-level onset threshold in the activity detector.
- [ ] T062 Projection. Word-confirmed interruption in `BargeInController`: duck, transcribe the
  overlap, then continue or stop.
- [ ] T063 (P) Projection. Backchannel and stop-word lists for eight languages.
- [ ] T064 Projection. Resume from the interrupted sentence after noise or a backchannel.
- [ ] T065 Projection. Replace the six fixed phrases in `VoiceFastPath` with model drafts.
- [ ] T066 Projection. The acknowledgment checker with every rule in section 8.2 and the opener
  history.
- [ ] T067 Projection. The quick-answer window and the small-talk rule.
- [ ] T068 (P) Projection. Generic fallback lines for eight languages, rotated.
- [ ] T069 Projection. Progress lines from kernel events, at most one every 15 seconds.
- [ ] T070 Projection. Send the short-answer and reply-language preference with voice turns.
- [ ] T071 Projection. Speak a component's `spoken` form and strip markup from text.
- [ ] T072 Kernel. The change from T003, if one is needed.
- [ ] T073 Projection. Local talk while a job runs, logged through the kernel.
- [ ] T074 Projection. Provider failure detection, the offline state, the recovery check, and a test
  that no request carries the person's words while offline.
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
- [ ] T101 Projection. Resample Piper and system-voice speech to 24 kHz before packing.
- [ ] T102 Projection. The watch waveform screen with its states and tap to interrupt.
- [ ] T103 Projection. Notices for an unreachable phone, a missing pack and a closed mesh.
- [ ] T104 Projection. Update `PairedWatchVoiceTests`, `PhoneCompanionRelayTests` and the watch tests.

## Phase 6. Rules and documents

- [ ] T110 Deep. Amend the voice sentence in `AGENTS.md`.
- [ ] T111 Deep. Dated amendments to spec 105 sections 12.1 and 12.5.
- [ ] T112 Projection. Allow `AVSpeechSynthesizer` in one named file in
  `tests/test_apple_no_hosted_voice.py`. Land it with T043.
- [ ] T113 (P) Projection. Rewrite `ONDEVICE-VOICE.md` and the voice lines in `README.md`.
- [ ] T114 (P) Projection. Acknowledgments screen entries and licence files.
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
- [ ] T123 Owner and agent. The live iPhone pass in spec section 15.
- [ ] T124 Owner. The verdict on naturalness, and the answer to open question 4.
- [ ] T125 Deep. Store the report and recordings under `specs/109-rumble-piper-voice/evidence/`.
