# R2: llama.cpp short-clip padding fix

## Question

Does llama.cpp at `8af68d486a9473820d24d00af8f33334ca80124d` carry the fix for the short-clip padding problem reported on the Rumble GGUF card? If not, which revision does?

## Answer

No revision carries it. The fix is not merged upstream and no open llama.cpp pull request for it was found. The pilot's revision (tag b12549, on master, 2026-11-19) has the bug, and so does master as of 2026-11-20 (b12584). The qwen3a audio files are byte-identical between the pilot's revision and master. Pin the pilot's revision and carry the third party patch `qwen3a-tail.patch`, or pin it unpatched and pad audio in the app. Both pinned options contain the background GPU fix (see the last section).

## The bug

**Read** (`tools/mtmd/mtmd-audio.cpp` lines 593-700, `tools/mtmd/mtmd-model.cpp` line 4886, `tools/mtmd/models/qwen3a.cpp` at the pilot's revision). `mtmd_audio_preprocessor_qwen3a::process` splits the mel spectrogram into windows of up to 800 frames and zero fills each window up to a multiple of 100 frames. `n_output_tokens` then returns `(nx / 100) * 13` for the padded width, and the graph feeds every token, padding included, into the encoder transformer with no mask. A 3.0 s clip is 301 frames. It becomes 400 frames and 52 tokens, where the Transformers reference gives 40. The padded tokens look like sound to the model, so it can append words that were not said.

**Read** (GGUF card `AtomicChat/Rumble-2-Flash-GGUF`, revision `d036f2af`, modified 2026-11-18). The card says the zero padding is passed to the model as sound, that its authors wrote a fix and are preparing it for upstream, and that the patch is in their metrics dataset. Tested on b12521. The source card `Reson8/Rumble-2-Flash` says nothing about it.

**Read** (`AtomicChat/Rumble-2-Flash-GGUF-metrics`, `scripts/upstream/README.md`). Same diagnosis, with a table of four LibriSpeech clips and a 2,200 utterance WER comparison. Their numbers: LS clean 3.13 to 2.09, LS other 5.68 to 4.70. The README describes the PR as staged and queued, not opened.

## The patch

`scripts/qwen3a-tail.patch` from that dataset. SHA-256 `b8f589e536c7ae806b1ff7f1a361149dfd47d69a65cffb62ba94ef8880e262c0`, 144 lines, 6 files, +45 and -5. It records the number of valid mel frames per window, computes the token count from the valid frames, and drops the padded tokens before `build_vit`. It bumps `MTMD_SERIALIZATION_VERSION` from 5 to 6.

**Measured** (`git apply --check`, then a full build in `src-patched/`). It applies cleanly at the pilot's revision and builds.

Unknown: the patch's licence terms (the dataset card was not checked for one), and whether upstream will accept it as written.

## Method for the upstream check

- **Measured** with a blobless clone of github.com/ggml-org/llama.cpp, fetched anonymously on 2026-11-20. `git merge-base --is-ancestor 8af68d48 origin/master` is true. `git tag --contains` gives b12549 as the first tag.
- **Measured** `git diff 8af68d48 origin/master -- tools/mtmd/models/qwen3a.cpp tools/mtmd/mtmd-audio.cpp tools/mtmd/mtmd-audio.h` is empty. Master adds nothing here.
- **Read** `git log` on `tools/mtmd` found only these related commits, all already in the pilot's revision: `72e60f500` (#23073, adds chunks for qwen3a, the source of the bug), `0cce7580b` (#30403, padding only chunks for Whisper, not qwen3a), `cc97e45a1` (#22770, Whisper tail). None changes the qwen3a padding.
- **Read** GitHub issue search, anonymous. Open items touching Qwen3-ASR are #32547 (the `language ...<asr_text>` prefix) and #32571 (a convert change for the same prefix). Neither is about padding. The search API hit its anonymous rate limit before every query ran, so a PR with unusual wording could be missed.

## Short-clip test

**Measured** on an Apple M5, `llama-mtmd-cli` Release build, Metal, greedy, Rumble AD-Q5_K_M and the Q8_0 mmproj, `lang:en`. 21 clips: seven lab fixtures cut hard to 0.6, 0.9 and 1.5 s. Files: `voice-bench-109/runtime-mac/short-clip-matrix-3way.txt`. Columns are stock, stock with the PCM padded with zeros to `16000*k - 160` samples, and patched.

Stock and patched differ on 13 of 21 clips. Stock more often ends on an extra word the audio does not contain. Examples: "Name one friend." (patched "Name one."), "Remember that it." (patched "Remember that."), "Remember that my trail markers are." (patched "Remember that my trail marker."), "What color did you?" (patched "What color?"). Stock with padding matched patched exactly on 12 of 21 clips and differed only by a word or a word ending on the rest.

This is indicative, not a WER. The clips are cut mid-word, there is no ground truth for the cut, and patched output sometimes stops earlier ("Count slowly from"). The effect is real and small in size, and stock never lost words the patched build kept.

## Options

1. Pin the pilot's revision and carry `qwen3a-tail.patch` in the vendored build. Matches the Transformers reference in the authors' numbers. Needs a patch step, and a re-pin when upstream merges a fix.
2. Pin the pilot's revision unpatched and pad each clip in the app to `16000*k - 160` samples with PCM zeros. No native change. Measured only on the 21 clips above.
3. Wait for upstream. No open PR was found, so there is no date.

## Background GPU (llama.cpp PR 20306)

**Read** (api.github.com/repos/ggml-org/llama.cpp/pulls/20306, anonymous, and `git show 1a5631bea`). Title "metal: handle command buffer failures gracefully in synchronize". Merged 2026-03-10T06:32:25Z. Merge commit `1a5631beaa2bfb9f76f177a8dbe5544dd6c2d528`. One file, `ggml/src/ggml-metal/ggml-metal-context.m`, +22 and -2. The PR text says iOS revokes GPU access when an app goes to the background, pending command buffers then fail with `kIOGPUCommandBufferCallbackErrorBackgroundExecutionNotPermitted`, and the old `GGML_ABORT("fatal error")` in `ggml_metal_synchronize()` killed the process.

What it does: sets a sticky `has_error` flag, logs, and returns. Later `ggml_metal_graph_compute()` calls return `GGML_STATUS_FAILED` until the backend is recreated.

What it does not do: it does not make Metal work in the background. The host sees a failed decode and must free the context and rebuild it, ideally on CPU, when the app returns. The PR notes the first failure can surface one call late because `synchronize` returns void.

**Measured** `git merge-base --is-ancestor 1a5631bea <rev>`: true for `8af68d486a9473820d24d00af8f33334ca80124d` and true for master. First tag containing it is b8267. `has_error` is present in the pilot's `ggml-metal-context.m` (lines 84, 176, 277, 301, 475). Two other `GGML_ABORT` calls remain in that file (lines 338, 384, missing buffer for a tensor), which are unrelated.

So the recommended revision, the pilot's `8af68d48`, has the background GPU fix. It lacks the short-clip fix, because that does not exist upstream. Not verified: the claim about iOS backgrounding was not reproduced on a device. This Mac run does not exercise it.
