# Node bundle spike (T0.5)

## Result

A relocatable macOS arm64 node bundle builds from an empty work directory in 330 seconds and passes every relocation check. The compressed archive is 514 MB as built. The Homebrew poppler and libmagic carry a macOS 27 minimum, which blocks the Mac app's macOS 14 target. That is the one hard blocker.

## What was tried

`build_bundle.sh` assembles the bundle in a scratch directory outside the repository.

1. python-build-standalone release `20261110`, `cpython-3.11.18+20261110-aarch64-apple-darwin-install_only.tar.gz`. SHA-256 `73f1ed14486aa278d331e95f7d6d452f5cfb69d90b418e26e62fd4846d53557d`. The value matches both the release `SHA256SUMS` file and the digest the GitHub API publishes for the asset. The script refuses a mismatch.
2. `backend/requirements.txt` plus `requirements-document-worker.txt`, installed the way the Dockerfile does. 150 packages resolved from PyPI on 2026-11-15. The repo pins no hashes for these files (ranges only, and `uv.lock` holds only the empty composition package). The script writes `locked-requirements.macos-arm64.txt` with the digest pip recorded for each wheel, and that file is committed beside the script.
3. The five component wheels through `scripts/install_local_components.py` `build`, `install` and `verify`, with the same pinned build tools as the Dockerfile. The installer needs no change. It skips the gitlink check when the tree has no `.git`, so the script reads each commit from `config/astral-composition.json` and feeds the installer a `git archive` of that commit. The worktree's submodules are empty, so I read the five checkouts under `/Users/sam/Desktop/Work/AstralDeep/components` read-only. Their HEAD commits equal the pins. The first build used `git archive HEAD`, which gave the same tree.
4. `en_core_web_lg` 3.8.0, found in `presidio_analyzer` use through `backend/personalization/phi_gate.py` and the Dockerfile `spacy download` line. The wheel comes from the explosion/spacy-models GitHub release. SHA-256 `293e9547a655b25499198ab15a525b05b9407a75f10255e405e8c3854329ab63`. GitHub publishes no digest for it, so the pin is my own download. UNVERIFIED against upstream.
5. The Kernel Python wheel. `config/astral-kernel-binding.json` pins Linux only, and the binding is unchanged. I built the macOS wheel and the mailbox from the pinned Kernel commit with `cargo build --release --locked --offline --target aarch64-apple-darwin` and the Kernel's own `scripts/package_wheel.py` (tag `macosx_14_0_arm64`). The build ran offline from the local cargo cache, so a clean machine would fetch crates from crates.io. That source is outside this spike's allowed list and stays UNVERIFIED. The final build remaps source and cargo registry paths with `--remap-path-prefix`. Its wheel SHA-256 starts `30832a0f` and its mailbox starts `982146cc`. Reproducibility is unproven, since I did not set `SOURCE_DATE_EPOCH` or compare two builds.
6. poppler 26.11.0 (`pdftoppm`, `pdfinfo`) and libmagic 5.48 from Homebrew bottles tagged `arm64_golden_gate`. `collect_native.py` checks each bottle digest against `brew info --json=v2`, copies only the 32 dylibs the tools load (of 54 runtime bottles), rewrites install names to `@loader_path`, deletes rpaths, and re-signs ad hoc. It fails if any `@@HOMEBREW`, `/opt/homebrew` or `@rpath` load command remains.
7. ripgrep 15.2.0 from the BurntSushi/ripgrep GitHub release `ripgrep-15.2.0-aarch64-apple-darwin.tar.gz`. SHA-256 `3750b2e93f37e0c692657da574d7019a101c0084da05a790c83fd335bad973e4`, matching the digest GitHub publishes. It is one binary with PCRE2 built in, `minos` 11.0, and it loads only system libraries. The Homebrew ripgrep bottle linked a macOS 27 libpcre2, so the first build used it and I replaced it.
8. `astral_bundle_paths.py` plus a `.pth` file. It points `ctypes.util.find_library`, `PATH`, `MAGIC` and `FONTCONFIG_FILE` at the bundle, so python-magic and pdf2image resolve their libraries and tools with no DYLD variables.

Tools used were macOS 27.2.1, Homebrew 7.0.13, cargo and rustc 1.99.1, zstd 1.5.7, bundled pip 26.3.

## Commands

```
specs/106-sign-in-free-clients/phase-0/node-bundle/build_bundle.sh /Users/sam/Desktop/Work/AstralDeep/components "$SCRATCH/build2"
specs/106-sign-in-free-clients/phase-0/node-bundle/verify_relocation.sh "$SCRATCH/build2"
```

