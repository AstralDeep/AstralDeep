# Research for spec 109

**Date:** 2026-11-20. Evidence behind [spec.md](spec.md). Nothing here is a decision.

Sources read:

- AstralDeep `origin/main` at `256301ef`.
- AstralProjection `origin/main` at `54e7ff1`. Paths below are relative to `apple-clients/`.
- AstralKernel `origin/main` at `4c43032`.
- The lab at `~/Desktop/VoiceTesting` and the pilot at `~/Desktop/AstralVoicePilot`, both dated
  2026-11-19. Neither is a Git checkout of an Astral repository.
- The vault page `wiki/synthesis-astral-conversational-voice-interface.md` in `kos-wiki-v2`.
- Three Hugging Face model cards and two Apple documentation pages, fetched 2026-11-20.

A line marked **measured** comes from a pilot or lab report on a physical iPhone 13 Pro. A line marked
**unverified** was not checked and has a matching item in section 10.

---

## 1. Voice in the shipping Apple apps today

One model does everything. LFM3-Audio-2B at 3 bits runs on MLX Swift 0.33.0 and handles recognition,
speech and short acknowledgements.

| Fact | Where |
|---|---|
| Model package, `actor LFM3AudioEngine` | `AstralVoiceModel/Package.swift`, `Sources/AstralVoiceModel/LFM3AudioEngine.swift:40` |
| Manifest: 13 files, 1,419,703,438 bytes, repo `Kentucky-Open-Science/LFM3-Audio-2B-MLX-3bit` | `AstralApp/AstralApp/Resources/voice-model-manifest.json` |
| Manifest parsing, SHA-256 and safe-path checks | `OnDevice/VoiceModelManifest.swift:80, 94, 109-125` |
| Download with resume, staging, atomic swap, install marker | `OnDevice/VoiceModelStore.swift:397-420, 517-605` |
| Cellular confirmation and a 200 MB free-space margin | `OnDevice/VoiceModelStore.swift:198, 582-583` |
| Storage under `Application Support/AstralDeep/VoiceModel`, excluded from backup | `OnDevice/VoiceModelStore.swift:620-631` |
| Device gate: Apple silicon, no simulator, phones need 5.5 GB of memory | `OnDevice/VoiceDeviceSupport.swift:56-65` |
| The inference seam | `OnDevice/MLXVoiceRuntime.swift:8-12`, `protocol VoiceModelInference` |

The seam is three functions: `transcribe`, `synthesize` and `acknowledgement`. Everything above it is
model-agnostic.

LFM3 has a second job. It is the kernel's on-device text model on devices without Apple's Foundation
Models (`OnDevice/OnDeviceFallbackModel.swift:7-66`, injected at `Mesh/MeshAppMode.swift:104`).
Removing LFM3 removes that fallback unless something replaces it.

### 1.1 The microphone button

One shared composer serves iOS and macOS (`Mesh/MeshChatViews.swift:206-236`). The accessibility id is
`mesh.composer.mic`.

A tap calls `onDeviceVoice.toggle()`. With no model installed it opens `VoiceModelSetupView` as a
sheet, which shows the size before asking for anything (`Mesh/MeshRootView.swift:36-39`,
`VoiceModelSetupView.swift:69-119`).

With a model installed, voice starts inline. There is no voice screen. The mic becomes a "Stop Voice"
capsule and a "Listening" label appears under the composer (`Mesh/MeshChatViews.swift:260-263`).

### 1.2 The turn path

| File | Job |
|---|---|
| `OnDevice/OnDeviceVoiceController.swift` | Readiness, permissions, lifecycle. Builds the pipeline. |
| `OnDevice/VoicePipeline.swift` | Runs one session. Waits up to 8 s for a final transcript. |
| `OnDevice/VoiceDuplexEngine.swift` | One `AVAudioEngine` with voice processing (echo cancellation). Playback format is 24 kHz mono. |
| `OnDevice/VoiceActivityDetector.swift` | Energy detector with an adaptive noise floor and a 0.3 s hangover. |
| `OnDevice/VoiceEndOfTurnDetector.swift` | Speculates after 0.25 s of silence. Ends the turn after 0.6 s, or 1.0 s after a continuation word. English word list only. |
| `OnDevice/BargeInController.swift` | Ducks playback to 0.15, confirms an interruption after 0.25 s, ignores backchannels such as "mm" and "okay". |
| `OnDevice/VoiceFastPath.swift` | Speaks one of six fixed phrases while the answer is generated. The model only picks among them. |
| `OnDevice/VoiceSentenceSplitter.swift` | Splits streamed text into sentences and strips markup. |
| `OnDevice/MLXVoiceRuntime.swift` | `MLXVoiceTranscriber`: 16 kHz, 30 s segments, a partial every 0.5 s by re-running recognition on the growing segment. |
| `Mesh/MeshVoiceLink.swift` | `VoiceKernelLink` over `MeshRuntime`, scoped to one chat and session. |

