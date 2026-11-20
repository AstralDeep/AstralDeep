# Spec 109: Rumble voice

**Date:** 2026-11-20. **Status:** draft for owner review. Nothing here is built.
**Amended:** 2026-11-20, after phase 0. Phase 0 measured what this spec had assumed and the owner picked
the English voice. The results are in [phase-0/README.md](phase-0/README.md).
**Companions:** [plan.md](plan.md) (phases and exit criteria), [tasks.md](tasks.md),
[research.md](research.md) (evidence, with file and source references).
**Builds on:** [spec 105](../105-decentralized-personal-mesh/spec.md) section 12 and
[spec 106](../106-sign-in-free-clients/spec.md).

**Scope:** AstralProjection (the Apple clients), AstralDeep (rules, spec 105 amendments, pins) and
AstralKernel (one additive change, section 9.3). iPhone, Mac and Apple Watch.

**Out of scope:** the web shell, Android, Windows, and any hosted speech service.

---

## 1. How to read this spec

- A sentence marked **today** describes code that exists now. [research.md](research.md) has the file
  references.
- A sentence marked **proposed** describes this design. Nothing proposed exists.
- O is the owner's decision, taken on 2026-11-20 unless a date says otherwise. C is this design's call,
  with the alternative that lost. The owner has not reviewed the calls yet. Section 16 lists the ones
  most worth a look.

| Word | Meaning |
|---|---|
| Rumble | Rumble 2 Flash, a speech recognition model. It turns speech into text and does nothing else. |
| The pack | The download that holds Rumble, its audio encoder, LFM2.5 and the Phonon voice. |
| LFM2.5 | LiquidAI LFM2.5 1.2B Instruct, the small local chat model inside the pack. |
| Local voice | Phonon 31m, an English speech model that runs on the device. The owner chose it on 2026-11-20 after phase 0 (O13). The pilot used Piper here. This spec does not. |
| System voice | A voice installed on the device, reached through `AVSpeechSynthesizer`. |
| Provider | The model the owner connected for chat, reached through the kernel. |
| The pilot | The test app at `~/Desktop/AstralVoicePilot`. The lab is `~/Desktop/VoiceTesting`. |
| Overlay | The voice screen that covers the chat during a spoken conversation. |

---

## 2. Summary

**Today** the Apple apps use one model, LFM3-Audio-2B, for hearing, speaking and short
acknowledgements. Voice runs inline in the composer. It speaks English only, in one voice.

**Proposed:** replace that model with the stack the pilot proved on an iPhone 13 Pro, and give voice
its own screen.

1. **Rumble hears.** It transcribes in eight languages.
2. **English is spoken by Phonon 31m,** a local voice model under Apache 2.0 that phase 0 measured at
   least as fast as the pilot's Piper. On iOS 17 and macOS 14, which Phonon does not support, English
   uses the default system voice. The seven other languages use a system voice either way.
3. **The provider answers, through the kernel,** exactly as a typed message would.
4. **LFM2.5 covers for the provider when it goes offline.** Astral says the provider is offline, keeps
   talking with local resources only, and says so again when the provider is back. While the provider
   is online, LFM2.5 only drafts the short spoken acknowledgment (C3).
5. **A waveform overlay** covers the chat while the person talks. It shows no transcript.
6. **The watch relays audio to the phone,** as it does today.
7. **LFM3 and MLX leave the apps.**

The pilot was close, and it felt clunky. The owner named two causes: filler lines that sounded canned,
and interruptions that went wrong. Section 8 is about fixing those. Natural conversation is the main
goal of this spec, ahead of everything else in it.

---

## 3. Goals, non-goals, anti-goals

**Goals**

- Talking to Astral feels like talking to a person. Replies start fast, fillers vary and fit the
  question, and cutting in works the way it does between people.
- The mic icon in a chat starts the whole thing. With no pack installed, the tap leads to the download
  and then straight into the conversation.
- The same conversation works on iPhone and Mac, and on the watch through the phone.
- Eight languages: English, Arabic, Chinese, French, German, Japanese, Korean and Spanish.
- When the provider is down, the person can keep talking. The app says so and recovers by itself.
- Every spoken exchange lands in the kernel log, under the same gates as typed input.

**Non-goals**

- The pilot's world clock tour, its clock tool, its Factory settings screen and its measurement
  harness.
- Voice in the web shell, on Android or on Windows.
- New languages beyond the eight, or downloadable voices for the seven non-English ones.
- Wake words, always-on listening, or voice while the app is closed.
- Voice approval of risky actions.

**Anti-goals.** Any of these makes the feature a failure even if it works.

- A conversation the kernel never sees.
- A system voice or a hosted service standing in without the person being told.
- A canned sentence before every answer.
- An answer that stops for a cough, or keeps going when the person clearly cut in.
- Audio leaving the device for recognition or speech.

---

## 4. Decisions

### 4.1 The owner's decisions

| Id | Decision |
|---|---|
| O1 | Swap the voice model in the Apple apps for Rumble. The seven languages other than English use the default system voice. |
| O2 | A system voice may speak when it is the chosen voice for that language. If the local English voice fails to load, the app switches English to the system voice by itself and says that it did. This changes spec 105 section 12.1. |
| O3 | Remove LFM3 entirely. Use LFM2.5 from the pack as the local model. Normal turns follow the ecosystem's normal conversation flow and go to the provider. |
| O4 | The overlay shows a waveform and no transcript. |
| O5 | The conversation must sound and flow as naturally as possible. The pilot's canned fillers and its interruption handling were the clunky parts. |
| O6 | Live testing: when the build is ready, the owner connects the iPhone. The agent sideloads the app and holds the conversation through the laptop's speakers and microphone. |
| O7 | A mic tap with no pack installed downloads Rumble from the Kentucky Open Science organization on Hugging Face and the voice model from its own page. |
| O8 | Voice works on iPhone and Mac. The watch connects to the phone, or does whatever Apple recommends. |
| O9 | English offers two voices in Settings, the local voice as the default and the system voice, each with a preview. Decided 2026-11-19 for Piper and carried over to its replacement. |
| O10 | When the provider goes offline during a conversation, Astral says "Your LLM model provider is offline." and keeps talking using local resources only. Those exchanges are recorded in the text conversation. When the provider returns, Astral says "Your LLM model provider is back online." and sends queries to it as normal. The owner called this an edge case and asked that it weigh less than the rest. |
| O11 | Piper was only part of the pilot. Use a similar model with a more open licence and similar speed if one exists. Otherwise use the default iOS English voice. |
| O12 | The entity that distributes Astral has annual revenue under $10 million. LFM2.5 is therefore licensed for this use under the LFM Open License v1.0. If that revenue crosses $10 million, the local model needs a commercial licence from Liquid AI or a replacement. |
| O13 | After listening to the phase 0 recordings, the owner picked Phonon 31m as the local English voice. Decided 2026-11-20. This answers O11. |

### 4.2 This design's calls

