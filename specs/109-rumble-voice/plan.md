# Plan for spec 109

**Date:** 2026-11-20. **Status:** draft. Phase 0 is done and its results are in
[phase-0/README.md](phase-0/README.md). The spec was amended to match on 2026-11-20. Nothing is built.
**Companions:** [spec.md](spec.md), [tasks.md](tasks.md), [research.md](research.md).

Read [spec.md](spec.md) first. Section numbers refer to it. Decision ids are the spec's. R ids are the
items of [research.md](research.md) section 10, which phase 0 answered.

## Preconditions

1. The owner has reviewed [spec.md](spec.md), in particular section 16. Not yet met.
2. The English voice is chosen. Met on 2026-11-20: the owner picked Phonon 31m (O13).
3. The kernel change in spec section 9.3 has been reviewed. It must be met before phase 3 starts.

## Shape of the work

Eight phases. Phases 1 and 2 put the new stack under the old interface, so voice keeps working at
every merge. Phase 3 is where the conversation gets its feel. Phases 4 and 5 are the screens.

| Phase | What it delivers | Repositories |
|---|---|---|
| 0 | Answers to R1 to R12, including the English voice. No product code. Done. | All |
| 1 | The `AstralVoiceRuntime` and `AstralVoiceSpeech` packages and the pack store | Projection |
| 2 | Rumble, Phonon, system voices and LFM2.5 under the shipping pipeline. LFM3 removed. | Projection |
| 3 | Natural conversation: end of turn, fillers, interruption, pacing, and the offline case | Projection, Kernel |
| 4 | The overlay and Settings on iPhone and Mac | Projection |
| 5 | The watch | Projection |
| 6 | Rules, documents, guards and acknowledgments | Deep, Projection |
| 7 | Qualification, including the live iPhone pass | All |

## Phase 0. Find out

No product code. Each item ends with a short written result in `phase-0/` beside this plan. All twelve
are answered and [phase-0/README.md](phase-0/README.md) summarizes them. The items below record what was
asked.

- **R1, R2.** Measure Rumble partials on the iPhone 13 Pro with the lab app. Find a llama.cpp revision
  with the short-clip fix, or confirm the pinned one has it.
- **R3.** Read the kernel's turn routing. Write down how the host learns that the provider is
  unreachable and how a turn is served by the on-device model in the same chat. If either is missing,
  propose the smallest change.
- **R4.** Build llama.cpp with `libmtmd` for macOS arm64. Run one recognition and one generation in a
  signed, hardened test host.
- **R5.** Confirm FluidAudio loads a voice model from a local folder with no network request, and that
  each candidate's Core ML files can be downloaded without accepting terms in a browser.
- **R6.** Read the paired-watch relay for what it needs from the phone app's state. Test on hardware if
  the owner's watch is at hand.
- **R7.** Check voice processing on watchOS in Apple's documentation and SDK headers.
- **R8.** Check the memory gate against the pilot's peak figures.
- **R9.** Collect the licence files for LFM2.5, FluidAudio and each voice candidate, including the
  pronunciation data each one ships. The owner settled the LFM2.5 revenue question on 2026-11-20 (O12).
- **R10.** Run LFM2.5 on twenty acknowledgment prompts in each of the eight languages and score them
  against the rules in section 8.2.
- **R11.** Feed Rumble coughs, room noise and clips under one second. Count how often it returns words.
- **R12.** The voice test in spec section 7.4. Measure the three candidates on the iPhone 13 Pro in a
  small test app, record each one speaking the same ten lines, and give the owner the recordings.

**Exit: met on 2026-11-20.** Every R item has a written result. The owner picked Phonon 31m after
listening to the recordings (O13). The kernel change from R3 is specified in spec section 9.3. Phase 0
left some items unverified, which the live pass in phase 7 records (spec section 15).

## Phase 1. Runtime and pack

- A build script for the xcframeworks, pinned at llama.cpp `8af68d486a9473820d24d00af8f33334ca80124d`,
  with iPhone, simulator and Mac slices and `MTMD_VIDEO` off.
- The `AstralVoiceRuntime` package with a Swift surface for recognition and text generation. The
  recognition call pads each clip to `16000*k - 160` samples. Cancellation works for both.
- The `AstralVoiceSpeech` package for Phonon, with FluidAudio pinned at `v0.18.4` by commit `e0c6103b`.
  It loads from a verified local folder with offline mode on and cancels.
- The app's own manifest, two repositories and the partial outcome (section 6). The manifest lists the
  three GGUF files and the Phonon Core ML files and no Piper archive.
- Tests on tiny fixture models that need no download.

