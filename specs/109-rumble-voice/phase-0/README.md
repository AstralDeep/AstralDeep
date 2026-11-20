# Phase 0 results for spec 109

**Date:** 2026-11-20. **Status:** every R item has a written result. The voice pick (T013) is open.
**Companions:** [spec.md](../spec.md), [plan.md](../plan.md), [tasks.md](../tasks.md), [research.md](../research.md).

Seven Sonnet agents did the work in parallel lanes. The orchestrating agent checked the claims marked
"checked" below against the source, the raw results or the built artifact. Everything else is the
agent's report and is an evidence lead, not proof.

Revisions read: AstralProjection `54e7ff1`, AstralKernel `4c43032`, llama.cpp `8af68d48`, FluidAudio
`v0.18.4` (`e0c6103b`). Phone figures are from one iPhone 13 Pro on iOS 27.2.1 in a Release build.
Mac figures are from an Apple M5 on macOS 27.2.1.

Work outside this folder lives in `AstralDeep-wt/voice-bench-109/`: the bench app and recordings
(`phone/`), the Mac runtime (`runtime-mac/`), the acknowledgment test (`ack-eval/`), the noise test
(`noise-eval/`) and licence scratch clones (`licences/`). None of it is in Git.

## Answers

| Id | Answer | File | Checked |
|---|---|---|---|
| R1 | A partial takes 0.25 s on a 2 s segment, 0.45 s at 5 s and 0.80 s at 10 s. A 0.5 s tick falls behind on long utterances. An LFM2.5 draft plus a sentence does not fit between ticks at any length. | [R1](R1-rumble-partials.md) | Read |
| R2 | The short-clip fix is not in upstream llama.cpp. It exists only as a third-party patch with no stated licence. Pin the pilot's revision `8af68d48`. It already holds the background-GPU fix (PR 20306). | [R2](R2-llama-short-clip-fix.md) | Ancestry checked |
| R3 | The kernel partly has the failure signal and the local turn, and has no content-free provider check. A small additive kernel change is needed. | [R3](R3-kernel-offline-routing.md) | Source checked |
| R4 | llama.cpp with `libmtmd` builds for macOS arm64. A host signed with the hardened runtime and only the audio-input entitlement ran both models on Metal. No JIT, unsigned memory or library-validation exception. | [R4](R4-macos-runtime.md) | Signature checked |
| R5 | All three voices load from a local folder with FluidAudio's offline mode on. Every file the app would fetch is open without accepting terms. Phonon and Pocket TTS need FluidAudio `v0.18.0` or later, not 0.17.7. | [R5a](R5a-fluidaudio-local-load.md), [R5b](R5b-candidate-downloads.md) | Read |
| R6 | A watch voice session already needs the phone app in front and unlocked. That stays. No audio background mode is justified. | [R6](R6-watch-relay-phone-state.md) | References checked |
| R7 | Voice processing is declared for watchOS 6 and later. Whether echo cancellation works on a watch needs hardware. C13 stands. | [R7](R7-watchos-voice-processing.md) | Header checked |
| R8 | Keep the 5.5 GB gate. The pilot's 1.85 GB was resident size. Footprint with Rumble, LFM2.5 and a voice loaded peaked at 0.50 to 0.70 GB. | [R8](R8-memory-gate.md) | Read |
| R9 | Rumble, llama.cpp, Phonon 31m and Pocket TTS small pass. FluidAudio bundles two GPL data files for a voice we do not use. Kokoro's English front end is not cleared. Two Pocket voices are non-commercial. | [R9](R9-licences.md) | GPL note checked |
| R10 | LFM2.5 drafts usable acknowledgments in English, Spanish, French and German, and probably Mandarin and Arabic. It fails in Japanese and Korean. The opener rule as written rejects most good drafts. | [R10](R10-lfm25-acknowledgments.md) | Read |
| R11 | Stock Rumble returned words for 39 of 169 non-speech clips. With app-side padding that drops to 4, with the patch to 2. Every real "stop" and "wait" came through. C5 is workable. | [R11](R11-rumble-false-words.md) | Totals checked |
| R12 | Pocket TTS 18m and Phonon 31m match Piper's speed in the same bench and use less memory. Kokoro is slower and not cleared. | [R12](R12-voice-candidates.md) | Clock code read |

## The voice candidates

Bench clock: from the synthesize call to the first non-silent sample. It does not compare with the
pilot's 0.539 s, which timed a whole turn. Piper ran in the same bench as the reference.

| Voice | Median to first audible | Footprint peak | Licence |
|---|---:|---:|---|
| Piper (reference only, not shippable) | 0.210 s | 658 MB | Fails |
| Pocket TTS 18m, voice `alba` | 0.048 s, about 0.21 s at a speech-level threshold | 499 to 538 MB | Pass, CC-BY-4.0, with a voice allow-list |
| Phonon 31m | 0.169 s | 523 to 528 MB | Pass, Apache 2.0 |
| Kokoro-82M | 0.522 s, about 0.21 s with leading silence trimmed | 698 to 699 MB | Not cleared |

