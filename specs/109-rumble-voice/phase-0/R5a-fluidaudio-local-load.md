# R5a. FluidAudio local-folder load

**Status: source and run-time halves done.** Run-time result is at the end.

## Question

Can FluidAudio 0.17.7 load Kokoro-82M, Phonon 31m and Pocket TTS small from a folder the app supplies with no network request? Where does it fetch by itself, and can that be blocked?

## Answer

- **The premise is wrong for two of three candidates.** Tag `v0.17.7` (`503b4bd1bbf7220882de39fe8ae6716aae4132da`) has Kokoro and the 100M Pocket TTS only. Phonon 31m and the Pocket small models (18m, 7m, 2m) first appear in `v0.18.0`. The bench uses `v0.18.4` (`e0c6103bc87a88d25276cad491c86e757f58c865`). The spec must pin 0.18.x, not 0.17.7. **read** (`git grep` on both tags, `Documentation/Models.md:76-78` at v0.18.0, absent at v0.17.7). In 0.17.7, "Phonon" means only an ASR model (`ModelNames.swift:27`).
- **Pocket small and Phonon: yes, cleanly.** `PocketTtsSmallManager.load(from:)` takes a pack folder and never calls the downloader. **read** (`Sources/FluidAudio/TTS/PocketTTS/Small/PocketTtsSmallManager.swift:57-69`). Only `initialize(directory:)` (line 50) calls `PocketTtsSmallDownloader.ensurePack` (line 194), which downloads when the pack is incomplete.
- **Kokoro: yes, but only by pre-seeding a folder layout.** There is no `load(from:)`. `KokoroAneManager(variant:directory:)` plus `initialize()` runs existence checks first and downloads only for missing files. **read** (`KokoroAneResourceDownloader.swift:31-85`, `ensureG2PAssets` around line 383, `KokoroAneManager.swift:88-110`). Required layout under `directory`: `kokoro-82m-coreml/ANE/{7 x *.mlmodelc, vocab.json, af_heart.bin}` and `kokoro/{G2PEncoder.mlmodelc, G2PDecoder.mlmodelc, g2p_vocab.json, us_lexicon_cache.json}`. The G2P folder is `kokoro`, not `kokoro-82m-coreml` (`ModelNames.swift:457-460`, `KokoroAneResourceDownloader.swift:383-392`).
- **The default Kokoro runtime is the legacy 7-stage chain.** The newer `.v3` runtime (`KokoroAneV3Assets.ensure`) also fetches a pinned manifest, lists a base repo tree through the API and compiles `.mlpackage` sources on the device. It is a harder fit for a verified local folder. **read** (`TTS/KokoroAne/V3/KokoroAneV3Assets.swift:185-240, 250-300`). The bench uses legacy.

## Where FluidAudio fetches by itself

| Path | What it does | Blocked by `ModelHub.offlineMode`? |
|---|---|---|
| `ModelHub.download`, `fetchFile`, `fetchWithAuth` | All model downloads | Yes. They throw `networkDisabled` (`ModelHub.swift:38-48, 444, 628, 802`, `HFClient.swift:16`) |
| `FileDownloader`, `HFTreeLister` | Per-request loops | Yes (`FileDownloader.swift:159, 326`, `HFTreeLister.swift:43`) |
| `KokoroAneModelCacheMigration.repairIfNeeded` | Runs before the existence check. Moves a bundle aside and re-downloads when `model.mil` lacks the flexible-shape attribute | Yes, through `ModelHub.download`, but the move would then roll back |
| `KokoroAneResourceDownloader.ensureEnglishLexicon` | Best-effort fetch of `us_lexicon_cache.json`. Also called at synthesis time (`KokoroAneManager.swift:552`) | **No.** It uses `AssetDownloader`, which has no offline check. It fetches only when the file is missing |
| `KokoroAneV3Assets.fetch` | v3 only | Partly |

Public switches: `ModelHub.offlineMode = true` (`ModelHub.swift:31`) and `ModelRegistry.baseURL` (`ModelRegistry.swift:32`), which can point at a closed loopback port to catch any path that ignores the first switch. Both are static globals and must be set before any FluidAudio call.

## Minimum OS

- Pocket small and Phonon: iOS 18 and macOS 15 (`PocketTtsSmallManager.swift:17`, `@available(macOS 15.0, iOS 18.0, ...)`). **read**
- Kokoro legacy: package floor iOS 17 and macOS 14 (`Package.swift:8-9`). `.v3` needs iOS 18 (`KokoroAneModelStore.swift:223-226`). **read**
- Package floor for FluidAudio: iOS 17, macOS 14. **read**