| Id | Call | Alternative that lost |
|---|---|---|
| C1 | Swap the models underneath the shipping `VoicePipeline`. | Port the pilot's own loop. The shipping pipeline already has the better turn-taking, the integrity checks and the tests. |
| C2 | Every local exchange is written to the kernel log. | The pilot's transcript in the app container. It never syncs and sits outside the gates. |
| C3 | LFM2.5 drafts each acknowledgment and code checks it. | The pilot's regular expressions, and today's six fixed phrases. Both sound canned. |
| C4 | No acknowledgment when the answer is already arriving, or when the turn is small talk. | An acknowledgment before every answer. |
| C5 | An interruption counts only when Rumble hears words in it, and only after the audio passes a 0.4 s minimum and a voiced-frame gate (section 8.3). | Stopping on sound level alone. That is what made the pilot stall on noise. |
| C6 | Keep the shipping two-stage end of turn, with word lists for all eight languages. | The pilot's fixed 0.3 s of silence. |
| C7 | System voices render to buffers and play through the app's own audio engine. | Letting the system play them directly. Echo cancellation and interruption would not apply. |
| C8 | Settled. Phase 0 measured Kokoro, Phonon 31m and Pocket TTS on the iPhone 13 Pro. Phonon and Pocket passed, Kokoro was rejected, and the owner picked Phonon (O13). Section 7.4 records the test. | Naming a winner from published figures. None of the three had a published iPhone number. |
| C9 | Keep the existing download sheet. The overlay opens by itself when the download finishes. | Starting a transfer of more than 1.2 GB with no confirmation. |
| C10 | Two new local packages. `AstralVoiceRuntime` wraps llama.cpp as an xcframework built by a pinned script and does nothing else. `AstralVoiceSpeech` wraps the FluidAudio Swift package and runs Phonon on Core ML. | One package for both, which would put speech code beside the llama.cpp wrapper. Sherpa-ONNX for speech, which links eSpeak NG under GPL-3.0. |
| C11 | Delete the installed LFM3 weights on first launch of the new version. | Keeping 1.42 GB that nothing reads. |
| C12 | In a voice session the local talker is always LFM2.5. Typed chat keeps today's order, Foundation Models first, with LFM2.5 where LFM3 was. | Foundation Models in voice too. Its speed and languages vary by device. |
| C13 | The watch keeps the WatchConnectivity relay and stays tap-to-interrupt. | Direct networking from the watch. Spec 106 removed the watch's network identity. |
| C14 | The overlay covers the chat on iPhone, the chat pane on Mac and the whole screen on the watch. | A floating panel on Mac. |
| C15 | Timing budgets are targets measured in the live pass. CI does not gate on them. | Wall-clock gates in CI, which the engineering rules forbid. |
| C16 | The live listening pass is required on iPhone only. Mac gets a live check by the agent. The watch qualifies by code tests. | Requiring the owner's ears on all three. |
| C17 | While the provider is online, the local model never answers a question. Something said during a running job becomes the next provider turn, and saying stop cancels the job. This follows from O10. | The pilot's side chat with the local model during a job. |
| C18 | Pin llama.cpp at `8af68d486a9473820d24d00af8f33334ca80124d`. The short-clip fix does not exist upstream, so the recognition wrapper pads each clip with zeros to `16000*k - 160` samples, where k is the smallest whole number of seconds that fits the clip. A voiced-frame gate of five frames and a 0.4 s minimum sit in front of word-confirmed interruption. (R2, R11) | Carry the third-party patch `qwen3a-tail.patch`. Its licence is unknown and its edge over padding is within noise on 169 clips. |
| C19 | "Offline" means a failure the kernel already treats as fallback-eligible: unreachable, timeout, stream failure, 404, 408, 429 and 5xx. A 400, 401, 403 or 422 stays an error shown to the person. (R3) | Treat every refusal as offline. A bad key would then hide behind a local answer. |
| C20 | The kernel gains four additive pieces: a structured fallback event with a reason code, an on-device-only model route, a provider probe with no body and no log write, and an `answered_by` field on the turn. The default route is unchanged. (R3) | A host-side workaround. The kernel always tries the provider first, so the host could not guarantee that no request carries the person's words. |
| C21 | Pin FluidAudio at `v0.18.4`, commit `e0c6103bc87a88d25276cad491c86e757f58c865`. A build step removes its `LuxTtsG2pResources` bundle, which holds two data files that FluidAudio's own note calls GPL-3.0-or-later, and a test asserts the bundle is absent. (R5a, R9) | FluidAudio as published, which ships those files. Forking the package is the other fix R9 names. |
| C22 | The acknowledgment draft starts at the speculation point and runs in the silence window. A partial tick is skipped while the previous partial is still running. (R1) | Drafting in the gap between two partials. R1 measured that the draft does not fit in that gap at any segment length. |
| C23 | Code picks the opening stem from a rotated list per language and passes it to the model. An allow-list of stems rejects answers in disguise. Phase 0 did not test this and phase 3 measures it first. (R10) | The rule that a draft must not repeat the last three openers. The model ignored the list and code rejected most good drafts. |
| C24 | Model drafts ship in English, Spanish, French and German. Mandarin and Arabic use generic lines until a native speaker has reviewed the drafts. Japanese and Korean use generic lines only. (R10) | Model drafts in all eight languages. |
| C25 | The app's own bundled manifest is the authority for the pack. It lists the three GGUF files and the Phonon Core ML files. (R9) | The pack repository's published v1.1 manifest, which still lists the Piper archive. |
| C26 | A watch voice session needs the phone app in front and unlocked, with its mesh open. C13 stands. (R6) | An audio background mode, a CPU-only path or a continued processing task. R6 did not recommend any of them. |
| C27 | Keep the 5.5 GB phone memory gate. (R8) | Lowering it to admit 4 GB phones. Nothing measured covers a 4 GB phone. |
| C28 | A device that cannot run Phonon, which means iOS 17 or macOS 14, downloads the three GGUF files only. The pack counts as complete and English uses the system voice. | Downloading 136 MB of voice files the device can never load. |
| C29 | A mirror for the pack serves the Hugging Face path shape, `<mirror>/<repository>/resolve/<revision>/<path>`. | The flat mirror layout the LFM3 store accepts. Two repositories at pinned revisions do not fit in it. |

---

## 5. What the person experiences

### 5.1 The first tap

1. The person taps the mic in a chat.
2. No pack is installed, so a sheet appears. It names the download and its size, more than 1.2 GB, before it
   asks for anything else.
3. The person taps Download. On cellular or a constrained network the app asks once more.
4. Progress shows in the sheet. The person can leave it and keep typing. The download resumes after a
   relaunch.
5. When the last file verifies, the sheet closes and the overlay opens, provided the person is still in
   that chat and the app is in front. Otherwise the mic shows that voice is ready.
6. The app asks for the microphone the first time the overlay opens.

### 5.2 A conversation

```
+------------------------------+
| [EN v]                   [X] |
|                              |
|                              |
|    ..|.||l|||l||.|..         |
|                              |
|          Listening           |
|                              |
|           [ mute ]           |
+------------------------------+
```

The person talks. The waveform follows their voice.

They stop. Within about half a second Astral starts to answer. For a quick question that is the answer
itself. For one that needs the provider to think or use a tool, it is a short line that fits the
question, followed by the answer when it arrives.

