# R11: how often Rumble returns words for audio with no speech

## Answer

Word-confirmed interruption (C5) is workable with Rumble, but not with the stock llama.cpp build on short or padded audio. The stock build returned words for 39 of 169 non-speech clips (23 percent) in English. Most of those are the zero-padding fault from R2, and the phrases are stock phrases such as "I'm sorry." and "I'm gonna go." With the third-party patch the figure is 2 of 169 (1.2 percent), and with the app padding clips to `16000*k - 160` samples it is 4 of 169 (2.4 percent). The two fixes are not statistically distinguishable at this sample size.

Ship the stock build with app-side padding. It removes the fault without carrying a patch whose licence is unknown (R2). Carry the patch only if the transcription quality gain in R2 justifies it on its own. For C5 the extra margin is under two points and inside the noise.

The app needs three filters and should not use three others.

- Needed: a 0.4 s minimum clip length (already in the spec), an energy and voiced-frame check before recognition, and the backchannel list that the spec already has.
- Not worth it: a minimum word or character count (it rejects "stop" and "wait"), a hallucination phrase list (the phrases change with the build and the language), and a two-partial agreement rule (it removed one of four false words and cost 0.5 s plus one real word).

What this does not settle is the case that matters most on a phone: Astral's own voice leaking back through echo cancellation. That is speech, and Rumble will transcribe it. This test cannot see it. See "What remains unknown".

Residual false words after padding, with the 0.4 s rule and the voiced-frame gate, were 2 of 169 in English. Both are sneezes (the vocal "ah-choo" is voiced and transcribes as "Yes." or "And this"). The gate removed the two digital-silence cases. With the patch the residual is 1 of 169, also a sneeze.

## Method

All figures are **measured** unless tagged **assumed**.

- Machine: Apple M5, macOS 27.2.1. Runtime: the other agent's `voice-bench-109/runtime-mac` builds of llama.cpp `8af68d486a9473820d24d00af8f33334ca80124d` (b12549), `build-stock` and `build-patched` (patch SHA-256 `b8f589e5...80c`). Not rebuilt and not modified.
- Model: Rumble-2-Flash-AD-Q5_K_M with the Q8_0 mmproj, from the frozen lab, read only.
- Command, one process per clip, run one at a time: `llama-mtmd-cli -m <rumble> --mmproj <mmproj> --jinja --audio <wav> -p "lang:<xx>" --temp 0 -n 96 -c 2048 -ngl 99 --no-warmup`. Metal, greedy, 16 kHz mono WAV.
- Three recognizers: `stock`, `patched`, and `stock_pad` (stock binary, clip padded in a script with zeros to `16000*k - 160` samples, k the smallest whole number of seconds that fits).
- Languages: `en`, `es` and `ja` for every non-speech clip. Speech clips use their own language.
- Test set, 236 clips, 1,721 recognitions:
  - Human non-speech from ESC-50, 38 clips: coughing 10, sneezing 8, breathing 8, laughing 8, snoring 4. ESC-50 has no throat-clearing or sigh class, so those two are not covered.
  - Room and device from ESC-50, 38 clips: keyboard typing 8, door knock 6, mouse click 6, footsteps 6, clock tick 4, washing machine 4, vacuum cleaner 4. The last two stand in for a fan or hum.
  - Synthetic, 17 clips: white, pink and brown noise at 60, 45, 30 and 18 dB below full scale, 60 Hz hum at two levels, digital silence at 0.5, 1 and 2 s. Seeded, 2 s long except silence.
  - Short cuts, 76 clips: 0.2, 0.3, 0.5 and 0.8 s cuts starting at the onset of 16 ESC-50 clips (8 classes, two clips each), plus white noise, brown noise and digital silence.
  - Speech controls: 35 lab fixtures (7 English, 4 each in ar, de, es, fr, ja, ko, zh), 21 cuts of 0.3, 0.5 and 0.8 s from the English fixtures starting at speech onset, and 11 short interjections made with the macOS `say` voices (stop, wait, hold on, no, hey, okay, yes, right, mm-hm, and Spanish "para" and "espera"). The fixtures are clean and loud (peak minus 21 to minus 12 dBFS). I did not check how they were made.
- ESC-50 clips were cut to 0.8 to 3 s starting 0.1 s before the sound. The 5 s originals are mostly silence.
- Dataset: ESC-50, github.com/karolpiczak/ESC-50, commit `33c8ce9eb2cf0b1c2f8bcf322eb349b6be34dbb6` (2023-10-13), fetched anonymously over raw.githubusercontent.com. Licence: the dataset is CC BY-NC 3.0, the ESC-10 subset CC BY 3.0, and each clip carries its own Freesound licence (CC0, CC BY or CC BY-NC) in the repository's LICENSE file. The clips were used for measurement only, kept in the work folder, and not copied into this spec folder.
- Classification of each transcript: empty, filler only (punctuation, "ah", "oh", "ha", "um", "mm" and the same in es and ja), or words (anything else).
- Work folder: `voice-bench-109/noise-eval/` with `scripts/` (fetch, build_clips, run, partials, filters, table), `clips/`, `clips-padded/`, and `results/` (`manifest.json`, `raw.jsonl`, `partials.jsonl`, `filters.json`, `summary_table.md`).