Rumble and LFM2.5 were resident during every pass. Resident size at the end was 1.57 to 1.93 GB, well
above the footprint, because the GGUF weights are memory mapped.

Recordings for the owner are in `AstralDeep-wt/voice-bench-109/phone/recordings/`. The two sets to
judge are `pocket18/deess` and `phonon/deess`, ten lines each. Those are what FluidAudio's own
`synthesize` call returns, with its de-esser on, so they are what the app would play. The raw sets
beside them, `kokoro` and `piper` are reference only. All 60 files were transcribed with Rumble on the
Mac and every one carries its line.

## Calls made from these results

The owner asked the agent to decide open questions and report them. Each of these can be overruled.

1. **Pin llama.cpp at `8af68d48` and pad in the app.** Each clip is padded with zeros to
   `16000*k - 160` samples before recognition. A voiced-frame gate (five frames) and a 0.4 s minimum
   sit in front of it. The third-party patch is not carried. Its licence is unknown and its edge over
   padding is within noise.
2. **"Offline" means a failure the kernel already falls back on.** Unreachable, timeout, stream
   failure, 404, 408, 429 and 5xx. A 400, 401, 403 or 422 stays an error. A bad key should surface,
   not be masked by a local answer.
3. **T072 is real.** The kernel gains a structured fallback event, an on-device-only route, a
   body-free provider probe and an `answered_by` field on the turn. The default route is unchanged.
4. **FluidAudio is pinned at `v0.18.4` by commit, with its LuxTTS resource bundle removed.** A build
   step deletes the bundle and a test asserts it is absent. Measured: all three voices still speak
   without it. This applies only if the owner picks a local voice.
5. **The shortlist is Pocket TTS 18m and Phonon 31m.** Kokoro stays out unless the origin of its
   pronunciation data is established and FluidAudio's iOS 27 advisory is lifted.
6. **Pocket TTS ships with a code-enforced voice allow-list.** `cosette` and `jean` are
   non-commercial and never load.
7. **The acknowledgment draft runs in the silence window, not between partials.** Section 8.2 says
   the draft runs "in the gap between two Rumble partials". That gap does not exist. The draft starts
   at the speculation point, when no new audio is arriving.
8. **Partials must not queue.** A tick is skipped while the previous partial is still running.
9. **Model drafts ship in English, Spanish, French and German.** Mandarin and Arabic use generic
   lines until a native speaker reviews the drafts. Japanese and Korean use generic lines only.
10. **The opener rule changes.** Code picks the opening stem from a rotated list per language and
    passes it to the model, and an allow-list of stems rejects answers in disguise. This design was
    not tested in phase 0 and is the first thing phase 3 measures.
11. **The watch keeps "phone app in front".** Section 12 states it, and a test covers the refusal.
12. **The pack needs a new manifest without the Piper archive.** Publishing it to Hugging Face needs
    the owner's word.

## Spec amendments owed

Applied in one pass after the voice pick.

- Section 7.4 and research section 9: FluidAudio version, the GPL bundle, the Pocket voices, Kokoro's
  status, measured figures.
- Section 6: a pack manifest without Piper. Section 7.2: the pinned revision and the padding rule.
- Section 8.2: where the draft runs, the opener rule, the language split.
- Section 8.3: the voiced-frame gate, and "Mm-hm" arriving as "M.M.H.M.".
- Sections 9.3 and 9.4: the kernel change and the failure classes.
- Section 12: the phone-in-front rule.
- Tasks: T072 becomes four named pieces. T023 and T042 name the voice.

## Needs the owner

1. **T013.** Listen to the recordings and pick Pocket TTS, Phonon or the system voice.
2. **The LFM Open License wording.** "Legal Entity" counts affiliates, and one reading requires each
   recipient, such as a large hospital, to be under the threshold itself. Quoted in R9. This needs a
   legal reading.
3. **A native review** of the Mandarin and Arabic drafts, if those languages should get model drafts.

## Not verified

- Astral's own voice leaking back through echo cancellation into Rumble. This is the main risk to
  word-confirmed interruption and needs the phone pass.
- Contention when Rumble, LFM2.5 and the voice share the GPU at the same moment.
- A cold first launch on a fresh install, and the output route latency.
- Background GPU behavior on a device, the App Sandbox and notarization on the Mac.
- A packet capture for the no-network claim. It rests on FluidAudio's offline mode and a closed port.
- The byte total of the lab app's models on the phone after the bench. The app is installed at
  version 1.0 (1) and its `Models` folder lists 416 entries. The bench left `BenchVoices` and
  `BenchResults` in its Documents folder, about 600 MB by the agent's count.
- Any phone with 4 GB of memory.
- The licences of three sources FluidAudio ports code from, and the origin of Kokoro's dictionary.