The person can cut in at any point. Astral lowers its voice the moment it hears them, and stops once it
is clear they are saying something. A cough or an "mm-hm" does not stop it.

Closing the overlay ends the session. The chat underneath now holds the whole exchange as text.

### 5.3 When the provider is down

This is an edge case. It gets a simple, predictable design and no more (O10).

The person is talking normally and the provider stops answering. Astral says:

> Your LLM model provider is offline.

Then it keeps talking, using the local model only. If a question was waiting on the provider when it
failed, the local model answers that question, so the person does not have to repeat it. The state
word under the waveform reads Offline.

Local answers are shorter and less capable. No tool runs and nothing leaves the device.

Only the failures in C19 count as offline. A rejected key shows as an error, and Astral does not say the provider is offline.

Every offline exchange is written into the chat as text, like any other turn, and marked as answered
on this device.

The app keeps checking quietly. When the provider answers again, Astral waits for a pause and says:

> Your LLM model provider is back online.

From the next thing the person says, queries go to the provider as normal. The provider sees the
offline exchanges as part of the chat, so the conversation carries on where it was.

Both sentences are spoken in the session's language.

### 5.4 Languages

The menu at the top left lists the eight languages. Picking one sets three things together: the
language Rumble listens for, the language Astral replies in, and the voice.

English uses Phonon unless the owner chose the system voice in Settings, or the device runs iOS 17 or
macOS 14. The other seven use the default installed system voice for that language. If the device has no voice for it, the app says how
to install one and does not start.

The choice is saved on that device. The first default is the device's language when it is one of the
eight, and English otherwise.

### 5.5 Mac

The same mic, the same sheet and the same overlay, covering the chat pane. The sidebar stays visible.
Escape closes the overlay. Intel Macs cannot run the pack and say so, as they do today.

### 5.6 Watch

The person taps the mic on the watch. The watch asks the paired iPhone to start, shows the waveform,
and streams the microphone to the phone. The phone does all the model work and streams speech back.

The phone must be reachable, with the pack installed, its mesh open and the app in front and unlocked.
When it is not, the watch says which of those is missing.

The watch listens and speaks in turns. Tapping the screen interrupts.

---

## 6. The pack and its download

**Proposed:** the app bundles its own manifest in place of today's LFM3 manifest. It lists every file,
with its repository, pinned revision, byte count and SHA-256.

| Part | Source on Hugging Face | Bytes |
|---|---|---:|
| Rumble recognition, GGUF | `Kentucky-Open-Science/Rumble-2-Flash-Astral`, pinned revision | 283,424,640 |
| Rumble audio encoder, GGUF | Same | 214,393,024 |
| LFM2.5 1.2B Instruct, GGUF | Same | 730,895,168 |
| Phonon 31m, Core ML | `FluidInference/pocket-tts-coreml` at `c2e998e269e4d4b51f74061faddb8450cd8f9a9a`, folder `phonon/phonon-31m-en/` | 136,148,876 in the 12 files the loader needs. The whole folder is 362,189,660 bytes in 148 files, and the rest is not downloaded. |

The three GGUF files total 1,228,712,832 bytes. With the voice the pack is 15 files and 1,364,861,708
bytes. In phase 1 the store lane's agent installed it from the real repositories on a Mac in a
temporary test and reported that every digest verified. The three GGUF digests in the manifest match
the lab's files, hashed separately in phase 0.

The pack installs as two units, the three GGUF files and the voice, each with its own install marker.
A change to the voice never forces the GGUF files to download again. A device that cannot run Phonon,
which means iOS 17 or macOS 14, downloads the GGUF files only and the pack counts as complete (C28).

The published v1.1 manifest in the Kentucky Open Science repository lists a Piper archive of 67,230,653
bytes. The app does not use that manifest. Its own bundled manifest is the authority and lists the three
GGUF files and the Phonon Core ML files only (C25). Publishing a new pack manifest without Piper needs
the owner's word (section 17).

The existing store keeps its behavior: one file at a time with resume, a byte count and SHA-256 for
each, staging, an atomic swap, an install marker bound to the whole manifest, a free-space check, the
cellular confirmation, and exclusion from backup. Digests are checked again before any model loads.

Two things are new.

- **Two repositories.** The manifest names both. The app contacts no other repository for the pack. The
  voice model loads from the verified local folder, and the speech library is never allowed to fetch
  files by itself. FluidAudio's offline mode is on before any FluidAudio call, and the speech package
  calls only the load-from-folder API. Its tests point the registry at a loopback listener and assert
  that nothing connects (phase-0/R5a).
- **A partial outcome.** If the three GGUF files install and the voice files keep failing, the sheet
  offers one extra button: continue with the system voice for English. The app retries the voice later
  on its own and tells the person when it arrives.

The mirror setting stays, with its HTTPS rule. A mirror for the pack has to serve the Hugging Face
path shape, `<mirror>/<repository>/resolve/<revision>/<path>`. A flat mirror set up for LFM3 fails
safely with a 404. (C29)

The Phonon folder's `LICENSE` file is not one of the 12 files, so the Apache 2.0 text ships in the
app's acknowledgments (section 14).

Consent given for the LFM3 download does not carry over. The person confirms the new pack once.

---

## 7. Runtime

### 7.1 The seam

**Today** `VoicePipeline` talks to its model through `VoiceModelInference`, which has three functions:
`transcribe`, `synthesize` and `acknowledgement`.

**Proposed:** keep the pipeline and split the seam by job.

| Job | Implementation | Notes |
|---|---|---|
| Recognition | Rumble through llama.cpp with `libmtmd` | 16 kHz mono. Takes a language. Partials by re-running on the growing segment, as today. Each clip is padded with zeros to `16000*k - 160` samples (C18). |
| English speech | Phonon 31m through Core ML, or the system voice | One sentence at a time. Phonon streams, so playback starts before the sentence is finished. |
| Other speech | `AVSpeechSynthesizer.write` | Rendered to buffers, never played by the system directly (C7). |
| Local text | LFM2.5 through llama.cpp | Acknowledgments, local talk and the kernel's on-device model. |

All speech enters the existing duplex engine after one resampling step. Echo cancellation, ducking,
interruption and the rule that only played speech is recorded then apply to every voice alike.

Rumble and LFM2.5 share one llama.cpp runtime and never run on the GPU at the same instant. Recognition
has priority, because a late transcript delays everything after it.

### 7.2 The packages

Two new local Swift packages. The app project links both in phase 2.

**`AstralVoiceRuntime`** replaces `AstralVoiceModel`.

- It wraps llama.cpp and nothing else. Recognition and text generation come from this package.
- llama.cpp is pinned at `8af68d486a9473820d24d00af8f33334ca80124d`, built with `libmtmd`. That revision
  holds the Metal change that turns a background GPU failure into an error the host can read (PR 20306).
  It has no short-clip fix, because none exists upstream (phase-0/R2). The recognition call pads each clip
  with zeros to `16000*k - 160` samples (C18).