## Results by class and recognizer

Clips that returned words, English hint. Three recognizers over the same clips. "Long" is 0.8 to 3 s, "short" is 0.2 to 0.8 s.

| Class | Clips | Stock | Patched | Stock + pad |
|---|---|---|---|---|
| Human non-speech, long | 38 | 12 (32%) | 1 (3%) | 2 (5%) |
| Room and device, long | 38 | 3 (8%) | 0 | 0 |
| Synthetic noise and hum, long | 14 | 4 (29%) | 0 | 0 |
| Digital silence, 1 to 2 s | 3 | 2 | 1 | 2 |
| Short cuts, all noise | 72 | 16 (22%) | 0 | 0 |
| Digital silence, short | 4 | 2 | 0 | 0 |
| **All non-speech** | **169** | **39 (23%)** | **2 (1.2%)** | **4 (2.4%)** |
| 95 percent interval (Wilson) | | 17 to 30% | 0.3 to 4.2% | 0.9 to 5.9% |

Short cuts by length (19 clips each): 0.2 s stock 10, 0.3 s stock 8, 0.5 s and 0.8 s stock 0. Patched and padded gave 0 at every length. The stock fault is a clips-under-0.4-s problem plus a padded-tail problem on longer clips.

By sound, English, long and short pooled (clips: stock, patched, stock+pad): sneeze (16: 7, 1, 2), mouse click (14: 5, 0, 0), keyboard (16: 5, 0, 0), breathing (16: 4, 0, 0), cough (18: 3, 0, 0), footsteps (14: 2, 0, 0), laughter (16: 1, 0, 0), snoring (4: 1, 0, 0), clock tick (4: 1, 0, 0), pink noise (4: 2, 0, 0), white noise (8: 1, 0, 0), brown noise (8: 3, 0, 0), digital silence (7: 4, 1, 2). Door knock (14), hum and machine hum (10) gave no words in any recognizer.

Language hint, all non-speech clips:

| Hint | Stock | Patched | Stock + pad |
|---|---|---|---|
| en | 39 (23%) | 2 (1.2%) | 4 (2.4%) |
| es | 31 (18%) | 2 (1.2%) | 4 (2.4%) |
| ja | 1 (0.6%) | 0 | 0 |

The hint changes the phrases, not the pattern. Japanese almost never produced text for non-speech. Spanish behaves like English. The stock `es` rate on short cuts is the same as `en` (16 of 72).

Positive controls: every speech fixture (35 of 35) and every short interjection returned words in all three recognizers. "Stop.", "Wait.", "Hold on.", "No.", "Hey.", "Para." and "Espera." came back correct. The 0.3 to 0.8 s cuts of real speech returned words too, often a clipped word ("Count.", "Stop!", "What color?"), so a short cut of speech looks like a short false word, which is why length alone cannot separate them. "Mm-hm" from the synthetic voice came back as "M.M.H.M.", so the backchannel list must be tolerant of spelled-out forms.

## Hallucinated phrases

Stock, English: "I'm sorry." 22 clips, "I'm gonna go." 5, "I'm not sure." 5, then once each "Yes.", "I'm not gonna lie.", "And this, and this.", "I'm going to go to the next one.", "I'm going to go back to the other one.", "I'm not.", "I'm gonna". Of the 22 "I'm sorry." results, 12 were on clips under 0.4 s.

Patched, English: "And this" (one sneeze) and "I'm not sure." (2 s of digital silence).

Stock + pad, English: "Yes." and "And this, and this." (two sneezes), "I'm going to go back to the other one." and "I'm going to go back to the first one." (digital silence).

Stock, Spanish: "¿Qué es eso?" 15, "¿Qué es lo que pasa?" 5, "¡Ah!" 4, "Ahí está." 3, "Ahora" 2, then single "¿Por qué no?", "¡Eso!", "¡Andi! ¡Andi!", "Sí, sí.". Patched Spanish: "¡Ja!", "Eso.", "¡Andi!", "¡Ah!". Stock + pad Spanish: "¡Ah!" twice, "Eso.", "¡Andi! ¡Andi!", "Pero no es así.", "Pero no es que se vaya a la calle."

Stock, Japanese: "おはようございます。" and "うん、うん、うん。" (the second counted as filler).

