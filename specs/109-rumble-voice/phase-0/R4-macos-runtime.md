# R4: llama.cpp with libmtmd on macOS arm64 under the hardened runtime

## Question

Does llama.cpp with `libmtmd` build as a macOS arm64 library with Metal, and do Rumble and LFM2.5 run in a hardened, signed host that has no entitlements beyond audio input?

## Answer

Yes. The build works at the pilot's revision `8af68d486a9473820d24d00af8f33334ca80124d` with static libraries and Metal, in about one minute. A small C++ host that links the static libraries loaded both models and ran them on Metal after signing with `codesign --options runtime` and the Developer ID Application identity, with only `com.apple.security.device.audio-input`. llama.cpp needed no JIT entitlement, no unsigned executable memory entitlement and no library validation exception. Not done: the llama.cpp test suite, notarization, and the App Sandbox.

## Build

**Measured**, Apple M5, macOS 27.2.1, Xcode 27.2, Apple clang 21.0.0, cmake 4.5.0, Ninja. Release, arm64, deployment target 14.0, `BUILD_SHARED_LIBS=OFF`, `GGML_METAL=ON`, `GGML_METAL_EMBED_LIBRARY=ON`, `GGML_OPENMP=OFF`, `GGML_NATIVE=OFF`, `LLAMA_BUILD_MTMD=ON`, tools on, server, tests and examples off. Targets `mtmd llama-mtmd-cli llama-completion`. Exact commands and paths are in `voice-bench-109/runtime-mac/README.md`. A second build with the short-clip patch (R2) sits in `build-patched/`.

## Recognition and generation on Metal

**Measured**, `llama-mtmd-cli` and `llama-completion`, `-ngl 99`, greedy, `-c 2048`. Log shows `using device MTL0 (Apple M5)`, `offloaded 15/15 layers to GPU` and `mtmd_backends: using MTL0 backend`.

- Rumble on `Fixtures/remember.wav` (4.01 s): transcript "Remember that my trail marker is blue. What color is it?" Audio encode 31 ms, prompt eval 47 ms for 86 tokens, 13 tokens decoded in 36 ms, 183 ms total in the process, three runs. Process wall time under 1 s with a warm file cache. Memory use was not measured.
- Rumble on `count.wav` (2.42 s): "Count slowly from one to 20." Encode 26 ms.
- LFM2.5 1.2B, system "You are a helpful voice assistant. Reply in one short sentence.", user "Name one fruit that starts with C.": "Cantaloupe is a fruit that starts with C." 36 prompt tokens in 39 ms, 11 tokens in 72 ms (6.5 ms per token), 112 ms total.
- A first CLI run with a cold cache had a 7.2 s wall time. That is file loading, not compute.

The CLI default context is the training context, which costs a 3.5 GiB KV buffer for Rumble. Pass `-c 2048` as the README commands do.

## Hardened host

**Measured.** `host/host.cpp` (C++, about 90 lines, same calls as the pilot's `Inference.cpp`) links `libllama.a`, `libmtmd.a`, the ggml libraries and `libvendor-hash.a`, plus Foundation, Metal, MetalKit and Accelerate. It loads Rumble with its mmproj, recognizes `remember.wav`, frees it, loads LFM2.5 and generates.

Signing: `codesign --force --options runtime --timestamp=none --entitlements host/audio-input.entitlements -s "Developer ID Application: Personalized AI Labs LLC (593LGH35GS)"`. It did not prompt. I used `--timestamp=none` because a secure timestamp needs a network call and notarization is out of scope, so a shipped build must use the default timestamp. `codesign -dv --verbose=4` shows `flags=0x10000(runtime)`, Authority Developer ID Application, TeamIdentifier 593LGH35GS. `codesign -d --entitlements -` shows only `com.apple.security.device.audio-input`. `codesign --verify --strict` passes.

Result, unsigned and signed alike: Rumble load 100 ms, recognition 87 to 91 ms, transcript identical to the CLI, LFM2.5 load 54 to 241 ms, generation 115 to 341 ms, same sentence. 75 `MTL0` log lines, so Metal was in use.

Why no exception is needed: **read** from the log `ggml_metal_library_init: using embedded metal library`. The Metal shader source is compiled by the system Metal compiler at run time, which the hardened runtime allows without `allow-jit`. The libraries are static, so there is no `dlopen` and library validation never applies. The CPU backend uses no generated code. This was run on this Mac only.

## CPU only

**Measured.** `-ngl 0` is not CPU only, a Metal context and compute buffer are still created. `--device none` (API: `devices = {nullptr}`) with `--no-mmproj-offload` (API: `use_gpu = false`) is. Same clip: 466 ms total, encode 154 ms, no `MTL0` lines. Transcript identical. On Metal: 183 ms.

## Short clip

See R2. At the pilot's revision stock output on hard-cut clips under 1 s often ends with an extra word. Patched and zero-padded runs end earlier.

## Not known

- The llama.cpp test suite was not built or run (`LLAMA_BUILD_TESTS=OFF`).
- App Sandbox, notarization and a stapled app were not tried. The host was a command line tool, not an app bundle.
- Peak memory and sustained timings under load were not measured.
- No iPhone or watch measurement. `llama_decode` and `llama_batch_get_one` are marked deprecated at this revision (`llama_process` replaces decode), so an app written against these calls will warn and may break on a later pin.