- A script in the repository builds the xcframeworks, the way the kernel's Apple build script does.
  Git ignores the outputs. The build turns `MTMD_VIDEO` off, because that feature starts ffmpeg as a child
  process and iOS forbids child processes (phase-0/R9).
- Slices: iPhone, iPhone simulator and Mac, all arm64. Metal on devices and Macs, CPU in the simulator.
- Measured on an Apple M5 (phase-0/R4): a command line host linked to `libmtmd` and signed with the
  hardened runtime and only the audio-input entitlement loaded both models and ran them on Metal. It
  needed no JIT, unsigned memory or library validation exception. App Sandbox and notarization were not
  tried.
- `llama_decode` and `llama_batch_get_one` are deprecated at the pinned revision (phase-0/R4), so a later
  re-pin may need code changes.
- The watch target does not link it.

**`AstralVoiceSpeech`** wraps the FluidAudio Swift package for Phonon and nothing else.

- FluidAudio is pinned at `v0.18.4` by commit `e0c6103bc87a88d25276cad491c86e757f58c865` (C21). Version
  0.17.7, which an earlier draft named, does not contain Phonon (phase-0/R5a).
- It loads from the verified local folder with `ModelHub.offlineMode` on and the registry URL pointed at a
  closed loopback port, both set before any FluidAudio call. Phase 0 loaded all three candidates this way
  on the iPhone 13 Pro with no error. It took no packet capture, so the no-network claim rests on those
  two switches.
- FluidAudio's text-to-speech build carries a resource bundle, `LuxTtsG2pResources`, with two data files
  derived from eSpeak NG. FluidAudio's own note lists them as GPL-3.0-or-later (phase-0/R9). Only its
  LuxTTS voice reads them. A build step deletes the bundle from the built app and a test asserts that it
  is absent. Phase 0 deleted it by hand and Phonon still spoke ten lines in three passes with the same
  timings (phase-0/R5a). Whether App Store validation and signing accept the deletion was not tested.
- Phonon needs iOS 18 or macOS 15. On iOS 17 and macOS 14 the package reports unavailable and English
  uses the system voice.
- The watch target does not link it.

The simulator gets a CPU build of the runtime so tests can run real inference on tiny fixture models. The
device gate still keeps the feature off in the simulator for users.

### 7.3 Loading

Models load when the overlay opens. The waveform shows a "Getting ready" state until the microphone is
live. On the iPhone 13 Pro that took about 3.8 s in the pilot.

Phase 0 measured load times in a bench app on the same phone. Phonon took 5.3 to 5.7 s. Rumble took 0.2
to 0.9 s after the first run and 12.9 s on the very first launch after install (phase-0/R12). The live
pass records the first open after a download.

The voice warms in parallel with the microphone. Models unload 60 seconds after the overlay closes, or
at once under memory pressure.

The device gate stays: Apple silicon, and at least 5.5 GB of memory on a phone (C27). The iPhone 13 Pro
reported 5,941,428,224 bytes, so the gate admits it.

Phase 0 measured memory on the iPhone 13 Pro in a bare bench app with Rumble and LFM2.5 resident.
`phys_footprint`, the figure the system's limit is judged on, peaked at 523 to 528 MB while Phonon spoke
ten lines, against 491 MB before the voice loaded. Resident size at the end of those runs was 1.57 to
1.64 GB because the GGUF weights are memory mapped (phase-0/R12). The pilot's 1.85 GB with Piper was
resident size (`ru_maxrss`) and not footprint, so the two figures do not conflict (phase-0/R8). Nothing
measured covers a 4 GB phone or the real app with its mesh runtime and interface, so the gate does not
move.

### 7.4 The English voice: the test and its result

The owner's rule (O11): a model like Piper with a more open licence and similar speed, or else the
system voice.

Piper itself is out. Its runtime links eSpeak NG, which is GPL-3.0, and the rights to redistribute its
Lessac voice are unclear.

Phase 0 tested the three models that passed the licence test on paper. All three run on Core ML through
FluidAudio. Each spoke the pilot's ten acknowledgment lines on the iPhone 13 Pro with Rumble and LFM2.5
resident. Piper ran in the same bench as the reference. The figures below are measured on one iPhone 13
Pro on iOS 27.2.1 in a Release build (phase-0/R12).

The bench clock runs from the synthesize call to the first non-silent sample. It leaves out recognition
and the audio route, so none of its figures compare with the pilot's 0.539 s. The comparison is the
difference to Piper on the same clock.

| Candidate | Licence | Median to first audible | Footprint peak | Result |
|---|---|---:|---:|---|
| Piper, reference only | Fails, links eSpeak NG (GPL-3.0) | 0.210 s | 658 MB | Not shippable |
| Phonon 31m | Apache 2.0 | 0.169 s | 523 to 528 MB | Passed. The owner picked it (O13) |
| Pocket TTS small, 18M | CC-BY-4.0, attribution required | 0.048 s, or about 0.21 s at a speech-level threshold | 499 to 538 MB | Passed. Not picked |
| Kokoro-82M | Apache 2.0 weights, front end data not cleared | 0.522 s, or about 0.21 s with leading silence trimmed | 698 to 699 MB | Rejected |

Phonon against the four tests of the original plan:

- **No GPL code and no field-of-use restriction.** Passed (phase-0/R9). Phonon's weights, tokenizer and
  voices are Apache 2.0. Text goes through a rule-based normalizer and a SentencePiece tokenizer, with
  no phonemizer and no eSpeak. Only the 31m English model is Apache. Gradium lists every 40m model as
  CC BY-NC 4.0, so the pack names the 31m model exactly. FluidAudio carries GPL data for another voice,
  and C21 removes it.
- **Median first word within 0.25 s of Piper.** On the bench clock that means at most 0.46 s. Phonon
  measured 0.169 s against Piper's 0.210 s. It streams, with its first chunk at 0.050 s.
- **Memory budget.** The spec set no number, so there is no pass or fail. Every footprint peak above is
  under 700 MB.
- **Loads from a local folder with no network request.** Passed (phase-0/R5a, section 7.2).

Phase 0 did not measure the output route latency, the full turn clock or speech quality. The owner
judged quality by listening to the recorded lines. The recordings sit outside Git in
`AstralDeep-wt/voice-bench-109/phone/recordings/`.

**Considered and not shipped.**

- **Pocket TTS small, 18M.** Passed the speed and memory tests, as the table shows. The owner did not
  pick it, so it does not ship. Its licence needs an attribution line, and it was released in October
  2026.
- **Kokoro-82M.** Rejected for three reasons. The data behind its English front end has no stated
  origin, because the Misaki dictionary does not say where its entries came from (phase-0/R9). It is
  slower, with a median of 0.522 s to first audible and a load of 10.2 to 11.0 s, against 5.3 to 5.9 s
  for the other two (phase-0/R12). And FluidAudio's own advisory says that on iOS 27 no Core ML route
  is known to be safe, with an uncatchable crash after about an hour of synthesis on the default route
  and within minutes on the Metal route (phase-0/R5a, read and not reproduced). Phase 0 ran 60 Kokoro
  lines across two launches on the phone without a crash.