Noise often produces filler-like output ("¡Ah!", "¡Ja!"), which the spec's backchannel rule already ignores. The apologies and "I'm gonna go" lines are the real danger, and they come almost only from the stock build.

## Filters

Each filter is applied to the confirmed-interruption decision. "False kept" is how many of the false-word clips still count as an interruption. Controls are the 35 fixtures (S), the 7 stopping words (I), and the 21 cuts of real speech (C). Figures are English hint, **measured** on this set.

| Filter | Stock: false kept of 39 | Patched: of 2 | Pad: of 4 | Real speech kept (S / I / C) |
|---|---|---|---|---|
| None | 39 | 2 | 4 | 35 / 7 / 21 |
| Clip at least 0.4 s | 21 | 2 | 4 | 35 / 7 / 14 |
| Clip at least 0.8 s | 21 | 2 | 4 | 35 / 3 / 7 |
| At least 2 words | 38 | 2 | 3 | 35 / 1 / 12 to 18 |
| At least 4 characters | 38 | 2 | 3 | 35 / 5 / 21 |
| A priori phrase list (thank you, you, bye, okay, yeah, I'm sorry, yes, oh) | 38 | 2 | 3 | 35 / 7 / 21 |
| Phrase list built from these results | 0 | 0 | 0 | 35 / 7 / 21 |
| At least 5 voiced frames (50 ms) | 20 | 1 | 2 | 35 / 7 / 21 |
| At least 12 voiced frames | 13 | 1 | 2 | 35 / 7 / 21 |
| At least 20 voiced frames | 8 | 1 | 1 | 35 / 6 / 19 |
| Peak above minus 45 dBFS | 31 | 1 | 2 | 35 / 7 / 21 |
| 0.4 s and 5 voiced frames | 14 | 1 | 2 | 35 / 7 / 14 |
| Same words in two consecutive partials (0.5 s steps) | not run | 1 | 3 | 35 / 6 / not measured |

Reading the table:

- **Minimum length 0.4 s** is the right cheap rule for the stock build. It removes 18 of 39. After padding or patching it removes nothing here, because those two never produced words on short clips. It rejects 7 of 21 cuts of real speech, which the spec already accepts.
- **Voiced-frame check** is the filter that works. It is the only one that removes false words without touching real speech here. I used 25 ms frames every 10 ms, normalized autocorrelation above 0.5 between 60 and 400 Hz, and a frame level within 30 dB of the clip's loudest frame and above minus 55 dBFS. Five voiced frames is enough. It removes the silence cases and quiet noise. It does not remove sneezes, which are voiced.
- **Minimum words or characters** costs the stopping words. One word is all "stop" and "wait" are. Not recommended, unless a stop-word list bypasses it.
- **Hallucination list**: an a priori list removed only 1 of 4 padded false words. The list built from these results scores zero by construction, so it proves nothing. The phrases differ by build and language, so a list would need constant upkeep.
- **Two consecutive partials** removed one of two patched false words (the silence one) and one of four padded ones. A sneeze that persists ("And this" for 1.5 s) passes it. It cost one real word: for "espera" the first partial said "Esther.", so agreement failed. It also adds about 0.5 s before an interruption confirms. The row for short cuts is not measured, because they have one partial at most.
- **Confidence**: not checked. `llama-mtmd-cli` exposes no token probability option in this build (its help lists none), and the runtime folder is not to be modified. A host built on the libraries could read logits, which was out of scope.

## What remains unknown

- **Echo residual.** On a phone the overlapping audio contains Astral's own voice after echo cancellation. That is real speech, and Rumble will transcribe it. This test used clean file input with no playback. It is the main risk to C5 and needs the phone pass. A check that the words are not Astral's own current sentence is the likely code-side answer.
- **Phone microphone.** All audio is 16 kHz files on a Mac. No microphone, no gain control, no noise suppression, no iPhone, and Metal on an M5 rather than an A15.
- **Sample size.** 169 non-speech clips and 2 to 4 false words. The intervals above are wide. A rate of 1 to 5 percent cannot be told from 0.3 to 4 percent here.
- **Missing sounds.** No throat clearing and no sigh (not in ESC-50). Coughs and sneezes are from one dataset with a few recordings per class, so the sneeze result rests on 8 clips.
- **Real overlapping speech** from other people in the room (TV, a second talker) was not tested. Both are speech and will return words.
- **Positive controls are clean.** The fixtures and `say` voices are loud and clear. Quiet real "stop" at conversational volume may fail a voiced-frame or level gate set from these. Real stopping words are probably (**assumed**) 0.3 to 0.5 s long, shorter than the 0.67 to 0.98 s of these clips (which include a 0.15 s lead-in), so the 0.4 s rule needs a live check against real "stop" and "wait".
- **Padding in the app** was tested by padding in a script, not in the Swift host. Its tail behavior is the same arithmetic as R2.