A recognized utterance becomes an `OwnerVoiceTurn`. It carries owner integrity only when the audio came
from this device's microphone or a paired watch microphone in a session the owner started
(`OnDevice/OwnerVoiceTurn.swift:31-38`). `approvalEligible` is always false.

`MeshRuntime.submitVoice` refuses anything else and then sends the text as an ordinary kernel message
(`Mesh/MeshRuntime.swift:1946-1958, 1480`). Assistant deltas come back to the pipeline only for the
chat being spoken (`Mesh/MeshRuntime.swift:2257-2271`).

Only speech that played is recorded, through the kernel's `chat.spoken` event
(`Mesh/MeshRuntime.swift:1965`, spec 105 `contracts/event.md:162`).

A pending consent sets `setRiskyActionPending`. Nothing in the voice path can approve it.

### 1.3 Speech output and languages

Speech comes from the LFM3 codec at 24 kHz. Language is hard-coded to English and the voice to
"female" (`LFM3AudioEngine.swift:77-95`). There is no language or voice setting anywhere.

No source file constructs `AVSpeechSynthesizer`, `SFSpeechRecognizer` or `SpeechAnalyzer`.
`Info.plist:74-75` still declares `NSSpeechRecognitionUsageDescription`, which nothing uses.

### 1.4 The watch

The watch target links `AstralCore` only. It has no kernel, no model and no network transport. A
source scan in `WatchCompanionTests` keeps it that way.

Voice on the watch is a relay over WatchConnectivity that already works end to end in code:

- The watch captures 16 kHz mono PCM and sends it in packets of at most 3,200 bytes
  (`AstralCore/Sources/AstralCore/CompanionVoice.swift:73`).
- The phone runs the pipeline with source `.pairedWatchMicrophone`
  (`Voice/PairedWatchVoiceRelay.swift:224-260`).
- The phone sends speech back as 24 kHz mono 16-bit PCM in packets of at most 4,800 bytes
  (`Voice/PairedWatchVoiceAudio.swift:89-131`).
- The watch pauses capture while it plays speech. It is half duplex.
- Loss of reachability stops the session (`Voice/PairedWatchVoiceRelay.swift:46`).

### 1.5 macOS

iOS and macOS are one multiplatform target. The Mac App Store and direct-download configurations both
carry `com.apple.security.device.audio-input` under the hardened runtime, with no library-validation
exception.

`MacPushToTalkKeyMonitor` exists for the Right Option key. Only a test uses it.

### 1.6 Tests and guards

- App tests: `OnDeviceVoicePipelineTests`, `MeshVoiceLinkTests`, `OnDeviceVoiceControllerTests`,
  `NoHostedVoiceTests`, `VoiceModelStoreTests`, `OnDeviceBargeInTests`, `OnDeviceFastPathTests`,
  `OnDeviceVoiceLogicTests`, `PairedWatchVoiceTests`, `PhoneCompanionRelayTests`.
- Package tests: `AstralVoiceModel` (tiny-model, parity), `AstralCore` `CompanionVoiceTests`.
- The drift guard `AstralProjection/tests/test_apple_no_hosted_voice.py` forbids `api/voice`, `LiveKit`
  and any constructor call to `AVSpeechSynthesizer`, `SFSpeechRecognizer`, `SpeechAnalyzer` or
  `SpeechTranscriber`.
- Two older guards cover the retired hosted path: `tests/test_voice_audio_focus_parity.py` and
  `tests/test_mobile_voice_interruption_065.py`. Both read `Voice/VoiceSessionController.swift` and its
  tests. This spec does not touch those files.
- No branch on the Projection remote changes the voice, watch or model files beyond `main`, and Deep's
  `converge/remove-web-chat` branch changes no voice file. Checked 2026-11-20.

### 1.7 Deployment targets and native code