- **Supertonic 3.** Fast, with 31 languages. Its OpenRAIL-M licence forbids use "to provide medical
  advice and medical results interpretation". Astral is a clinical platform. The project was also
  archived on 2026-09-09 with no further support.
- **KittenTTS 2.** The small models are Apache 2.0. There is no iPhone runtime, and the one portable
  path uses eSpeak NG data.
- **Matcha.** Its English model needs eSpeak NG.
- **Pocket TTS 100M.** About 1.8 times real time on a Mac. Too slow.

Phonon and the newer Core ML builds need iOS 18 or macOS 15. The apps still support iOS 17 and macOS 14.
On those, English uses the system voice.

---

## 8. Making it sound natural

This is the heart of the spec. Four things decide how a spoken conversation feels: when Astral decides
the person is done, what it says first, how it handles being cut off, and how its speech is paced.

### 8.1 Knowing when the person is done

**Today** the pipeline speculates after 0.25 s of silence and ends the turn after 0.6 s. If the last
word was a continuation word such as "and" or "because", it waits 0.4 s more. The word list is English.

**Proposed:** keep that design (C6).

- Rumble produces a partial transcript about twice a second while the person talks. A tick is skipped
  while the previous partial is still running (C22). Measured on the iPhone 13 Pro, a partial takes
  0.25 s on a 2 s segment, 0.45 s at 5 s and 0.80 s at 10 s, so a 0.5 s tick falls behind on a long
  utterance (phase-0/R1).
- The last word of a partial is provisional. Padding stops invented words on noise. It does not stop a
  misheard last word when a clip ends in the middle of a word. Phase 1 measured "Name one fruit" cut
  at 0.6 s coming back as "Name one from." A draft, a continuation check or an interruption check that
  rests on the last word waits for the next partial or the final transcript.
- Each of the eight languages gets its own continuation list. A language with no list uses the plain
  0.6 s.
- A partial that ends mid-number or mid-list also earns the extension.
- The live pass tunes the three durations. The values above are the starting point.

### 8.2 What Astral says first

The pilot opened nearly every answer with a sentence built from a pattern, such as "Let me look into
the colors of apples." Ten of those in a row sound like a machine. The shipping app picks from six
fixed phrases, which is worse.

**Proposed:** three rules.

**Rule 1. Say nothing extra when the answer is quick.** The acknowledgment is held for a short window
after the end of the turn. If the first sentence of the real answer is ready inside that window, Astral
speaks the answer and the acknowledgment is thrown away. The window starts at 0.3 s and is tuned in the
live pass.

The hold costs no time when the filler is used. The filler is drafted and synthesized before and
during the window, so its audio is ready to play the instant the window closes.

**Rule 2. Small talk gets an answer, with no filler.** Greetings, thanks, yes, no and one-word replies
never get an acknowledgment.

**Rule 3. When a filler is needed, the model writes it and code checks it.**

LFM2.5 starts drafting at the speculation point, from the partial transcript, so the line is usually
ready when the turn ends. The voice synthesizes an accepted draft at once. The model sees the question,
the language and the opening stem that code chose for it.

The draft runs in the silence window and not between Rumble partials. R1 measured on the iPhone 13 Pro
that a partial takes 0.25 s on a 2 s segment, 0.45 s at 5 s and 0.80 s at 10 s, and that a draft takes
0.26 to 0.42 s. The gap between two partials is 0.25 s at 2 s, 0.05 s at 5 s and none at 10 s, so the
draft fits in it at no length. At the speculation point no new audio is arriving and partials stop being
the bottleneck. That is reasoning from R1 and not a measurement. If the final transcript differs from the
partial the draft was written for, the draft is checked against the final text and dropped when it no
longer fits.

Code accepts the draft only if all of these hold:

- At most twelve words and one sentence.
- In the session's language.
- No question mark.
- No digits and no claim that something was done, found or sent.
- It starts with the stem code chose, and that stem is on the language's allow-list.
- The request did not look private or technical. Phone numbers, addresses, keys and the like get a
  generic line.

A rejected draft falls back to a short generic line, rotated so the same one never plays twice in a
row. The model proposes and code decides.

If the person keeps talking after the speculation point, the draft is discarded.

**The opening stem (C23).** Code picks the stem from a rotated list per language and passes it to the
model as the required start. The allow-list of stems per language rejects an answer in disguise, such as
a line that states a fact. This replaces the first draft's rule that a draft must not repeat the last
three openers. R10 ran that rule on 14 normal requests per language. The model kept reusing one stem
whatever it was told. Code accepted 8 of 14 English drafts and only 1 to 3 of 14 in Spanish, French,
German, Arabic and Mandarin. The form checks also passed bad lines written with a plainer prompt, such
as "I can't access your emails directly." in English and a diet suggestion in Mandarin. The stem design
was not tested in phase 0. Phase 3 measures it first.

**Languages (C24).** Model drafts ship in English, Spanish, French and German, where R10 judged 14, 14,
10 and 13 of 14 lines good. Mandarin and Arabic use generic lines until a native speaker has reviewed the
drafts. R10 judged 11 and 12 of 14 acceptable and asks for that review. Japanese and Korean use generic
lines only, with 2 and 0 of 14 judged good.

**Longer waits.** A tool call or a slow answer gets one spoken progress line, drafted the same way from
the kernel's own progress events. At most one such line every 15 seconds.

### 8.3 Being interrupted

The pilot stopped its answer on any burst of sound, then sometimes waited on a segment with no words in
it. The owner felt that.

**Proposed:** an interruption has three stages (C5).

1. **Sound over speech.** The moment the detector hears the person while Astral is talking, playback
   drops to 15 percent. Nothing is cancelled. This is today's behavior.
2. **Is it speech from this person?** The detector's threshold rises with the recent level of the
   person's own voice, so quieter room sound does not count. This comes from the pilot.
3. **Does it have words?** Rumble transcribes the overlapping audio. The interruption is confirmed
   only when the transcript has words that are not a backchannel.

Stage 3 trusts Rumble to return nothing for noise, with two guards in front of it (C18). A clip shorter
than 0.4 s never confirms an interruption by itself. The overlapping audio must also hold at least five
voiced frames, which is 50 ms, before it goes to Rumble. A frame is 25 ms taken every 10 ms. It counts as
voiced when its autocorrelation between 60 and 400 Hz is above 0.5, its level is within 30 dB of the
loudest frame and it is above minus 55 dBFS. The recognition call also pads the clip (section 7.2).

R11 measured this on an Apple M5 with file input and not on a phone. Stock Rumble returned words for 39
of 169 clips with no speech in them: coughs, breathing, keyboards, door knocks, noise and short cuts.
With the padding the count was 4. With the padding, the 0.4 s rule and the voiced-frame gate it was 2,
both sneezes, which are voiced. Every real "stop" and "wait" came through, though the test clips were
clean and loud. A quiet "stop" at conversational volume may fail a gate set from them, and the live pass
checks that. Two to four false words is too few to tell 1 percent from 5 percent.

R11 could not test Astral's own voice leaking back through echo cancellation into the microphone. That is
speech and Rumble will transcribe it. It is the main risk to word-confirmed interruption and needs the
live pass on iPhone (section 15).