`$SCRATCH` is `/Users/sam/Library/Caches/astral-106-phase-0/bundle`. The first command ran with the downloads, Homebrew cache and Rust target directory deleted. The cargo registry cache under `~/.cargo` stayed warm, so a machine with no cache would add crate download time. The second command copies the bundle to `relocated copy/bundle`, a path with a space. It then moves the original directory away and runs `env -i PATH=/usr/bin:/bin "$TARGET/bundle/python/bin/python3" -I relocation_check.py`. The check output was `ALL CHECKS PASSED`. `PATH` held one temp directory with a symlink to `/usr/bin/vmmap`, so `python3` and `python` are not on it, and the check asserts that.

## Relocation checks that passed

- The interpreter, `libmagic.1.dylib`, `pdftoppm`, `rg`, the spaCy model and the kernel dylib all load from the copied path. `vmmap` shows no `/opt/homebrew` or `/usr/local` dylib mapped.
- `magic.from_buffer` sniffs a PDF. `pdfinfo_from_bytes` and `convert_from_bytes` render a ReportLab PDF through the bundled poppler. `rg` counts a match on stdin.
- `spacy.load("en_core_web_lg")` parses a sentence, and Presidio `AnalyzerEngine` finds PERSON and US_SSN with it.
- `astral_kernel`, `astralplane`, `astralprojection`, `astralprims`, `lets`, `rote`, `psycopg2`, `openslide`, `SimpleITK`, `orchestrator.orchestrator` and `start` import. `start` is the module `start-docker.sh` execs. The node's backend source sits in `app/backend`.
- `astral-mailbox --help` exits 0.

`pip check` ran clean at build time, inside the original bundle only. No `.pyc` or binary holds a local path after the remap. Seven `direct_url.json` files in `*.dist-info` still hold `/Users/sam/...` paths to the scratch wheels. `install_local_components.py verify` reads them, so the script keeps them.

This Mac has libmagic under `/opt/homebrew`, so the check prints the loaded library path to prove the bundle copy won.

## Measurements

| Item | Value |
|---|---|
| Build to a finished bundle, downloads and Homebrew and Rust caches empty | 330 s, plus 110 s for `zstd -19` (440 s in all) |
| Same build with downloads cached | 154 s, plus 71 s for `zstd -19` |
| Time by step, cold | wheels and requirements 50 s, components 18 s, model 15 s, Kernel wheel and mailbox 166 s, native tools 75 s, source and bytecode 3 s |
| Total size | 1,486 MB (36,336 files, 373 Mach-O files) |
| Python interpreter and stdlib | 80 MB |
| Wheels (site-packages without the model, bytecode included) | 874 MB, of which the Kernel wheel is 35 MB |
| Native tools and libraries | 50 MB (32 dylibs 17 MB, binaries 22 MB with `astral-mailbox` 18 MB, `magic.mgc` 11 MB) |
| spaCy model | 445 MB |
| Node backend source | 37 MB |
| Bytecode (`.pyc`, 15,601 files, counted in the parts above) | 273 MB |
| Package `test` and `tests` directories | 107 MB |
| `.tar.zst` level 19, as built | 514 MB |
| Same, tests dropped, bytecode kept | 492 MB |
| Same, tests and bytecode dropped | 449 MB |

The two smaller archives are size measurements only. I did not import-test a tree with the test directories removed, so they are UNVERIFIED as shippable. An earlier 391 MB figure came from a tar run while the verify script moved the bundle directory, so it was truncated and I discarded it.

Largest ten site-packages entries (MB) are en_core_web_lg 445, SimpleITK 180, scipy 99, pandas 80, plotly 56, phonenumbers 47, astral_kernel 35, numpy 34, spacy 27 and sqlalchemy 21.

I did not measure a `.zip`. Windows would need one later.

## Blockers and findings

