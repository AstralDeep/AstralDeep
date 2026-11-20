# Phase 1 voice packages on the iPhone: device check

Run by a Sonnet agent on 2026-11-20. The orchestrating agent did not rerun it. Relative paths below
refer to `AstralDeep-wt/voice-bench-109/phone-phase1/`, which is not in Git.

## Answer

**Yes. Both packages work on the phone.** `AstralVoiceRuntime` and `AstralVoiceSpeech` built for iOS in Release, linked, loaded Rumble, LFM2.5 and Phonon, and ran every item below with no build, link, load or runtime error. The host app uses only the public Swift API of the two packages and no copy of their source.

Nothing is broken. Five findings matter for the next phase.

1. **Phonon through the package is the same sound as the owner's de-essed pick, but about 8.6 dB quieter.** The phase 0 `deess/` files were peak-normalized to 1.0 by FluidAudio's WAV writer. The package streams unnormalized audio with peaks of 0.33 to 0.57. The app needs a gain stage or the voice will seem quiet next to what the owner heard.
2. **CPU-only mode works but costs a lot.** Recognition is about 1.9 times slower, the LFM2.5 reply about 1.15 times slower, and the footprint of Rumble plus LFM2.5 rises from about 490 MB to about 1,400 MB. A background fallback needs that budget.
3. **Contention is mild.** Running Rumble, LFM2.5 and Phonon together slowed each by 0 to 23 percent. Phonon never fell behind real time in any run.
4. **The first launch after an install is slow.** Launch to all three ready took 20.0 s, then 0.9 s and 0.5 s on later launches. The Phonon share (about 5 s) came back after a reinstall, so it follows the install. The Rumble share (13.7 s) did not.
5. **The phone heats quickly under back-to-back decoding.** Thermal state reached serious (2) within about a minute of continuous recognition. Hot partials ran up to 27 percent slower. All headline numbers below were taken after a cooldown to nominal or fair.

## Conditions

- **Device and OS:** iPhone 13 Pro (iPhone14,2), iOS 27.2.1 (24B156), 5.94 GB RAM, 6 cores. Every **measured** tag below means this device and OS.
- **Build:** Release, iOS 18.0 deployment target, Xcode 27.2 with Swift 6.4.2, signed with the lab's wildcard development profile under the lab's bundle id. Version 1.0 build 2 during the work.
- **Package revision:** `536a33e530f8512df06acf3bdf4e02c57cb2da3d` (branch `feat/109-rumble-voice`, worktree unchanged afterwards). FluidAudio `e0c6103bc87a88d25276cad491c86e757f58c865` (v0.18.4), llama.cpp `8af68d486a9473820d24d00af8f33334ca80124d`. These two are **read** from the package manifests.
- **Models:** Rumble-2-Flash-AD-Q5_K_M with the Q8 encoder, LFM2.5 1.2B Instruct Q4_K_M, Phonon 31m English folder from phase 0. All were already on the phone.
- **Clock:** every duration is `CLOCK_MONOTONIC_RAW` on the phone. Cold start uses the kernel process start time against the wall clock. Times start at the call into the package API and end when the call returns or the stream finishes.
- **Package configuration:** default `RuntimeConfiguration` (automatic compute, 4 threads) unless item 3 says otherwise. Recognition limit 160 tokens, 96 for the ladder to match R1. The GPL LuxTTS resource bundle was removed with the package's own strip script before signing, as its README requires.
- **Raw data:** `raw/pull/*/result.json` and `log.txt` per run. Runs used: `cold-firstinstall`, `cold-second`, `cold-third`, `core-core1`, `core-core2` (after cooldown, the source of items 1, 2 and 4), `cpu-cpu2` (after cooldown, item 3), `voice-voice1` (items 5, 6, 7). `cpu-cpu1` and `core-core1` ran hot and are kept as evidence of throttling.

## 1. Does it run at all

**Yes.** All 7 fixtures transcribed correctly through `SpeechRecognizer` with the default accelerated configuration. **measured**, run `core-core2`.