Then one of three things happens.

| What was heard | What Astral does |
|---|---|
| Nothing, or noise | Volume returns. The answer continues from where it was. |
| A backchannel: "mm-hm", "okay", "right" | Volume returns. The answer continues. Each language has its own list. |
| Real words | Speech stops. The unplayed part of the answer is dropped. The new words become the next turn. |

Rumble returned the backchannel "mm-hm" as "M.M.H.M." in phase 0 (R11). The backchannel lists match
without regard to case, punctuation or spacing, so a spelled-out form still counts.

"Stop", "wait" and "hold on", in each language, stop speech at once and also cancel a running job when
that job is safe to cancel. A pending risky action is never cancelled or approved by voice.

Only the sentences that finished playing are recorded as spoken. That rule is unchanged.

After Astral finishes speaking, capture stays as it is. Echo cancellation in the audio engine keeps
Astral from hearing itself.

### 8.4 How the speech is paced

- The provider's answer streams sentence by sentence into the voice. Speech starts on the first
  sentence and never waits for the whole answer.
- Each sentence is fully synthesized before it plays, so there are no gaps inside a sentence. The next
  one is synthesized while the current one plays.
- Voice turns carry a preference to the provider for short, conversational answers, with more detail
  when the person asks for it. The pilot did this.
- Markup, links, code and table syntax are stripped before speech. A result that is a component speaks
  its `spoken` form.
- If an answer arrives while a filler is playing, the filler finishes and the answer follows with no
  pause.

### 8.5 Targets

These are targets for the live pass on an iPhone 13 Pro, warm, with the local English voice. CI does not gate on them
(C15).

| Moment | Target |
|---|---|
| End of the person's turn to the first audible word | Median 0.8 s or less. The pilot measured 0.54 s with Piper and 1.09 s with the system voice, both with no hold. This design adds a 0.3 s hold. |
| The person starts talking over Astral, to lowered volume | 150 ms or less |
| A confirmed interruption, to silence | 600 ms or less from the first word |
| Mic tap to the overlay on screen | 300 ms or less |
| Overlay open to listening, cold | 4 s or less |

The pilot's 0.54 s and the bench clock in section 7.4 measure different spans. Phonon's median of 0.169 s
to first audible sample does not compare with the pilot figure (phase-0/R12).

---

## 9. The kernel, safety and the log

### 9.1 What does not change

- A recognized utterance becomes an `OwnerVoiceTurn`. It has owner integrity only when it came from
  this device's microphone, or a paired watch's, in a session the owner started.
- The text goes to the kernel as an ordinary message. Tools, permissions, PHI gates, consent and audit
  apply as they do to typed input.
- Voice cannot approve anything. When a consent prompt appears, Astral says that something needs
  approval, and the normal consent sheet opens over the overlay.
- Audio from a file, a call or a video is untrusted.
- Only speech that played is written to `chat.spoken`.

### 9.2 The local model's two jobs

| Job | When | Reaches the kernel as |
|---|---|---|
| The acknowledgment and progress lines | Provider online | `chat.spoken` records, as today |
| The whole answer | Provider offline | A kernel turn served by the on-device model, in the same chat (C2) |

While the provider is online the local model answers nothing (C17). Something the person says during a
running job is queued as the next provider turn. "Stop" cancels the job when it is safe to cancel.

LFM2.5 replaces LFM3 as the host's `OnDeviceModel`. The existing limits stay: plain text only, bounded
input and output, no structured schemas.

PHI rules already treat the device as a named destination. A local answer follows them.

### 9.3 The kernel change

The app needs a clear signal that the provider is unreachable, a way to serve a turn from the on-device
model in the same chat while it is, and a way to check that the provider is back without sending
anything. Phase 0 read the kernel at `4c43032` (phase-0/R3, source read, nothing built or run). The first
two partly exist and the third does not. A kernel change is needed. It is additive, about 300 lines of
Rust plus tests, and changes nothing while the new route stays at its default.

**Today.** The kernel classifies a provider failure precisely, but only the English notice sentence
reaches the host. It falls back to the host's on-device model inside a turn, automatically, and the signed
log already marks the answer. It tries the provider first on every turn. Nothing probes a provider
without sending a chat completion.

**Proposed.** Four independent pieces (C20).

| Piece | What it adds | Tests |
|---|---|---|
| A. Fallback event | A reason code (the code of the last provider error) on the fallback notice, journaled as an additive `fallback` key with kind `credential`, `model` or `on_device`. A new `TurnEvent::ModelFallback` with chat id, turn id, kind and reason, and a matching FFI `TurnProgress::ModelFallback`. The English notice stays. | The reason is right for 503, unreachable, first-token timeout and 429. A 400 produces no fallback event. Journal replay ignores the new key. |
| B. On-device-only route | `ModelRoute { Auto, OnDeviceOnly }` held by the turn service, with an FFI setter and getter. `OnDeviceOnly` builds the client with no endpoints, so no request is formed for a remote provider. The engine gets an empty tool list in this route. The route is process state and returns to `Auto` after a restart. | `OnDeviceOnly` never calls the transport. The exchange lands as user message, final model response with provider `on-device`, and assistant message. A following `Auto` turn sends it as history. A PHI-denied on-device candidate still fails closed. Cancel and resume still work. |
| C. Provider probe | `probe_model_provider` sends `GET {base_url}/models` through the same guarded client, with the stored credential and a 5 s limit. It sends no body and no chat data, writes nothing to the log and meters nothing. It returns reachable, unreachable, refused with a code, or not configured. A 408, a 429 and any 5xx count as not back, so a rate-limited provider does not flip the conversation between the two notices. Any other response counts as reachable, including 401 and 403. | Reachable, 401, 408, 429, 503, connect refused, timeout and no provider configured. No log append, no metering, and no body fields from any chat. |
| D. `answered_by` | An `answered_by` field on the turn in the chat read model, taken from the provider of the last final model response and passed through FFI. | The field reads `on-device` after an offline turn. |

R3 marks three things as assumed, and each needs a kernel test. The empty tool list is a few lines in
`Runner::tool_defs`. Provider endpoints answer `/models`, and a 404 still proves reachability. And
`ModelClientConfig::new(vec![], vec![])` is safe for a normal turn and not only for the cancel-only path.

The new `TurnProgress` variant breaks any exhaustive match on it. The Swift `switch` in
`KernelMapping.turn` changes in the same change. R3 did not check whether the Rust command line tool
matches exhaustively.

The host's side, from R3. On a fallback event of kind `on_device`, it speaks the offline sentence and sets
the route to `OnDeviceOnly`. While offline each utterance goes through `submitVoice` as today. A probe on a
timer and before each turn decides recovery. On reachable it waits for a pause, speaks the back-online
sentence and sets the route back to `Auto`. If the first `Auto` turn fails again, a new event repeats the
cycle. The chat view reads `answered_by` to mark each turn as answered on this device. The existing label
at `MeshChatViews.swift:440` says "Add a model in Settings for fuller answers", which is wrong for an
outage, so it needs its own wording.

