# R9: licence files read for the Rumble voice stack

Checked 2026-11-20. Spec 109 task T009. I am not a lawyer and neither is the reader. This reports what the licence files say. Anything marked "needs a legal reading" is not decided here.

## Answer

Most components pass. FluidAudio fails as published. Two Kokoro pieces are not cleared. One pack manifest needs a different pin.

- **Pass:** LFM2.5 1.2B Instruct (while Astral's revenue stays under $10 million), Rumble 2 Flash with its Qwen3-ASR base, llama.cpp with its vendored audio code, Phonon 31m English, Kokoro-82M weights and the Kokoro Core ML models, and NemoTextProcessing.
- **Pass with a condition:** Pocket TTS small, with attribution and a voice allow-list. `cosette` and `jean` are non-commercial and must be blocked.
- **Fail as published:** FluidAudio v0.17.7 and v0.18.4. Both bundle GPL-3.0-or-later data in the library itself. The text says so in FluidAudio's own licence file. "No GPL dependencies" is true of code and false of data.
- **Not cleared:** the Kokoro English front end data (one provenance gap, the Misaki dictionary) and the Kokoro Spanish and French lexicons (built from eSpeak NG output). Spec 109 does not need the Spanish and French lexicons for English.
- **Fix the pin:** the pack's v1.1 manifest still lists the Piper archive. Use a manifest without it.

The decisive finding is FluidAudio. Every TTS path in the shortlist runs through it. Using it as published puts a GPL-3.0-or-later lexicon in the app bundle. The fix is to exclude two files or fork the package, then prove the app still works.

All licence files are saved under `licences/<component>/` beside this file. Each folder has a `SOURCE.txt` with the URL and revision.

## Table

Tags: **read** means I opened the file at the revision shown. **assumed** means I did not.

| Component | Revision | Licence | GPL exposure | Field-of-use restriction | Duties for the app | Verdict |
|---|---|---|---|---|---|---|
| LFM2.5 1.2B Instruct (source `LiquidAI/LFM2.5-1.2B-Instruct`, GGUF `LiquidAI/LFM2.5-1.2B-Instruct-GGUF`) | source `b9d816ab`, GGUF `8ed28802` | LFM Open License v1.0 (**read**, `LICENSE`) | None | None. I read all 11 sections. No clause limits medical or clinical use. The limit is revenue (below) | Give recipients a copy of the licence. Mark modified files. Acknowledgments entry naming Liquid AI and the licence. Keep the licence copy in the pack | **Pass** while revenue stays under $10M. Needs a legal reading on affiliates and on recipients (see item 1 below) |
| Rumble 2 Flash (`Reson8/Rumble-2-Flash` and `AtomicChat/Rumble-2-Flash-GGUF`) | `95bd5f91`, `d036f2af` | Apache 2.0 (**read**, `LICENSE` in both, byte-identical) | None | None | Bundle or link the Apache 2.0 text. Acknowledgments entry naming Reson8, AtomicChat and the Qwen base | **Pass** |
| Qwen3-ASR-0.6B (base model) | HF `5eb14417`, GitHub `7c6daf77` | Apache 2.0 (**read**, GitHub `LICENSE`. The HF repository has no LICENSE file and only the card tag) | None | None | Acknowledgments entry naming Qwen | **Pass** |
| Pack repository `Kentucky-Open-Science/Rumble-2-Flash-Astral` | `573fa2b4` (v1.1) | `other: component-licenses` (**read**). Carries the Apache and LFM texts under `licenses/` | None in the three GGUF files. The v1.1 manifest also lists a Piper archive (see item 6) | None | Keep `licenses/` with the pack | **Pass** for the three GGUF files. **Fail** for the v1.1 manifest as written |
| llama.cpp | `8af68d48` (pilot pin, 2026-11-19) | MIT (**read**, `LICENSE`) | None found. A search of the core, `tools/mtmd`, `vendor`, `common` and the CPU and Metal backends for GPL, LGPL and AGPL text and SPDX tags found none. Only SPDX tag found was MIT (52 hits). I did not search the `ui` or other GPU backend folders | None | Acknowledgments entry for llama.cpp and the vendored items below | **Pass** |
| llama.cpp vendored audio and helpers | same | miniaudio v0.11.26 is "public domain or MIT No Attribution" (**read**). stb_image is MIT or public domain (**read**). subprocess.h is Unlicense (**read**). nlohmann json and cpp-httplib are MIT. xxhash is BSD-2. sha256 and rotate-bits (**read**, headers) | None | None | Notices for each in the acknowledgments screen. Only MIT and BSD ones strictly require it | **Pass** |
| FluidAudio | tag `v0.17.7` (`503b4bd1`). There is no tag `0.17.7` | Apache 2.0 (**read**, `LICENSE`) | **Yes, data.** The library target bundles `luxtts_en_us_lexicon.tsv.zz` and `luxtts_en_us_g2p_aux.json` through `resources: [.process("TTS/LuxTts/G2p/Resources")]` (**read**, `Package.swift`). FluidAudio's later licence file says these are derived from espeak-ng, "GPL-3.0-or-later" (**read**, v0.18.4). v0.17.7 has no licence note for them | None | At minimum the full third-party list (see below) | **Fail** |
| FluidAudio | tag `v0.18.4` (`e0c6103b`, 2026-11-19) | Apache 2.0 | **Yes, data, same two files.** Now in a separate `LuxTtsG2pResources` target. The `TTS` trait links it. FluidAudio's note says: "Apps that don't use LuxTTS can leave both files out of their bundle." | None | Same. Also no licence entry for the ported `ptts` normalizer, the jieba and pypinyin ports (item 2) | **Fail as published.** Pass only if both files are excluded and the app is proven to run without them |
| NemoTextProcessing (binary linked by FluidAudio on iOS, from `FluidInference/text-processing-rs`) | v0.3.4 (`2e085647`) pinned by FluidAudio v0.18.4. v0.17.7 pins v0.3.1 (`46ebddce`) | Apache 2.0 (**read**, `LICENSE`, `NOTICE`, `THIRD-PARTY-LICENSES.md`, identical at both tags). Built from NVIDIA NeMo Text Processing (Apache 2.0) with rustfst and flate2 (MIT or Apache 2.0) | None found in the direct dependencies. `Cargo.lock` was not read, so transitive crates are **assumed** permissive | None | Apache 2.0 notices and the NVIDIA copyright notice | **Pass** on what I read |
| Kokoro-82M weights (`hexgrad/Kokoro-82M`) | `f3ff3571` | Apache 2.0 by the card tag (**read**). The repository has no LICENSE file | None in the weights | None | Acknowledgments entry. The card also lists CC BY training audio (Koniwa `tnc`, SIWIS) | **Pass** |
| Kokoro Core ML (`FluidInference/kokoro-82m-coreml`) | `51c07202` | Apache 2.0 (**read**, `ANE/LICENSE` and card). ANE-v3 adds a NOTICE for `mattmireles/kokoro-coreml` (Apache 2.0) | None in the Core ML models | None | Keep the NOTICE and `mattmireles-LICENSE` if ANE-v3 artifacts are redistributed | **Pass** |
| Kokoro English front end (`us_lexicon_cache.json`, `G2PEncoder` and `G2PDecoder`) | same | Misaki repository is Apache 2.0 and the BART upstream `PeterReid/graphemes_to_phonemes_en_us` is tagged Apache 2.0 (**read**). Neither says where the dictionary entries came from. The BART model was trained on the Misaki datasets (**read**, its script comment) | None found. Not proven absent | None | Notices for Misaki and the BART model | **Not cleared.** One provenance gap, the Misaki dictionary (item 4) |
| Kokoro Spanish and French lexicons (`es_lexicon_cache.json`, `fr_lexicon_cache.json`) | same | ipa-dict MIT and CharsiuG2P MIT (**read**). Pronunciations "were generated with espeak-ng" | **Data derived from eSpeak NG output.** FluidAudio says "No espeak-ng code is distributed" and says nothing about the licence of the output | None | Unknown | **Not cleared.** Needs a legal reading. Not fetched for English |
| Phonon 31m English (`gradium/phonon-31m-en`, Core ML in `FluidInference/pocket-tts-coreml` `phonon/phonon-31m-en/`) | Gradium `8f7a37c9`, Core ML `c2e998e2` | Apache 2.0 (**read**, both `LICENSE` files are byte-identical). Card: "The weights, tokenizer and voices in this repository are released under Apache 2.0" | None. No phonemizer. Text goes through a rule-based normalizer and a SentencePiece tokenizer | None in the licence. The card's "Responsible use" paragraph is advice (item 3) | Acknowledgments entry for Gradium | **Pass.** Needs FluidAudio v0.18.0 or later (item 2) |
| Phonon 40m models | n/a | CC BY-NC 4.0 per the Gradium card (**read**) | None | Non-commercial | n/a | **Fail.** Do not use |
| Pocket TTS small 18M, 7M, 2M (Kyutai, Core ML in `FluidInference/pocket-tts-coreml` `v4/`) | Kyutai `db04f0e6`, Core ML `c2e998e2` | CC-BY-4.0 (**read**, card front matter and the Core ML card) | None. No phonemizer. Raw text into a SentencePiece tokenizer | A prohibited-use list (item 5). Not a medical ban | Attribution. See wording below. Ship only the English voices from CC BY or CC0 sources (item 5) | **Pass with a voice allow-list.** `cosette` and `jean` come from non-commercial datasets and must not ship. Needs a legal reading on the gate (item 5) |

### Wording for the acknowledgments screen

- **Pocket TTS.** The Core ML card says: "CC-BY-4.0, inherited from https://huggingface.co/kyutai/pocket-tts. Attribution to Kyutai is required." CC BY 4.0 section 3(a) (**read**, `legalcode.txt`) asks the app to keep the creator name, a licence notice, a link to the material, and an indication that the material was modified. Converting to Core ML is a modification. Suggested entry: "Pocket TTS by Kyutai, CC BY 4.0, converted to Core ML by FluidInference and modified for this app, https://huggingface.co/kyutai/pocket-tts".
- **LFM Open License.** Entry for Liquid AI with the licence text bundled (section 4(a)).
- **Others.** Entries for Reson8 and AtomicChat (Rumble), Qwen, llama.cpp with miniaudio, stb and nlohmann, Gradium (Phonon), hexgrad (Kokoro) and FluidInference (FluidAudio and Core ML conversions).
- **Kokoro.** The card lists Koniwa `tnc` (CC BY 3.0) and SIWIS (CC BY 4.0) as training audio. The Apache weights licence does not carry that duty on its face. Naming them is cheap and safe.

### FluidAudio third-party files (read, v0.17.7)

- `ThirdPartyLicenses/KokoroANE-v3/`: Apache 2.0 NOTICE and two Apache texts.
- `KokoroAneSpanishFrenchG2P-LICENSE.md`: ipa-dict MIT, CharsiuG2P MIT.
- `JapaneseG2P-LICENSE.md`: BSD, Apache, MIT. unidic is offered under "GPL, LGPL or BSD" and FluidAudio takes the BSD option.
- `NemoTextProcessing-LICENSE.md`: Apache 2.0 and MIT or Apache Rust crates. Linked as a binary xcframework.
- `fastcluster-LICENSE.md`: BSD-2. `vbx-LICENSE.md`: Apache 2.0.
- `Package.swift` lists `dependencies: []`. There are no remote Swift package dependencies. The one remote item is the `NemoTextProcessing` binary target.

## Surprises and disagreements with research.md

### 1. LFM Open License: the revenue test reaches further than O12 reads

research.md section 7 and decision O12 are right on the facts. The text confirms the threshold, the non-profit exemption, no field-of-use limit and a redistribution duty. Four wordings are worth a legal reading.

- "Threshold" shall mean annual revenue of 10 million United States dollars ($10,000,000) or more.
- Section 5(a): "conditioned upon You or Your Legal Entity not exceeding the Threshold." The definition says "or more" and 5(a) says "not exceeding". They disagree at exactly $10M.
- "Legal Entity" is "the union of the acting entity and all other entities that control, are controlled by, or are under common control with that entity", with control meaning 50% or more of shares. O12 speaks of "the entity that distributes Astral". The licence counts affiliates too.
- Section 5(b): "Any Commercial Use of the Work or a Derivative Work by a Legal Entity that exceeds the Threshold is not licensed under this Agreement." Section 4(a) says redistributors must give recipients a copy of the licence. "You" is anyone exercising the permissions. A reading where each recipient, such as a hospital system, holds its own licence and must itself stay under $10M is open. "Commercial Use" is "any use of the Work for direct or indirect commercial advantage or monetary compensation." The text does not settle whether a large customer running Astral on its devices is outside the grant. This needs a legal reading before sales to institutions with revenue above $10M.

Other terms read: section 11 terminates the licence on any breach. Section 3 ends the patent licence if you sue over the Work. Section 7 grants no trademark rights. The three copies of the licence in the Liquid repositories and the pack have the same body. The pack and GGUF copies start with the heading "LICENSE TEXT" and drop the title line. The source repository copy starts "LFM Open License v1.0".

research.md also says the shipping LFM3 manifest names the same licence. I did not check that.

### 2. FluidAudio: "no GPL dependencies" is true of code only, and Phonon and Pocket small are not in v0.17.7

- **GPL data inside the library.** research.md section 9.1 says FluidAudio's documentation "states 'no GPL dependencies' for these paths and lists none in its third-party licence folder." Both facts are right and both mislead. At `v0.17.7` the folder lists none because the LuxTTS note was added later, at `v0.18.0`. The files were already in the package. The v0.18.4 note reads: "These two data files are **not** covered by FluidAudio's Apache-2.0 license alone." It lists the lexicon pronunciations as "Output of espeak-ng" under "GPL-3.0-or-later", and the aux tables as parsed from espeak-ng's `dictsource/en_list` under the same licence. It also lists FrequencyWords word keys under CC-BY-SA-4.0. Whether data generated by a GPL program is itself GPL is a legal question. FluidAudio's own file treats it as GPL. For this spec's rule ("any GPL-family code or data ... shipped with the app") I treat it as a fail.
- **Where it links.** v0.17.7 `Package.swift` declares the resources on the `FluidAudio` target itself, so any app that links FluidAudio ships them. In v0.18.4 the `TTS` trait pulls in `LuxTtsG2pResources`. The Kokoro, Pocket and Phonon paths all need the `TTS` trait. I did not test building with the files removed or whether the code tolerates their absence.
- **v0.18.4 read in full.** I listed `ThirdPartyLicenses/` at v0.18.4. It has the same files as v0.17.7 plus the LuxTTS note. I searched `Package.swift`, `Package@swift-6.2.swift`, the podspec, `Sources`, the README and `Documentation` for GPL, LGPL, AGPL and "General Public". The only substantive hits are documentation that says eSpeak NG is GPL-3.0 (the Paradee and Inflect docs, and a source comment in `ParadeeG2P.swift`) and the "GPL dependencies: None" table. There are no remote Swift package dependencies (`dependencies: []`). `Documentation/TTS/Results/CMUDict-LICENSE.txt` is a CMUdict licence used for benchmarks and is not in `Sources`. The Paradee doc adds that Kokoro's Hindi, French and Spanish training phonemes came from eSpeak NG.
- **Ported code with no licence entry.** `PhononTextNormalizer.swift` calls itself "A line-for-line port of `normalize_text(input, Lang::En, Rules::DEFAULT)` from the `ptts` crate". Gradium's card says the `ptts` runtime is MIT OR Apache-2.0. The Mandarin code ports jieba's HMM tables and pypinyin's spelling rules. None of the three has a file in `ThirdPartyLicenses/`. Their upstream licences are **assumed** MIT or Apache 2.0 and I did not read them. Both MIT and Apache require the notice to travel with copies.
- **Tag name.** The tag is `v0.17.7`. There is no `0.17.7`. Current is `v0.18.4`.
- **Phonon and Pocket small need v0.18.0 or later.** research.md says all three candidates run through 0.17.7. At v0.17.7 the only "Phonon" is Fermion Research's speech recognition model. The Pocket small models (18M, 7M, 2M) and Gradium's Phonon 31m TTS first appear at `v0.18.0` (`PocketTtsSmallManager`, packs under `v4/` and `phonon/`). Kokoro ANE v3 is in v0.17.6 and later.

Needs a decision: either fork FluidAudio and remove the two files, or exclude them in a build step and test, or drop FluidAudio and run Core ML directly.

### 3. Phonon: the licence is clean, and the text front end is now read

research.md section 9.1 marked the text front end "Not read". Now read. The Core ML card says "Normalize text first (numbers, currency, emails, URLs, symbols) as Gradium's `ptts` does". FluidAudio's doc calls its `PhononTextNormalizer` "a Swift port of Gradium's English normalizer". The pack ships `tokenizer.model` and `tokenizer.json`. There is no phonemizer and no eSpeak. I did not open `PhononTextNormalizer.swift` itself. The Gradium card says its runtime is "MIT OR Apache-2.0". That is **assumed** for the port.

Only `phonon-31m-en` is Apache. The Gradium card lists every 40m model as CC BY-NC 4.0. The word "Phonon" alone is not a safe search. The Gradium card has a "Responsible use" paragraph: "Do not use Phonon to impersonate a real person, to deceive listeners about who is speaking, or to clone a voice without the speaker's consent." It sits in the card and not in the LICENSE file. I read it as advice and not a licence term. That reading needs a lawyer. It does not mention medical use.

research.md says "About 171 MB" for Phonon. I did not check sizes.

### 4. Kokoro English: one provenance gap

- The English path fetches `us_lexicon_cache.json` and a BART grapheme-to-phoneme Core ML pair (`G2PEncoder`, `G2PDecoder`). The lexicon is Misaki's `us_gold` and `us_silver` (**read**, FluidAudio changelog). The Misaki repository has an Apache 2.0 LICENSE (**read**) and does not say where the dictionary entries came from. FluidAudio's conversion script for the BART model loads `PeterReid/graphemes_to_phonemes_en_us` (**read**, revision `a5631b28`). That repository's card is only the tag "license: apache-2.0" and its training script says it is "designed to load the datasets from `misaki`". So both English front end pieces rest on the Misaki dictionary, whose origin is not stated. I found no GPL text, and I also found no statement that there is none.
- research.md says the English path uses "its own Core ML pronunciation model plus a lexicon. No eSpeak at run time." That holds for the code I read. The Kokoro card says it was trained on "IPA phoneme labels", and FluidAudio's docs say the Spanish and French voices were trained on eSpeak NG output. That is about the training recipe and not shipped data.
- The Kokoro card says the audio was "permissive/non-copyrighted" including "Synthetic audio generated by closed TTS models from large providers". Those providers' terms are not in any file I read. That is a legal question about the weights, not about the licence text.

Spanish and French (research.md says built from eSpeak NG output, "licence file was not opened"). Now opened. `KokoroAneSpanishFrenchG2P-LICENSE.md` says the files are generated from ipa-dict word lists (MIT) plus proper nouns from Misaki, "Their pronunciations were generated with espeak-ng and Misaki's `EspeakG2P` post-processing", and "No espeak-ng code is distributed." It gives MIT texts for ipa-dict and CharsiuG2P and does not say what licence the generated pronunciations carry. FluidAudio treats the same kind of output as GPL-3.0-or-later for LuxTTS. The two files are inconsistent. They are downloaded only for the Spanish and French variants. English does not fetch them.

### 5. Pocket TTS: gate, terms, voices

- **Gate.** `kyutai/pocket-tts` has `gated: auto`. Weights return 401 to an anonymous request. The card is readable through the `/resolve/` URL but not through `/raw/`. The 18M, 7M and 2M weights are in this gated repository under `languages/english_*_2026-10/`. The ungated `kyutai/pocket-tts-without-voice-cloning` has the same card text byte for byte.
- **Terms text (read, `extra_gated_prompt` and the card's "Prohibited use" section).** "Use of our model must comply with all applicable laws and regulations and must not result in, involve, or facilitate any illegal, harmful, deceptive, fraudulent, or unauthorized activity. Prohibited uses include, without limitation, voice impersonation or cloning without explicit and lawful consent". It ends with "We disclaim all liability for any non-compliant use." It does not mention medical use. The licence itself is CC-BY-4.0.
- **Does it bind someone who takes only the Core ML build?** The gate is an access form between the person and Kyutai. A person who downloads from `FluidInference/pocket-tts-coreml` never sees it. The FluidInference card repeats CC-BY-4.0 and the attribution duty. It does not repeat the prohibited-use text. Whether the prohibited-use paragraph still follows the weights, and how it sits with CC BY 4.0's rule against adding restrictions, **needs a legal reading**. The practical point is that Astral has no use that collides with the list, and voice cloning is not wired for the small models.
- **Voices.** Kyutai's `tts-voices` card says each voice has its own licence. It lists Expresso and EARS as "Non-commercial use only", VCTK and CML-TTS as CC BY 4.0, voice donations and voice-zero as CC0, and `alba-mackenna` as CC BY 4.0. The Kyutai card lists the source file of each pack voice (**read**, `pocket-tts/README.md`). I mapped all 26 names in the `v4` pack's `constants_bin`.
  - **Non-commercial, do not ship:** `cosette` (from `expresso/`) and `jean` (from `ears/`). Both are in the FluidInference `v4` English packs.
  - **CC BY 4.0, attribution needed:** `alba` (alba-mackenna) and the VCTK voices `anna`, `azelma`, `charles`, `eponine`, `eve`, `fantine`, `george`, `jane`, `mary`, `michael`, `paul`.
  - **CC0:** `bill_boerst`, `caro_davy`, `peter_yearsley`, `stuart_bell` (voice-zero) and `javert`, `marius` (voice-donations).
  - **Other languages, out of scope:** `estelle` is from `unmute-prod-website`, which the card says is CC0 for most files. `giovanni`, `lola`, `juergen` and `rafael` point to Common Voice style files with no licence stated in the card.
  - Whether a voice embedding derived from a non-commercial recording carries that restriction, and whether the pack as downloaded can be used with an allow-list, are legal readings. The safe course is to refuse any voice not on the allow-list in code.
- **Pocket TTS small are English only** in the pack I read. The 100M packs and cloning are out of scope.

### 6. The pack v1.1 manifest still pulls Piper

The pack at `573fa2b4` has `astral.rumble-voice-pack/v2` with an `archives` entry for `vits-piper-en_US-lessac-medium.tar.bz2` (359 files, 67,230,653 bytes). research.md says spec 109 does not ship Piper. A client that reads the v1.1 manifest and installs everything would fetch it. The README table lists the Piper voice as "with tokens and eSpeak NG data". I did not open the archive. The v1.0 tag still has a manifest, and the README says it "remains available for clients that only read the GGUF file list". Spec 109 should pin a manifest without the Piper entry. The three GGUF files themselves are clean. Their SHA-256 values match the AtomicChat and LiquidAI copies.

### 7. llama.cpp and audio

- research.md section 7 lists llama.cpp as MIT and is right. libmtmd's audio path vendors miniaudio (v0.11.26), stb_image and subprocess.h, plus hashing and JSON helpers. All are permissive. miniaudio offers "ALTERNATIVE 1 - Public Domain" or "ALTERNATIVE 2 - MIT No Attribution".
- **Video.** `tools/mtmd/CMakeLists.txt` sets `MTMD_VIDEO` ON by default and describes it as "requires ffmpeg binary in PATH". It starts the binary as a child process and does not link it. The app never ships ffmpeg. I did not read the pilot's build script. It should turn this off. iOS forbids child processes anyway. I did not test it.
- The sparse checkout covered the core, `tools/mtmd`, `vendor`, `common` and the CPU and Metal backends. The other GPU backends and the web `ui` folder were not searched and do not ship on iOS.

### 8. Smaller points

- `Qwen/Qwen3-ASR-0.6B` on Hugging Face has no LICENSE file. The text is only in the GitHub repository. The GitHub copy is at the default-branch tip and not pinned to the weights revision.
- `hexgrad/Kokoro-82M` and `FluidInference/kokoro-82m-coreml` have no root LICENSE file. The licence is the card tag. FluidInference ships Apache texts in `ANE/` and `ANE-v3/`.
- Rumble's LICENSE is the stock Apache 2.0 text with the appendix and no named copyright holder. Neither Rumble repository has a NOTICE file. Rumble's card does not list its training data.
- The `kyutai/pocket-tts` card at `db04f0e6` describes the 100M model. It does not mention the small models.

## Not opened

- The Piper archive and its `espeak-ng-data`.
- FluidAudio Swift source beyond `Package.swift` and the header comments and grep hits quoted above.
- Misaki's dictionary JSON files and the origin of their entries.
- `Cargo.lock` of `text-processing-rs` (transitive crates).
- Upstream licences of the `ptts`, jieba and pypinyin ports.
- Source datasets of the non-English Pocket voices.
- The LFM3 manifest.
- Terms pages for the closed TTS providers named on the Kokoro card.
- The ipa-dict data licences beyond the MIT text FluidAudio quotes.