## Kokoro is flagged unsafe on iOS 27

`KokoroAneManager.osAdvisory` (`KokoroAneManager.swift:~122-142`) states that on iOS 27 no Core ML route is known to be safe. The default route has crashed in libBNNS after about an hour of synthesis and the Metal route aborts in MPSGraph within minutes. Both are uncatchable in process (upstream issues #889, #843). The phone runs iOS 27.2.x. **read**, not yet reproduced here.

## Method for the run-time half

Bench app `phone/bench` (Release, iOS 18 target, FluidAudio 0.18.4 as a local package, Swift 6.4.2, Xcode 27.2). Packs fetched anonymously from `FluidInference/pocket-tts-coreml` and `FluidInference/kokoro-82m-coreml` at the commit that `main` resolved to (see `phone/voices/fetch-log-*.json`, every file HTTP 200, no login, no terms). Copied into the app container under `Documents/BenchVoices`. At process start the bench sets `ModelHub.offlineMode = true` and `ModelRegistry.baseURL = http://127.0.0.1:9`, then loads and speaks ten lines. A `gate` mode also loads from an empty folder, once offline and once with only the closed port, to show that a fetch attempt fails loudly.

## Downloads without a terms prompt

**measured** on the Mac, 2026-11-20, anonymous HTTPS with a generic user agent: all 169 Pocket and Phonon files and all 49 Kokoro files returned HTTP 200. The Phonon pack ships a `LICENSE` file. The Pocket 18m pack folder has none. Kyutai's own repository is gated, but the FluidInference conversion is not.

## Run-time result

**measured**, iPhone 13 Pro (iPhone14,2), iOS 27.2.1, Release build, FluidAudio v0.18.4 (`e0c6103b`), bundle id of the lab app, results in `phone/results/pull/`.

- With `ModelHub.offlineMode = true` and `ModelRegistry.baseURL = http://127.0.0.1:9` set before any FluidAudio call, Kokoro (legacy runtime), Phonon 31m and Pocket 18m each loaded from the pre-seeded folder and spoke 30 lines (3 passes of 10) with no error. Every one of those runs raised no network error, and the closed port would have refused any attempt. **No request was observed**, but there was no packet capture, so the claim rests on the two gates, not on a trace.
- `gate` mode, same phone: loading Pocket or Kokoro from an empty folder with `offlineMode` on threw `networkDisabled(operation: "download(...)")` at once. With `offlineMode` off and only the closed port, both threw `NSURLErrorDomain -1004` for `http://127.0.0.1:9/api/models/FluidInference/...`. So a fetch attempt is visible either way, and the lexicon-bypass path was not needed because the lexicon file was present.

## The eSpeak-derived LuxTTS resource bundle

- **The built app contains it.** `FluidAudio_LuxTtsG2pResources.bundle` sat at the top of `VoiceBench.app` because the `TTS` trait (on by default) links the target (`Package@swift-6.2.swift:65-73`). **measured** (`ls` of the build product).
- **Only LuxTTS reads it.** The one use of `LuxTtsG2pResources.bundle` outside the target itself is `Sources/FluidAudio/TTS/LuxTts/G2p/LuxTtsG2p.swift:83-85`. Kokoro, Phonon and Pocket do not touch it. **read**
- **Deleting it is safe for the three paths.** After removing the bundle from the built app (`rm -rf *LuxTtsG2pResources*.bundle`, then re-sign and install), Kokoro, Phonon and Pocket 18m all loaded and spoke ten lines in three passes with the same numbers as before. **measured**, runs tagged `warm-nolux` in `phone/results/pull/`.
- **Product consequence.** Astral must remove the target at build time. The package manifest has no switch for one target, so the app build needs a phase that deletes the bundle, or a vendored package copy without it. Whether App Store validation and signing accept either is unverified here. Also unverified: the other eSpeak-derived data in the Spanish and French Kokoro lexicons, which English does not use.

## Voices used

Pocket 18m: `alba`, from `alba-mackenna/` in `kyutai/tts-voices`, CC BY 4.0 per that repository's page (read 2026-11-20). `cosette` (Expresso) and `jean` (EARS) are CC BY-NC 4.0 and were not used. Phonon: the checkpoint `default` voice of `phonon-31m-en` (Apache-2.0 per its pack README), English 31m only. Kokoro: `af_heart`, the legacy runtime default. Kokoro's English front end has no stated data origin, so it is **not cleared**.

## Still unknown

Whether `ensureEnglishLexicon` (the one path with no offline check) is ever reached when the lexicon is present, beyond the observed runs. Kokoro on iOS 27.2 survived 60 lines across two launches here. The upstream warning is about long runs and was not tested.