C12 needs no kernel change. The host holds a flag for an active voice session that the on-device model
reads. A typed turn during a voice session would also see LFM2.5, and phase 3 checks that race.

**Built, 2026-11-20.** The four pieces are on the AstralKernel branch `feat/109-offline-route`, open as
draft Kernel PR 22 and unmerged. The affected crates' tests pass. Three things differ from the text above or add to it.

- The reason code reaches the host through a new observer method. The fallback notice type itself is
  unchanged, so no existing kernel test changed.
- The kernel's shared model client, which learning and run summaries use, also honors the route. With
  the route at on-device only, those background calls cannot send chat-derived text to a provider.
- An on-device-only turn on a device with no on-device model fails with `route.on_device_unavailable`
  before anything is written to the log.

The empty candidate list is safe for a normal turn, and a tool call from a round that offered no tools
is dropped. Only the kernel's own event-to-progress mapping matched exhaustively. The Python binding
suites were not run, because they need the Linux build.

**This change needs the owner's review on the Kernel pull request before it merges, and before phase 3
starts.**

### 9.4 Provider failure and return

**Going offline.** The switch happens on the kernel's fallback event (section 9.3, piece A). Offline
means a failure the kernel already treats as fallback-eligible: the provider is unreachable, the request
times out, the stream fails, or the provider answers 404, 408, 429 or 5xx (C19). A 400, 401, 403 or 422
does not count. It ends the turn as an error shown to the person, because a bad key should surface and
not hide behind a local answer. A slow answer that completes does not trigger the switch. Astral speaks
the offline sentence once, then answers the waiting question locally. The kernel is already answering that
question in the same turn.

A provider that accepts the connection and then stalls is slow to detect. R3 derived a 60 s first-token
wait and three attempts from constants and did not measure it. A shorter deadline for voice turns is a
separate decision that this spec does not take.

**While offline.** The host sets the on-device-only route (piece B). Each turn is a kernel turn served by
the on-device model. It lands in the chat as text, marked as answered on this device (piece D). The route
forms no request to any provider, so no request carrying the person's words leaves the device. The engine
offers no tools.

**Checking.** The app calls the provider probe (piece C) before each turn and on a timer between turns,
starting at 15 seconds and backing off to one minute. The probe carries no conversation content, because
it never receives a request. A provider that answers 401 counts as reachable, and the next turn then shows
the error. A provider that answers 408 or 429 counts as still offline.

**Coming back.** When a probe passes, Astral speaks the back-online sentence at the next pause. It never
talks over the person and never cuts off its own sentence to say it. The host sets the route back to
`Auto`. The next turn goes to the provider with the full chat, offline exchanges included.

If the provider fails again, the same steps repeat. Each change of state is announced once.

In typed chat, with no voice session open, nothing is spoken. The same switch applies and the chat
shows the two notices as text.

---

## 10. The overlay

One view for iPhone and Mac, and a smaller one for the watch.

| Element | Behavior |
|---|---|
| Waveform | Follows the microphone while listening and the output while speaking. A slow, calm motion while thinking. Flat while muted. |
| State word | One of: Getting ready, Listening, Thinking, Speaking, Offline, Muted. |
| Language menu | Top left. Changing it starts a fresh capture. |
| Close | Top right. Ends the session and returns to the chat. |
| Mute | Bottom. Stops capture and leaves speech playing. |

There are no captions and no transcript (O4). The chat underneath receives every turn as text, and it
is there when the overlay closes.

Errors appear as one line of text in place of the state word, with a button when there is something to
do: grant the microphone, install a voice, retry the download.

**Accessibility.**

- VoiceOver reads state changes. Every control has a name and keeps a stable identifier for tests.
- With Reduce Motion on, the waveform becomes level bars that step and do not flow.
- State is never shown by color alone.
- A person who cannot hear the conversation still has the full text in the chat and typed input beside
  it.

The session ends when the app leaves the foreground, the device locks, or the person moves to another
chat. Coming back does not restart the microphone. This matches today.

The composer's inline "Listening" state goes away. The mic opens the overlay.

---

## 11. Settings

The Voice row in Settings keeps its job and gains two controls.

- **English voice.** Phonon, the default, or the system voice. Each has a preview button that plays one
  fixed sentence, whatever the saved choice is (O9).
- **Language.** The same list as the overlay menu.

It also shows the pack's status, its size, and Remove.

Both choices are saved on the device. They do not sync.

If the local voice fails to load, the app switches English to the system voice, shows a notice in the
overlay and in Settings, and keeps the notice until the person dismisses it or the voice loads again
(O2).

On iOS 17 and macOS 14, where Phonon does not run, the English voice control is absent and English uses
the system voice.

---

## 12. The watch

**Today** the watch captures 16 kHz audio, sends it to the phone over WatchConnectivity, and plays the
24 kHz speech the phone sends back. It has no model and no network of its own.

**Proposed:** keep all of it (C13). The packet format does not change.

What changes on the phone side: the relay builds its pipeline on the new runtime, and local-voice or
system-voice output is resampled to 24 kHz before it is packed.

What changes on the watch: the mic opens a waveform screen in place of today's button and label. The
language follows the phone's saved choice.

This is also what Apple's rules point to. A watch app may open its own network connection only while
it streams audio or runs a call, and spec 106 took the watch's network identity away on purpose.
WatchConnectivity is the route Apple provides for immediate exchange between a watch app and its phone
app.

Three limits are stated plainly to the person and tested in code.

- The phone must be reachable. Losing it ends the session with a spoken and visible notice.
- The phone app must be in front and unlocked, with its mesh open (C26). This is how the code behaves
  today (phase-0/R6, read from code and not run on hardware). The watch can wake the phone process over
  WatchConnectivity, and the phone app's own rules then refuse the session because its scene is not
  active or its protected data is unavailable. Leaving the foreground or locking the phone ends a running
  session through the existing stop path. The new runtime changes what a violation costs. llama.cpp on
  Metal fails on GPU work from the background where the current runtime was never allowed to start. The
  pinned revision turns the abort into an error, and the runtime stops GPU work when the scene goes
  inactive, before the system revokes it. No background mode is added.
- The watch does not listen while it speaks. A tap interrupts.

On watchOS, voice processing is declared for watchOS 6 and later and compiles at the watchOS 10 target
(phase-0/R7). Nothing shows whether it works or cancels echo on a watch, and only hardware can settle
that. C13 stands either way. Listening while speaking would also need a capture path that stays open
during playback, a phone pipeline that accepts overlapping audio and a rule for how a spoken interruption
cancels playback. Spoken interruption on the watch stays a follow-up, outside this spec.

---

## 13. Retiring LFM3

- The `AstralVoiceModel` package, the MLX dependency and the LFM3 manifest are deleted.
- On first launch after the update, the app deletes the installed LFM3 weights and their staging folder
  (C11). That frees 1.42 GB.
- `VerifiedLFM3OnDeviceModel` is replaced by an LFM2.5 model with the same limits.
- The unused `NSSpeechRecognitionUsageDescription` leaves `Info.plist`.
- Tests that name LFM3 or MLX are rewritten against the new runtime or removed with the code they
  covered.