| Fixture | Transcript | Time (s) | Tokens | Padded samples |
|---|---|---:|---:|---:|
| remember | Remember that my trail marker is blue. What color is it? | 0.347 | 13 | 79,840 |
| recall | What color did I tell you? | 0.222 | 7 | 47,840 |
| correct | Correction, my trail marker is green. What is the new color? | 0.358 | 14 | 79,840 |
| arithmetic | I packed 3 batteries and used one. How many are left? | 0.367 | 14 | 79,840 |
| fruit | Name one fruit that starts with the letter B. | 0.281 | 10 | 63,840 |
| count | Count slowly from one to 20. | 0.253 | 9 | 47,840 |
| interrupt | Stop counting. Say apple instead. | 0.242 | 7 | 63,840 |

The only differences from the reference sentences are punctuation and the digit style ("3" for three, "20" for twenty), which the default `RecognitionStyle` asks for. Every transcript was identical on a second run. Every call ended on end of sequence.

`TextGenerator` answered through its own chat template. **measured**.

| Prompt | Reply | Tokens | Time (s) |
|---|---|---:|---:|
| System "reply in one short sentence", user "Name one fruit that starts with C." | Cantaloupe is a fruit that starts with C. | 11 | 0.43 |
| Same system, user "What color is the sky on a clear day?" | The sky appears blue on a clear day. | 9 | 0.37 |
| Phase 0 acknowledgment prompt on the "fruit" transcript, limit 24 tokens | Banana | 3 | 0.21 |

The third row answered the question instead of acknowledging it, in one word. That matches R1's note that the crude draft prompt is only a timing probe. It is not a package fault.

## 2. Cancel and scheduling on the device

**Both behave as documented.** **measured**, run `core-core2`, counts are text deltas from `onText`. An uncancelled 60-token run produced 60 deltas, so one delta is one token in this test.

| Test | Result |
|---|---|
| Generation cancelled from inside `onText` at the 20th delta | `CancellationError`, 20 deltas at return, still 20 after 500 ms, the next generation answered normally |
| Generation cancelled from outside by a 0.2 ms poller at 20 deltas | `CancellationError`, exactly 20 deltas, still 20 after 500 ms |
| Recognition cancelled at the 3rd delta | `CancellationError`, 3 deltas, a rerun gave the same transcript as before |

Scheduling test. A 70-token generation (G1) was running. Then a second generation (G2) was requested, then a recognition (R). The scheduler's `waitingCount` reached 2 while it stayed busy, and R ran first.

| Operation | First text (s) | End (s) |
|---|---:|---:|
| G1 | 0.082 | 1.907 |
| R (requested after G2) | 2.079 | 2.206 |
| G2 (requested before R) | 2.274 | 3.035 |

R's own run after G1 ended took 0.298 s against 0.280 s alone, which a recognition that had overlapped G1 would not show. The windows do not overlap and recognition was served first. The limit of this evidence is that overlap is inferred from first-text and end times plus the scheduler state. The package does not expose when a slot is taken.

## 3. CPU-only mode on the device

**Both models load and run CPU-only on the phone with identical transcripts and replies.** **measured**, run `cpu-cpu2`, thermal state 0 or 1, median of 5 after one warm-up call. Two rounds of each mode agreed within 0.01 s.

| | CPU-only | Automatic (GPU) | Ratio |
|---|---:|---:|---:|
| Rumble, fruit clip (3.0 s), median | 0.535 s | 0.284 s | 1.9 |
| Rumble, arithmetic clip (4.0 s) | 0.694 s | 0.369 s | 1.9 |
| Rumble, correct clip (4.4 s) | 0.711 s | 0.368 s | 1.9 |
| Rumble, first call after load, fruit | 0.71 to 0.78 s | 0.26 s | 2.8 |
| LFM2.5, 11-token reply | 0.479 s | 0.417 s | 1.15 |
| Rumble load | 0.67 to 0.97 s | 0.28 to 0.37 s | |
| LFM2.5 load | 1.58 to 1.82 s | 0.08 s | |
| Footprint, both loaded | 1,378 to 1,389 MB | 485 to 512 MB | 2.8 |
| Footprint, both warm | 1,490 to 1,499 MB | 471 to 497 MB | 3.1 |
| Lowest `os_proc_available_memory` | 1,722 MB | 2,725 MB | |

CPU-only recognition of the same clip is about twice as slow. A 5 s partial would land near 0.8 s by that ratio, which is a projection and was not measured. The bigger cost is memory. The GPU path leaves the weights memory mapped, and the CPU-only path raised the footprint by about 900 MB. The reason for that is not isolated here. Backgrounding itself was not tested, so what happens to a loaded GPU model when the app leaves the foreground is still unknown.