iOS 17.0, macOS 14.0, watchOS 10.0. The only vendored binary is the kernel xcframework, built by a
script and ignored by Git. No llama.cpp, GGUF, ONNX Runtime, Sherpa or Piper exists under
`apple-clients`.

---

## 2. Rules that this spec touches

| Rule | Where |
|---|---|
| "No hosted-voice or system-speech fallback may silently substitute for an unavailable local model." | `AGENTS.md`, Mental model |
| The Apple apps require the verified LFM3 download and use neither hosted speech nor a system-speech fallback. Owner decision 2026-11-12. | Spec 105 section 12.1 |
| Code tests qualify voice. Live microphone tests are optional. Owner instruction 2026-11-14. | Spec 105 section 12.5, `clarification-voice-qualification.md` |
| Owner integrity only from the device's own microphone. No approval by voice alone. | Spec 105 section 12.4 |
| The kernel is the only turn engine. | Owner direction 2026-11-19 |
| No wall-clock performance bounds on shared hosted runners. | `AGENTS.md`, engineering rules |

---

## 3. The Rumble pack

### 3.1 What Rumble is

Rumble 2 Flash is a speech recognition model. Its card describes transcription only, with options for
language, digits, fillers and verbatim output. It produces text. It does not converse and it does not
speak.

- Source card: `Reson8/Rumble-2-Flash`, Apache 2.0, a fine-tune of `Qwen/Qwen3-ASR-0.6B`.
- The GGUF build comes from `AtomicChat/Rumble-2-Flash-GGUF` and runs in llama.cpp with `libmtmd`.
- Audio is 16 kHz mono. Long audio is encoded in 8-second windows.
- The card claims 45 languages and does not list them. The pilot exercised eight.
- The GGUF card reports that stock llama.cpp can add a few unsaid words to short clips because it
  treats zero padding as sound. A fix was pending upstream. **Unverified** whether the pilot's pinned
  llama.cpp revision carries it.

### 3.2 What the pack contains

Repository `Kentucky-Open-Science/Rumble-2-Flash-Astral`. The v1.1 manifest uses format
`astral.rumble-voice-pack/v2`. Release commit `573fa2b486f6e5f134f35350b3256fe8977ee826`. Weights pinned
at `b1b5a9dea24885fa348a7fa9f8ce23efefb08b2c`.

| Part | File | Bytes | Job |
|---|---|---:|---|
| Recognition | `rumble/Rumble-2-Flash-AD-Q5_K_M.gguf` | 283,424,640 | Speech to text |
| Audio encoder | `rumble/mmproj-Rumble-2-Flash-Q8_0.gguf` | 214,393,024 | Feeds audio to Rumble |
| Local chat | `chat-1.2b/LFM2.5-1.2B-Instruct-Q4_K_M.gguf` | 730,895,168 | Local conversation |
| English voice | `vits-piper-en_US-lessac-medium.tar.bz2` | 67,230,653 | Piper Lessac medium, 22,050 Hz mono |

The three GGUF files come from Hugging Face. The Piper archive comes from the Sherpa-ONNX release on
GitHub, listed under `archives` in the manifest with its own SHA-256 and a digest for each of its 362
files.

A clean install downloads 1,295,943,485 bytes and occupies 1,309,859,838 bytes.

### 3.3 Runtimes in the pilot

- llama.cpp at `8af68d486a9473820d24d00af8f33334ca80124d`, built with `libmtmd`, as a static library.
  Metal on device, CPU in the simulator (`VoiceTesting/Scripts/build_runtime.sh`).
- Sherpa-ONNX with ONNX Runtime 1.17.1 as xcframeworks.
- Slices exist for iPhone and the iPhone simulator only. There is no macOS slice.
- The Sherpa library references `espeak_Initialize` and related symbols. It links eSpeak NG for
  pronunciation. eSpeak NG is licensed GPL-3.0.

---

## 4. How the pilot behaves

The pilot is `Sources/Pilot.swift`, about 800 lines, with its own loop. It does not use the shipping
`VoicePipeline`.

| Behavior | Where | Effect on feel |
|---|---|---|
| A turn ends on a fixed 0.3 s of silence | `Integration/VoiceActivityDetector.swift:12` | Pauses mid-thought end the turn |
| Any detected onset holds playback at once | `Sources/Pilot.swift:353` | Room noise stalls the answer |
| Recognition runs once, after the turn ends | `Sources/Pilot.swift:388` | No early signal to prepare a reply |
| Acknowledgments come from regular expressions over the question | `Sources/RequestAcknowledgment.swift` | Every turn opens with the same sentence shapes |
| While a job runs, speech goes to the local model | `INTEGRATION.md` | A side conversation the kernel never sees |
| The local transcript lives in the app container | `INTEGRATION.md` | Other devices do not get it |