---

## 14. Rules and documents that change

| Where | Change |
|---|---|
| `AGENTS.md`, Mental model | The voice sentence allows a system voice as a chosen output, with a visible notice when it replaces the local voice. Hosted speech stays forbidden. |
| Spec 105 section 12.1 | A dated amendment: the platform table names Rumble, LFM2.5, the local English voice and the system voices. |
| Spec 105 section 12.5 | A dated amendment: spec 109 adds a required live pass on iPhone. Spec 105's own qualification is untouched. |
| `apple-clients/ONDEVICE-VOICE.md` | Rewritten for the new stack. |
| `apple-clients/README.md` | The voice lines. |
| `tests/test_apple_no_hosted_voice.py` | `AVSpeechSynthesizer` is allowed in one named file. Every other pattern stays forbidden everywhere. |
| App acknowledgments | Rumble with its Qwen3-ASR base, LFM2.5 with a copy of the LFM Open License and its notices, llama.cpp with the notices of its vendored miniaudio, stb and nlohmann code, Phonon 31m from Gradium, and FluidAudio with its NemoTextProcessing binary and their notices (phase-0/R9). |
| Build | A step removes FluidAudio's `LuxTtsG2pResources` bundle from the built app, and a test asserts it is absent (C21). |
| `config/astral-composition.json` | The Projection pin, when the work merges. |
| Vault | `synthesis-astral-conversational-voice-interface` and `project-astral`, at each checkpoint. |

`ui_protocol.json` does not change. Voice is local to each Apple client, so no other client needs a
matching change.

---

## 15. Qualification

**Code tests** cover every requirement that code can check, with fixture audio, fixture models, a fake
clock and a fake provider, as spec 105 section 12.5 describes. They include:

- The store: both repositories, path escapes, tampering, the partial outcome, and that the speech
  library makes no network request.
- The built app: `LuxTtsG2pResources` is absent.
- Recognition: clips are padded to `16000*k - 160` samples, and the 0.4 s minimum and the voiced-frame
  gate hold back a false interruption.
- Partials: a tick is skipped while the previous partial is still running.
- The opening stem: rotation per language, the allow-list rejecting an answer in disguise, and generic
  lines only in Mandarin, Arabic, Japanese and Korean.
- The kernel, in its own suite: the tests in section 9.3.
- The watch refusal: a session start from the watch is refused while the phone app is in the background
  or locked, and a running session ends when the scene goes inactive.
- The acknowledgment checker: every rejection rule, opener rotation, the quick-answer window.
- Interruption: noise, a backchannel and real words, each in at least English and one other language.
  A backchannel spelled out as "M.M.H.M." counts as one.
- End of turn with continuation words in all eight languages.
- Provider failure: both announcements, each spoken once, local turns written to the chat as text, the
  waiting question answered locally, recovery, that no request carries the person's words while
  offline, and that a 400, 401, 403 or 422 shows as an error and not as offline.
- Speech during a running job with the provider online becomes the next provider turn.
- Local exchanges reaching the kernel log.
- Integrity: a file or programmatic source never produces an owner turn. Saying yes approves nothing.
- The system voice path: buffers only, the automatic switch from the local voice, and its notice.
- The watch relay on the new runtime.
- The LFM3 cleanup.

**The live pass on iPhone is required (O6).** When the build is ready the agent asks the owner to
connect the phone. The agent installs a localtest build under the rules in `AGENTS.md`, plays scripted
speech through the laptop's speakers, records the replies through its microphone, and reports.

| Part | Content |
|---|---|
| Clean install | Mic tap, download, overlay, first turn |
| Ten varied questions | With the local voice, then with the system voice |
| Interruption | Real words, a cough, a backchannel, and a stop command |
| Offline | Provider made unreachable mid-conversation, the offline sentence, three local turns that appear in the chat, provider restored, the back-online sentence, one provider turn |
| A second language | Spanish, four turns |
| Timings | Every target in section 8.5 |

Phase 0 left some things unverified, so the agent records them in this pass: Astral's own voice leaking
back through echo cancellation into Rumble, contention when Rumble, LFM2.5 and the voice use the GPU at
the same instant, a cold first launch on a fresh install with the output route's latency, background
GPU behavior on the device, and a packet capture for the no-network claim (phase-0/README.md).

The owner then talks to it and decides whether it feels natural. That judgment is the gate. The
recordings and the timing report are kept with the spec's evidence.

**Mac** gets the same script run by the agent on this Mac, without a required sign-off (C16).

**Watch** qualifies by code tests. A live watch check is welcome and blocks nothing.

---

## 16. Calls worth the owner's eye

Most calls in section 4.2 follow from the owner's answers or from phase 0 measurements. These do more
than that.

- **C17.** With the provider online, the local model stays out of the conversation. The pilot let it
  chat during a running job. This design drops that, reading O10 to mean local talk is for the offline
  case only.
- **C4.** Astral often says no filler at all. The pilot always said one.
- **C19.** "Offline" means a failure the kernel already falls back on. A bad key shows as an error. This
  narrows the word "refused" in the first draft of section 9.4.
- **C18.** Padding over the patch. The only fix for the short-clip fault is a third-party patch with an
  unknown licence, and padding is app code whose measured edge is within noise.
- **C23.** The opener rule. Code picks the stem and a list of stems checks the answer. Phase 0 did not
  test it.
- **C24.** The language split. Model drafts in four languages and generic lines in the other four.
- **C16.** Only the iPhone needs the owner's ears.

---

## 17. Open questions

| # | Question | Who answers | Needed by |
|---|---|---|---|
| 1 | Which voice sounds right? Answered 2026-11-20: Phonon 31m (O13). | The owner | Done |
| 2 | How should the LFM Open License be read? It counts affiliates as part of the legal entity, its threshold says "or more" where its grant says "not exceeding", and one reading makes each recipient, such as a large hospital system, stay under $10 million itself (phase-0/R9). | A lawyer, for the owner | Before Astral is sold to an institution with revenue above $10 million |
| 3 | Do the Mandarin and Arabic drafts read naturally? | A native speaker of each | Before either language gets model drafts |
| 4 | May a new pack manifest without the Piper archive be published to the Kentucky Open Science repository? | The owner | Before release. The app's bundled manifest works without it |

---

## 18. Approaches considered

**A. Port the pilot.** Bring `Pilot.swift` and its loop into the app. Fastest to a demo, and it carries
over the exact behavior the owner found clunky. It also bypasses the integrity checks and the watch
relay the shipping pipeline has. Rejected.

**B. Swap the models under the shipping pipeline and add the overlay.** Keep turn-taking, interruption,
the kernel link and the watch relay. Replace the model seam, the store's manifest and the composer's
inline state. Add the pilot's good parts: the language triple, the speaker-level threshold, the English
voice choice. This is the design (C1).

**C. One audio model for everything.** Stay with a single model that hears and speaks, as LFM3 does. The
lab measured LFM3 at a 2.76 s median to first audio against 1.57 s for Rumble with LFM2.5, in English
only. Rejected by O1.