**Exit:** both packages' tests pass on Mac and in the simulator. The store installs the real pack on a
Mac from a clean folder and refuses a tampered file and an escaping path. The app still ships
LFM3 voice, untouched.

## Phase 2. Swap the models

- Implement the four jobs in section 7.1 behind the pipeline's seam. Link both packages into the app
  project.
- Add the build step that removes FluidAudio's `LuxTtsG2pResources` bundle, and its test.
- The language triple: recognition language, reply language and voice (section 5.4).
- System voices render to buffers and play through the engine (C7).
- LFM2.5 becomes the kernel's on-device model in place of LFM3.
- Remove `AstralVoiceModel`, MLX and the LFM3 manifest. Delete installed LFM3 weights on launch (C11).

**Exit:** the existing voice test suites pass against the new runtime. An unsigned Mac build and an
iPhone simulator build succeed. The built app holds no `LuxTtsG2pResources` bundle. A spoken English turn
works end to end on a Mac. No source file imports
MLX.

**Status, 2026-11-20.** Code complete on the local Projection branch `feat/109-voice-swap`. The app runs
on Rumble, LFM2.5, Phonon and system voices, and LFM3, MLX and the old package are gone. The voice test
classes pass on macOS, an arm64 Release Mac build and an iPhone simulator build succeed, the built app
holds no `LuxTtsG2pResources` bundle and no source file imports MLX. Two exit items are open: the live
spoken English turn on a Mac, which needs the owner at the microphone, and the universal Release link,
which needs the x86_64 kernel library that only CI builds.

## Phase 3. Natural conversation

The order matters. Each step is felt on a device before the next starts. The kernel change in spec
section 9.3 is reviewed before this phase starts and lands before step 7.

1. Measure the opener design on a device: the rotated stem list and the allow-list (section 8.2). Phase 0
   did not test it, so it comes first. Its result decides how step 4 is built.
2. End of turn with per-language word lists and the skipped partial tick (section 8.1).
3. The three-stage interruption with the voiced-frame gate and the 0.4 s minimum (section 8.3).
4. Model-drafted fillers with the checker, the stem allow-list and the quick-answer window (section 8.2).
5. Sentence pacing and the voice preference sent to the provider (section 8.4).
6. Speech during a running job becomes the next provider turn (section 9.2).
7. The offline case: both announcements, local turns in the chat, checking and return (sections 5.3,
   9.4). This is an edge case and comes last.

**Exit:** code tests for each step pass. The agent holds a ten-turn English conversation on the Mac and
reports timings against section 8.5.

## Phase 4. Overlay and Settings

- The overlay view, its states, the waveform and the controls (section 10).
- The first-tap flow from sheet to overlay (section 5.1).
- Settings: English voice with previews, language, status and Remove (section 11).
- Remove the composer's inline listening state.
- Accessibility: VoiceOver, Reduce Motion, identifiers.

**Exit:** UI tests cover first tap, download, overlay open, each state, mute, language change and
close on iPhone and Mac. Screens are checked on a small and a large iPhone and at two Mac window sizes.

## Phase 5. Watch

- Point the phone-side relay at the new runtime and resample speech to 24 kHz.
- The watch waveform screen and its states.
- Notices for an unreachable phone, a missing pack, a closed mesh and a phone app that is not in front.
- A test that the phone refuses a watch session while its app is in the background or locked.

**Exit:** `PairedWatchVoiceTests`, `PhoneCompanionRelayTests`, `CompanionVoiceTests` and the watch
target's tests pass. The watch source guard still passes.

## Phase 6. Rules and documents

Every row of the table in section 14. The drift guard change lands in the same Projection change as the
first use of `AVSpeechSynthesizer`, so the guard is never red on main.

**Exit:** Deep's `ruff check .` and the guard tests pass. The vault pages are updated and the Deep
composition pin matches the merged Projection commit.

## Phase 7. Qualify

- The Apple gates in `AGENTS.md`: `swift test` for `AstralCore`, the affected `xcodebuild` schemes for
  iOS, macOS and watchOS.
- The live Mac conversation by the agent.
- The live iPhone pass (section 15). The agent asks the owner to connect the phone, installs a
  localtest build and runs the script. It also records what phase 0 left unverified: Astral's own voice
  leaking into Rumble, GPU contention, a cold first launch, background GPU behavior and a packet capture.
- The owner talks to it and gives the verdict.

**Exit:** all code gates pass. The iPhone report meets section 8.5 or records each miss. The owner has
said the conversation feels natural, or has listed what to change, in which case the work returns to
phase 3.

## What this plan does not do

It does not push, merge or release. Each of those needs the owner's word, per `AGENTS.md`.

iOS 2.1.2 was in App Review on 2026-11-20. This work targets a later version.