## 4. Recognition timing through the package

**The package's partial ladder matches R1 within noise up to 4 s and at 8 to 10 s, and is 0.02 to 0.04 s faster at 5, 6 and 9 s.** **measured**, run `core-core2`, median of 3 per prefix, thermal 0 for the 2 s and 5 s ladders and 0 to 1 for the 10 s ladder. R1 values are **read** from `R1-rumble-partials.md` (same phone and OS, no padding, nominal thermal). Input is the same concatenation of the arithmetic, correct, remember and fruit fixtures.

| Prefix (s) | Package median (s) | R1 (s) | Padded samples |
|---:|---:|---:|---:|
| 0.5 | 0.129 | 0.129 | 15,840 |
| 1.0 | 0.185 | 0.184 | 31,840 |
| 1.5 | 0.209 | 0.222 | 31,840 |
| 2.0 | 0.252 | 0.253 | 47,840 |
| 3.0 | 0.333 | 0.333 | 63,840 |
| 4.0 | 0.366 | 0.366 | 79,840 |
| 5.0 | 0.417 | 0.454 | 95,840 |
| 6.0 | 0.478 | 0.494 | 111,840 |
| 7.0 | 0.570 | 0.578 | 127,840 |
| 8.0 | 0.654 | 0.651 | 143,840 |
| 9.0 | 0.706 | 0.742 | 159,840 |
| 10.0 | 0.802 | 0.800 | 175,840 |

The conclusion is unchanged. A 2 s segment (0.25 s) fits a 0.5 s tick, a 5 s segment (0.42 s) barely fits and a 10 s segment (0.80 s) does not.

The package pads every clip to 16,000 times k minus 160 samples, so a 0.5 s prefix is fed as nearly a full second. That padding is not visible in the times. The 0.5 s partial took 0.129 s here and in R1. The cost rises with the number of decoded tokens more than with the padded length, as the equal padded size at 1.0 s and 1.5 s but different times shows. The small advantage at 5, 6 and 9 s is not explained by padding. A likely cause is that R1 ran the ladders back to back on a warming phone. I did not test that. The same ladder run hot (core-core1, thermal 2) took 1.016 s at 10 s, which is 27 percent above the cool figure.

## 5. Phonon through the package

**The package's streaming path with its per-frame filter is as fast as phase 0's raw streaming path.** **measured**, run `voice-voice1`, Rumble and LFM2.5 loaded and warm, 10 acknowledgment lines times 3 passes. Phase 0 values are **read** from R12 (Phonon 31m, pass 1 medians, bench clock).

| Metric | Package (30 lines) | Phase 0 raw stream |
|---|---:|---:|
| First buffer, median (min to max) | 0.049 s (0.016 to 0.053) | 0.050 s |
| First audible sample, median (min to max) | **0.166 s** (0.125 to 0.191) | **0.169 s** (0.157 to 0.189) |
| Total time per line, median (min to max) | 0.377 s (0.283 to 0.470) | 0.394 s |
| Audio per line, median | 2.80 s | 2.88 s |
| Real-time factor, median | 7.45 | 7.5 |
| Buffers per line | 35 median, each 80 ms | not recorded |
| Buffers that arrived late | 0 | not recorded |

First audible follows phase 0's rule, the first sample above 0.003, placed at the later of its arrival time and the first buffer's arrival plus its position in the audio. The filter adds no timing cost that shows against the 0.169 s figure. Per-pass medians of first audible were 0.160, 0.169 and 0.166 s. Phonon's load took 5.09 s on the first launch after install, 4.59 s after the second install, and 0.19 s and 0.17 s on the launches between.

Recordings are in `recordings/phonon-package/line-01.wav` to `line-10.wav` (24 kHz mono 16-bit, pass 1).

**Is it the same sound as the owner's pick?** Yes in character, no as audio samples. **measured** on the Mac from the WAV files, against `phone/recordings/phonon/deess/`.