The shipping pipeline already handles the first three differently. It has a two-stage end of turn with
continuation words, a confirmation delay before an interruption counts, a backchannel list and partial
transcripts.

### 4.1 Measurements

All **measured** on one iPhone 13 Pro running iOS 27.2.1. The clock starts when the detector declares
the turn over. It stops at the first non-silent output sample plus the route's reported latency.

| Measurement | Result |
|---|---|
| First word with Piper, ten questions | Median 0.539 s |
| First word with the iOS voice Samantha, same questions | Median 1.094 s |
| First word with Piper, seven live questions | Median 0.524 s, range 0.438 to 0.643 s |
| Start of the full provider answer | Median 3.450 s with Piper |
| Microphone ready after opening the overlay | 3.75 to 3.86 s |
| Peak app memory at the tenth turn | 1.50 GB with the iOS voice, 1.85 GB with Piper |
| Exact transcripts after normalization | 9 of 10 in each run |
| Six more languages, 96 replies | All under 3 s. Median 1.85 s |

Rumble misheard "sides" as "sites", "penguins" as "pendants" and one book title. The provider still
answered correctly each time.

---

## 5. Apple's rules for voice on the watch

Apple offers three routes.

1. **System dictation.** A text field that the system fills by voice. It is a typing aid. It cannot
   carry a spoken conversation, and the watch app already offers it.
2. **Direct networking.** TN3135 allows TCP, UDP and WebSocket connections on watchOS only for an app
   that is actively streaming audio, or one running a CallKit call. Every other app sees `ENETDOWN`.
   The simulator does not enforce this.
3. **WatchConnectivity.** `sendMessageData` delivers small payloads at once while the counterpart is
   reachable. Apple describes it as meant for immediate communication between the two apps.

Route 2 would give the watch its own network identity. Spec 106 removed that, and a source guard
forbids it. Route 3 is what the app uses today.

**Unverified:** whether `AVAudioInputNode.setVoiceProcessingEnabled` works on watchOS. Without echo
cancellation the watch cannot listen while it speaks.

---

## 6. The kernel's on-device model

The kernel takes an optional `OnDeviceModel` from the host (`crates/astral-kernel/src/turns.rs:90`,
`crates/astral-kernel-ffi/src/turns.rs:72`). PHI rules treat the device as a named destination
(`crates/astral-kernel/src/turns_runtime.rs:182-189`).

**Unverified:** the exact condition under which the kernel routes a turn to the on-device model when a
provider fails, and how the host is told that the provider is unreachable.

---

## 7. Licences

| Component | Licence | Note |
|---|---|---|
| Rumble 2 Flash | Apache 2.0 | |
| LFM2.5 1.2B Instruct | LFM Open License v1.0 | Commercial use is licensed only while the legal entity and its affiliates have annual revenue under $10 million. Qualified non-profits are exempt for non-commercial and research use. No field-of-use restriction. The shipping LFM3 manifest names the same licence. |
| llama.cpp | MIT | |
| Sherpa-ONNX | Apache 2.0 | Links eSpeak NG |
| eSpeak NG | GPL-3.0 | Code is linked into the app. Data is downloaded. |
| ONNX Runtime | MIT | |
| Piper Lessac medium voice | See its model card | The Piper project names Lessac and CSTR as the contact for redistribution. The app downloads it and does not bundle it. |

The Sherpa-ONNX, eSpeak NG and Piper rows describe the pilot. Spec 109 ships none of them.

---

## 8. Sample rates

| Source | Rate |
|---|---|
| Rumble input | 16 kHz mono |
| Piper output | 22,050 Hz mono |
| System voice output | Whatever `AVSpeechSynthesizer.write` returns for that voice |
| Engine playback format today | 24 kHz mono |
| Watch speech packets | 24 kHz mono, 16-bit |

The engine already converts model speech to the capture client's format before output. Piper and
system-voice buffers need one resampling step into that path.

---

## 9. English voice candidates

The owner ruled on 2026-11-20 that Piper was only part of the pilot. Model cards and project pages were
read the same day. Every figure below is the publisher's own. None was measured for this spec.