1. **Homebrew poppler and libmagic need macOS 27.** Every Mach-O in those bottles and their libraries has `minos` 27.0 or 27.2, while the Mac app targets macOS 14 (`MACOSX_DEPLOYMENT_TARGET = 14.0`). dyld refuses a binary built for a newer OS. I did not run the tools on an older OS, so the failure is UNVERIFIED, but the load command is explicit. Poppler lists only the `arm64_golden_gate` bottle, so no older bottle exists to fetch. The options are to build poppler and libmagic from source with `MACOSX_DEPLOYMENT_TARGET=14.0`, or to replace `pdf2image` with a PyPI wheel that bundles its own renderer. The second option changes product code. ripgrep from the upstream release, Python, the wheels and the Kernel dylib and mailbox target 11.0 to 14.0.
2. **Hardened runtime breaks the bundle until library validation is relaxed.** I signed a copy of `python3.11` with `--options runtime` and ad hoc identity. `import numpy` failed in dlopen with a code signature error. Adding `com.apple.security.cs.disable-library-validation` fixed `import numpy, spacy`. `ctypes` and `cffi` callbacks, PyNaCl and `cryptography` also loaded, and none of them needed `allow-jit`. The alternative is to sign all 373 Mach-O files with the app's Developer ID so library validation passes with no entitlement. That plan is UNVERIFIED because I had no Developer ID in this spike.
3. **The model is the largest single part.** `en_core_web_lg` is 445 MB, about a third of the bundle. A smaller model changes PHI detection quality, so that choice belongs to the owner.
4. **Bytecode must be baked in.** The script compiles it at build time, so a signed bundle never writes `__pycache__` at run time. It adds 273 MB uncompressed. The launcher must start Python with `-B`, since `-I` ignores `PYTHONDONTWRITEBYTECODE`. The relocation check runs with `-I -B`.
5. **The Kernel binary paths leak without remapping.** The first build left `/Users/sam/.cargo/registry/...` strings in the Kernel dylib and the mailbox. The script now passes `--remap-path-prefix` and a string search finds none. Any other build host needs the same flag.
6. **No hash lock exists in the repo for the node's requirements.** The generated lock is a snapshot from 2026-11-15. A release build needs a committed lock that CI regenerates on purpose.

## What a Mac release still needs

- Sign every Mach-O (373 here) with Developer ID and `--options runtime`, inside out, then notarize the archive contents. UNVERIFIED here. I expect a tarball to hold no stapled ticket, so first launch would need the network for the notarization check. I did not test it.
- Entitlements. `disable-library-validation` for an ad hoc or mixed-identity bundle, shown above. None if every Mach-O shares one Team ID, which is UNVERIFIED. No JIT entitlement showed up as necessary.
- Console scripts. The script deletes every `python/bin` file with a Python shebang. The `pip3` trampoline PBS ships resolves its own directory, so it stays relocatable.
- Launch with `python3 -I -m` or an explicit script so site customization stays under the bundle's control.

## Linux builds

- x86_64 and aarch64 use the matching python-build-standalone `unknown-linux-gnu` install_only archives with their own published hashes.
- Native tools come from apt in the target image, from a manylinux-style build, or from conda-forge. Homebrew bottle relocation does not carry over. libmagic needs its `magic.mgc` and `MAGIC` set. poppler needs `FONTCONFIG_FILE`.
- The Kernel wheels and mailboxes already exist for both architectures in `astral-kernel-binding.json`. No Rust build is needed.
- The 150-package lock changes per platform, since SimpleITK, scipy and others ship platform wheels.
- The shim and relocation check port with `.so` and `ldd` or `patchelf` in place of `.dylib` and `install_name_tool`.

## Calls

- **O24** (app downloads the bundle and checks a built-in hash). Supported. One archive, one SHA-256, verified before extract. The size is a first-launch cost. 449 to 514 MB will take a while on slow links, and I did not measure a download.
- **C27** (self-contained bundle with Python, wheels, poppler, libmagic, ripgrep, spaCy model). Supported with one correction. The task brief suggests Homebrew bottles for the natives, and that route does not reach macOS 14 for poppler and libmagic. Those two need a deployment-target build. Section 10.2's list of parts matches what I built.
- **Spike item "sign it, run it under the hardened runtime"**. Partly answered here with an ad hoc experiment on one binary. The Developer ID run and the App Sandbox test stay open for the signing spike.
- **C32** (no Docker assumptions on the user path). Unchanged. `start.py` imports cleanly, and I did not start the server.

## Open risks

- The Kernel's macOS build used a populated cargo cache and was not repeated on a clean machine.
- The bundle ran on macOS 27.2.1 only. No older macOS was available.
- Build and check ran on one machine and one day. The requirements lock holds whatever PyPI served on 2026-11-15.
- poppler is GPL and a Homebrew bottle bundle redistributes it. The license review is open.
- The spaCy model has no upstream digest.

## Files

- `specs/106-sign-in-free-clients/phase-0/node-bundle/build_bundle.sh`
- `specs/106-sign-in-free-clients/phase-0/node-bundle/collect_native.py`
- `specs/106-sign-in-free-clients/phase-0/node-bundle/astral_bundle_paths.py`
- `specs/106-sign-in-free-clients/phase-0/node-bundle/relocation_check.py`
- `specs/106-sign-in-free-clients/phase-0/node-bundle/verify_relocation.sh`
- `specs/106-sign-in-free-clients/phase-0/node-bundle/locked-requirements.macos-arm64.txt`
- `specs/106-sign-in-free-clients/phase-0/node-bundle/native-manifest.json`