- **Spectral balance.** The long-term spectrum of all ten lines, level-matched, differs by at most 1.3 dB in any 1 kHz band and 0.66 dB rms across 0 to 12 kHz. The share of energy above 6 kHz is 0.55 dB lower in the package files than in the de-essed files (mean over ten lines). Against phase 0's unfiltered streaming files, the package files are 2.97 dB lower above 6 kHz and the de-essed files 2.42 dB lower. That is the filter doing the same job in both.
- **Sample correlation** after level matching and best lag is only 0.12 to 0.25. The package's `synthesize(sentence:)` takes no seed, so every take is a new random realization of the same sentence. Durations differ by up to 0.32 s per line (for example line 1 is 2.64 s against 2.48 s). Phase 0 fixed its seeds. A direct waveform match was never possible. The ten lines should be judged by ear as new takes of the same voice.
- **Level.** The de-essed files peak at 1.00 and sit at about minus 16.8 dBFS rms. The package files peak at 0.33 to 0.57 and sit at about minus 25.4 dBFS rms, 8.6 dB lower. FluidAudio's `AudioWAV.data` peak-scales by default (**read**, `AudioConverter.swift` lines 630 to 640 in the phase 0 FluidAudio copy). Phase 0's raw streaming files were at about minus 25.1 dBFS, the same as the package.

## 6. All three on the GPU at once

**Overlap costs 0 to 23 percent per operation and Phonon stays well ahead of real time.** **measured**, run `voice-voice1`, 5 repetitions per scenario. Each repetition ran the alone baseline and the overlap back to back so both share the same thermal state (0 to 1 throughout). Sentence "Sure, I will look into that for you." (about 2.5 s of audio). Recognition used the first 5 s or 10 s of the R1 stream, draft means an LFM2.5 draft of at most 24 tokens.

| Scenario | Operation | Alone (s) | Overlapped (s) | Change |
|---|---|---:|---:|---:|
| A. 5 s partial and Phonon start together | Recognition | 0.399 | 0.430 | +8% |
| | Phonon total | 0.342 | 0.377 | +10% |
| | Phonon first audible | 0.147 | 0.141 | none |
| B. 10 s partial running, Phonon starts 0.15 s later | Recognition | 0.791 | 0.785 | none |
| | Phonon total | 0.343 | 0.388 | +13% |
| | Phonon first audible | 0.155 | 0.142 | none |
| C. Draft and Phonon start together | Draft | 0.205 | 0.252 | +23% |
| | Phonon total | 0.338 | 0.368 | +9% |
| | Phonon first audible | 0.148 | 0.146 | none |
| D. Recognition, draft and Phonon all requested together | Recognition | 0.397 | 0.427 | +8% |
| | Draft, measured from the common start | 0.212 alone | 0.633 | queued |
| | Phonon total | 0.341 | 0.369 | +8% |
| | Phonon first audible | 0.154 | 0.140 | none |

In D the draft finished at 0.633 s because the scheduler holds it until the recognition ends (0.427 s plus about 0.2 s of its own work). That is the designed serialization, not contention.

**Real time.** For every Phonon stream in this item I took each buffer's arrival time against the time it would be needed if playback began at the first buffer. The maximum lateness was 0.000 s and no buffer arrived late in any of the 40 streams (20 alone, 20 overlapped). Under overlap Phonon made about 6.4 to 6.7 s of audio per second of wall time and its first buffer arrived at about 0.045 s, so the audio stays well ahead. Playback through an audio engine was not part of this test. These streams were consumed and timed in the app and no sound was played.

## 7. Memory

**With all three models loaded the footprint is about 540 MB and the peak across the whole run is 557 MB.** **measured**, run `voice-voice1`, footprint is `phys_footprint` from `task_vm_info`, sampled every 20 ms. Peak columns show the highest sample since the previous stage, so they can sit a little under a snapshot taken at the end of the stage. Weights are memory mapped, which is why the footprint is small next to the 1.2 GB of model files.

| Stage | `phys_footprint` (MB) | Peak since previous stage (MB) | `ru_maxrss` (MB) | `os_proc_available_memory` (MB) |
|---|---:|---:|---:|---:|
| Process start, nothing loaded | 9 | 9 | 34 | 3,212 |
| Rumble loaded | 437 | 413 | 767 | 2,785 |
| LFM2.5 loaded | 507 | 507 | 1,443 | 2,715 |
| Phonon loaded | 501 | 545 | 1,574 | 2,720 |
| All warm (first recognition, draft and sentence done) | 541 | 557 | 1,574 | 2,680 |
| After the 30 Phonon lines | 514 | 522 | 1,574 | 2,707 |
| After the overlap scenarios | 519 | 531 | 1,577 | 2,702 |