### 9.1 Shortlist

All three run through FluidAudio 0.17.7, a Swift package under Apache 2.0. Its documentation states
"no GPL dependencies" for these paths and lists none in its third-party licence folder.

| Model | Weights | Text to sound | Published speed | Notes |
|---|---|---|---|---|
| Kokoro-82M (`hexgrad/Kokoro-82M`, Core ML build `FluidInference/kokoro-82m-coreml`) | Apache 2.0 | Its own Core ML pronunciation model plus a lexicon. No eSpeak at run time. | 3 to 11 times real time on Apple silicon. About 50 ms median on an M5 Pro for the newest build. | 24 kHz. Does not stream. One Mac benchmark peaked at 1.503 GB of process memory. The newest build needs iOS 18 or macOS 15, and its iPhone inference is described as unvalidated. Also speaks Spanish, French, Mandarin and Japanese. |
| Phonon 31m (Gradium) | Apache 2.0 | Not read | 19 to 25 times real time on an M5 Pro. About 20 ms to first audio. | Streams. English only. About 171 MB. Needs iOS 18 or macOS 15. |
| Pocket TTS small, 18M, 7M and 2M (Kyutai, October 2026) | CC-BY-4.0 | Raw text | 28 to 47 times real time on an M5 Pro. | Streams in 80 ms frames. 34 to 145 MB. Needs iOS 18 or macOS 15. Kyutai's own repository is gated behind accepting terms. The terms forbid voice cloning without consent. |

The Spanish and French lexicons in FluidAudio's Kokoro path were built from eSpeak NG output. English
does not use them. Their licence file was not opened.

### 9.2 Rejected

| Model | Why |
|---|---|
| Piper Lessac medium | Sherpa-ONNX links eSpeak NG, GPL-3.0. The Piper project names Lessac and CSTR as the contact for redistribution rights. |
| Supertonic 3 | OpenRAIL-M. Attachment A forbids use "to provide medical advice and medical results interpretation". The repository was archived on 2026-09-09 with "Development and support have ended". No Mandarin. |
| KittenTTS 2 mini, nano, pico | Apache 2.0, with their own pronunciation files. No iPhone runtime. The Sherpa-ONNX path for the older model passes eSpeak NG data. The 1.7B model has a separate licence with a $1 million revenue cap. |
| Matcha | The English model passes eSpeak NG data in Sherpa-ONNX. |
| Pocket TTS 100M | About 1.8 times real time on a Mac in FluidAudio's figures. 549 to 766 MB. |

### 9.3 The bar

Piper's **measured** median to first word on the iPhone 13 Pro was 0.539 s. The system voice's was
1.094 s. A replacement has to land near the first figure to be worth shipping in place of the second.

---

## 10. Not verified

Phase 0 of [plan.md](plan.md) answers each of these.

| Id | Question |
|---|---|
| R1 | Does re-running Rumble on a growing segment every 0.5 s keep up on an iPhone 13 Pro, with an LFM2.5 draft and a Piper sentence fitted between partials? Speculation, fillers and interruption all depend on it. |
| R2 | Does llama.cpp at the pinned revision carry the short-clip padding fix? If it does not, which revision does? |
| R3 | How does the host learn that the provider is unreachable, and how is a turn served by the on-device model in the same chat while it is? |
| R4 | Does llama.cpp with `libmtmd` build and pass tests as a macOS arm64 slice under the hardened runtime? |
| R5 | Does FluidAudio load a voice from a local folder with no network request, and can each candidate's files be downloaded without accepting terms? |
| R6 | Does the paired-watch relay keep working when the phone app is in the background, given that the models use the GPU? |
| R7 | Does voice processing work on watchOS? |
| R8 | Is the 5.5 GB phone memory gate still right at a 1.85 GB peak? |
| R9 | Do the licence files of each voice candidate, its pronunciation data and FluidAudio confirm what their cards say? Does LFM2.5's revenue threshold fit the entity that distributes Astral? |
| R10 | How good is LFM2.5 1.2B at short acknowledgments in the seven languages other than English? |
| R11 | How often does Rumble return words for a cough, room noise or a clip under one second? Word-confirmed interruption depends on the answer. |
| R12 | How fast is each voice candidate to first audio on an iPhone 13 Pro with Rumble and LFM2.5 loaded, and how much memory does it add? |