The overall peak was **557 MB** and the lowest `os_proc_available_memory` sample was **2,664 MB**. `ru_maxrss` ended at 1,577 MB and includes the mapped model pages, so it is not the figure to budget against. Phase 0 (R12) measured Phonon alone on top of Rumble and LFM2.5 at 490 to 496 MB after load, 523 to 528 MB peak, `ru_maxrss` 1,572 to 1,636 MB and 2,694 to 2,698 MB lowest available. The package build is about 30 MB higher at its peak, which I did not trace to a cause. Core ML's work in a system process is not in the app footprint, a limit R12 states as well. CPU-only memory is in item 3.

## 8. Cold start

**A first launch after an install takes 20.0 s to reach all three models ready. Later launches take 0.5 to 0.9 s.** **measured**, `--mode cold`, the three models loaded one after another in the order Rumble, LFM2.5, Phonon. Launch time is the kernel's process start time. The app's own init began 0.07 to 0.19 s after it.

| Launch | Rumble load | LFM2.5 load | Phonon load | Process launch to all ready | Process launch to first reply spoken |
|---|---:|---:|---:|---:|---:|
| First after install (build 2) | 13.67 s | 1.10 s | 5.09 s | **20.05 s** | 20.90 s |
| Second | 0.48 s | 0.10 s | 0.19 s | **0.93 s** | 1.81 s |
| Third | 0.19 s | 0.09 s | 0.17 s | **0.51 s** | 1.34 s |
| First after the second install (the item 5 to 7 run) | 0.86 s | 1.04 s | 4.59 s | not timed from launch | not timed |

"First reply spoken" is the time to finish one transcription of the fruit clip, one draft and one spoken sentence, about 0.85 s of work after ready, so the first audible sound came earlier than the figure.

Phonon's roughly 5 s load recurred on both first launches after an install and vanished on the launches in between, so it looks tied to the installed binary (likely Core ML preparing the model for the new bundle, which I did not confirm). Rumble's 13.7 s did not recur after the second install (0.86 s), so it is more likely reading about a gigabyte of weights from storage that had not been touched for hours. R12 saw 12.9 s for Rumble on its first launch after install. No file of mine or the lab's was deleted to force either case.

## What is still unknown

- **Backgrounding.** Whether a loaded GPU model throws `decodeFailed` after the app is backgrounded, and whether unloading and reloading CPU-only recovers cleanly and how long that takes. Needs a person or a UI driver to leave the app.
- **Audible playback.** Nothing was played. Behavior of the real audio path next to GPU work (underruns in the audio engine, route changes) is untested.
- **Sustained load and heat.** The phone moved to serious thermal state within about a minute of back-to-back decoding. How long a real conversation takes to get there, and what the ladder and Phonon cost at that state, was only seen in the hot ladder run (up to 27 percent slower).
- **Where Phonon runs.** Not examined. The low contention suggests it mostly avoids the GPU path llama.cpp uses, but that is a guess.
- **Cause of the 5 s Phonon load and the 13.7 s Rumble load** on first launches (see item 8), and whether an App Store install behaves the same.
- **Real microphone input and end-of-turn timing**, as in R1.
- **The 30 MB higher peak** than phase 0 and whether per-frame filter state or the larger package contexts cause it.
- **Reviewing the voice.** The ten package takes are new random takes. Whether the owner still prefers them at the lower level, or normalized, needs a listen.

## Phone state at the end

The frozen IPA was reinstalled over the work build. The app is back at version 1.0 (1) with bundle id `org.kentuckyopenscience.edgevoicelab`. `Documents/Models` lists 416 entries (the folder plus 415 below it), the same count and the same names as before the work, and `BenchVoices` and `BenchResults` are unchanged. The app's `Documents/Phase1Results` folder holds this work's raw output and was left in place, as it is mine and was not asked to be removed. No file under the lab's or any Astral app's container was deleted, no App ID, profile, certificate or registration was created, and nothing was signed in to.

## Files

- Write-up: `device-check.md`
- Host app source and project: `app/` (`Sources/Runner.swift`, `Sources/Util.swift`, `project.yml`)
- Build and device scripts: `rebuild.sh`, `run.sh`, `pull.sh`, `ent.plist`, `compare.py`, `show.py`
- Raw results: `raw/pull/`, `raw/deess-compare.json`, `raw/docs-before.txt`, `raw/docs-after.txt`
- Recordings: `recordings/phonon-package/line-01.wav` to `line-10.wav`
