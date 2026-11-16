# Virtualization.framework fallback spike (T0.2)

This result supports **C29** (one executor interface, three backends) and gives the evidence for **question 1** in spec section 16. It also bears on the section 15 risk row "`sandbox-exec` is deprecated".

## What was tried

I built `vzspike`, a Swift command-line tool that boots a minimal Linux guest with `VZLinuxBootLoader`, shares one host directory through virtiofs, runs one command in the guest and returns its output and exit status. The code is in `apple-clients/Spikes/VirtualizationFallback/` on branch `feat/106-p0-vz-fallback` of AstralProjection.

The guest has no network device and no vsock device. The only way out besides the workspace share is a virtio console. That console carries the command in and the output and exit status out, framed as one base64 line per command. I chose it because the guest needs no extra kernel module for it. A vsock channel is the better production path and is described under egress below.

The tool takes the five fields of the C29 policy document. It fails closed on the two fields this spike does not implement. A non-empty host list or a read-only path list is refused before boot.

## Tools and sources

- Mac with 32 GB RAM, macOS 27.2.1, Swift 6.4.2, Apple silicon. Host Python 3.11.15 runs the image builder.
- Guest kernel and initramfs come from the official Alpine 3.24.4 aarch64 netboot tarball, Linux 6.18.60-0-virt, BusyBox 1.37.0.
- The tarball SHA-256 `9d10f909...359745e` matches the value in Alpine's published `latest-releases.yaml`. The builder pins it and refuses any other.
- The Alpine kernel ships as an EFI zboot wrapper. The builder unwraps the gzip payload to the raw arm64 `Image` that `VZLinuxBootLoader` needs.
- Python packages for the guest come from the Alpine v3.24 `main` and `community` repositories. The builder checks the index signature with `openssl` against the Alpine key found inside the verified tarball, then checks each `.apk` control stream against the signed index and each data stream against the `datahash` in its `.PKGINFO`. A test with one flipped byte in a repackaged `zlib` payload stopped the build with `data stream differs from the datahash`.
- I did not use the Ubuntu cloud image. Alpine covered the question, and a second image would have added download weight without changing the answer.

## Commands

```
swift test --scratch-path $S/swiftpm-test                      6 tests, 0 failures
Scripts/build-and-sign.sh $S                                   builds, then codesign --sign - with the virtualization entitlement
python Scripts/build_guest.py --scratch $S --init Guest/spike-init --dhcp-script Guest/spike-dhcp   base image
python Scripts/build_guest.py --scratch $S --init Guest/spike-init --dhcp-script Guest/spike-dhcp --package python3 --suffix py   image with Python
Scripts/confinement-checks.sh $S                               12 checks, 0 failures
python Scripts/measure.py $S                                   timings and memory below
```

`$S` is `~/Library/Caches/astral-106-phase-0/vz`. Ruff 3.11 target passes on the Python scripts.

## Measurements

Ten cold runs per image, thirty warm runs, on an otherwise busy developer Mac. A cold run is a fresh process, VM start, guest boot and the first command result. A warm run reuses the live VM. The table shows the final session. An earlier busier session gave 364 and 536 ms, so expect load to move these numbers by half.

| Measure | Alpine base | Alpine plus Python 3.14 |
|---|---|---|
| Cold, launch to first command result (median, min, max) | 220 ms, 219, 233 | 308 ms, 304, 335 |
| Of which `VZVirtualMachine.start` call | 81 ms | 84 ms |
| Guest uptime when it reported ready | about 0.15 s | about 0.17 s |
| Warm `true` in a live VM (median) | 1 ms | 1 ms |
| Python `import json, sqlite3, ssl`, first run | n/a | 1341 ms |
| Same import, warm (median) | n/a | 14 ms |
| Guest assets on disk (kernel plus initramfs) | 36.3 MB plus 9.4 MB | 36.3 MB plus 23.3 MB |
| Disk written per run | 0 (RAM-only guest, no disk image) | 0 |

- **Host memory.** The guest lives in Apple's `com.apple.Virtualization.VirtualMachine` helper, and the tool itself holds 2.8 MB. Helper footprint with a 512 MB guest is 144 MB idle, 192 MB at launch with Python and 373 MB after Python touches 200 MB. A 1024 MB guest reads 404 MB for the same work. Guest memory is only paid for when touched.
- **Python layer.** 18 apks, 39.3 MB installed, 13.9 MB added to the compressed initramfs. Python 3.14.10 imports `json`, `sqlite3`, `ssl` and `hashlib` and reports OpenSSL 3.5.9.
- **First Python import** is slow because the layer has no precompiled bytecode. The `python3-pycache-pyc*` packages would remove most of that. I did not add them.
- **Console throughput.** A 4 MB output returned in 2.4 s including boot. Large outputs belong on a share or vsock.

## Confinement checks

Full log: `~/Library/Caches/astral-106-phase-0/vz/confinement.log`. All twelve checks passed. The guest init loads `virtio_net` and brings `lo` up in every run, so a missing device is the only reason the guest has no `eth0`.

| Check | Command in the guest | Result |
|---|---|---|
| Write outside the share | `echo x >/tmp/p`, `/etc/p`, `/p` and `/workspace/inside.txt` in turn | All four succeed in the guest. On the host only `inside.txt` exists |
| Host symlink in the share | host `ln -s <host dir> ws/host-link`, guest `cat /workspace/host-link/secret.txt` | `No such file or directory` |
| Escape by `..` | `cat /workspace/../etc/hostname`, `ls /Users` | Resolve inside the guest root and fail |
| Device count | `vzspike --json` reads `machine.networkDevices` and `machine.socketDevices` | `"network_devices": 0` and `"socket_devices": 0`. This is the attributable evidence, since `GuestMachine.swift` configures neither device |
| Positive control | Same probe script, run with `--nat-control` (adds one `VZNATNetworkDeviceAttachment`, guest takes a DHCP lease) | Link list `lo`, `eth0`. `wget` to the DHCP gateway returns the host listener's `secret.txt`, and the listener logs 1 request |
| No device | Same image and probe script, no `--nat-control` | Link list `lo` only. `wget` to `192.168.64.1` and `1.1.1.1` return `Network unreachable`. The listener on `0.0.0.0:18765` logs 0 requests |
| Host sockets | `lsof -nP -a -p <tool pid>,<its VM helper pid> -i` during a run | 0 inet sockets. The script fails if it cannot find the tool or its own new helper process |
| File size limit | `dd if=/dev/zero of=big bs=1M count=300` | `File size limit exceeded`, status 153, file stops at 256 MB |
| Memory limit | Python allocates 900 MB in a 512 MB guest | `MemoryError` in the guest, host unaffected |
| Time limit | `sleep 60` with `--timeout 3` | `no result after 3 seconds`, exit 124, VM stopped |
| No persistence | file written in one run, `ls /tmp` in the next | Not present |

The positive control is the reason the no-device probes mean anything. With `virtio_net` loaded and `lo` up, the only difference between the two runs is the attached device. The guest-side probes still corroborate the device count and do not replace it. The result says nothing about vsock from inside the guest, because the initramfs lacks the vsock modules.

The guest runs as root inside its own VM. Guest `ulimit` values are advisory against a hostile guest, since root can raise them. The VM memory and CPU settings are the hard limits.

## The egress proxy

The spike keeps the guest at zero network devices. A proxy would join in this way. Add a `VZVirtioSocketDeviceConfiguration`, listen on a vsock port on the host and run a small forwarder in the guest that accepts loopback TCP on `127.0.0.1:3128` and relays the bytes over `AF_VSOCK`. The host end hands each stream to the existing hostname allowlist logic in `MacComputerEgressProxy.swift`, with the same refusal of private, loopback, link-local and metadata addresses. Tools that ignore the proxy variable find no route and fail closed, as section 10.4 requires.

I rejected `VZNATNetworkDeviceAttachment`. It gives the guest a route to the internet and to the host's gateway address, which bypasses the proxy.

The Alpine initramfs lacks the vsock modules. They live in the modloop, so this step needs a modloop pass or a custom kernel. UNVERIFIED because I did not build it.

## Entitlements and signing

- The tool is ad-hoc signed with only `com.apple.security.virtualization`. It booted guests on this Mac with SIP on and no provisioning profile.
- The direct-download app already carries hardened runtime. It needs that one entitlement added to `AstralApp-DirectDownload.entitlements`, which holds only `audio-input` today. Notarization with the new entitlement is UNVERIFIED.
- The Mac App Store build can ask for the same entitlement with the App Sandbox on, and Xcode lists Virtualization as a sandbox capability. I did not build a sandboxed variant, so acceptance in review is UNVERIFIED. A virtiofs share in a sandboxed app also needs a user-selected or bookmarked directory.
- Section 10.3 already says only the direct-download build hosts the governed-command host and the node. The App Store build gains nothing from this backend.

## What fits behind the same interface

| C29 policy field | VM backend |
|---|---|
| Writable workspace | Fits. One virtiofs share, read-write |
| Allowed hosts | Fits with the vsock forwarder above. Empty list works today |
| Limits | Memory and CPU are hard VM limits. Process, file and size limits are guest `ulimit`, advisory against root |
| Time | Fits. Host kills the VM and returns 124 |
| File reads | Fits as the guest image plus optional read-only shares. Not built here |

What does not fit:
- **Linux binaries only.** No `xcodebuild`, `swift`, `git` from the Command Line Tools, `osascript` or any Mach-O tool. Rosetta in Linux guests would add x86_64 Linux binaries and still no macOS ones. I did not test it.
- **The node needs a Linux build.** Alpine 3.24 ships Python 3.14 only, and the node targets 3.11. The bundle in C27 is a macOS artifact. A Linux guest needs its own relocatable 3.11 build with musl or glibc wheels. UNVERIFIED which image is cheaper to maintain.
- **File semantics.** virtiofs ownership, permissions and symlinks follow the guest. Host-visible results can differ from native Mac files.
- **A hostile guest can write any bytes to the console.** The host must treat every result as untrusted data and never reuse a result id as an authorization.

## Recommendation for question 1

Keep Seatbelt `enforce` as the Mac default. It costs nothing, runs macOS tools and already has code. Phase 4 should still flip the shipped default from `shadow` to `enforce` for untrusted code, as C29 says.

Adopt this backend as the named contingency. Its trigger is Apple removing `sandbox-exec`. It would then cover node tools and any Linux command. Mac-native commands would have no sandbox, so the owner should decide now whether the governed-command host refuses them in that case. I recommend refusing, which matches "untrusted code never runs without an enforcing backend".

Cost if the trigger fires. The spike is under 1000 lines including tests. Production adds the vsock forwarder and proxy bridge, a pinned guest image with its own supply chain, a Linux Python 3.11 bundle, read-only shares and a conformance run (C30). I estimate several weeks of work. That estimate is UNVERIFIED. Runtime cost is a 0.4 to 0.6 s cold start per sandbox, 2 ms per warm command, and 150 to 400 MB of host memory per live VM. Disk cost is 45 to 60 MB of guest assets.

## Open risks

- Timings come from one Mac under normal load. They are indicative and carry no pass or fail bound.
- The Ubuntu path, Rosetta, memory ballooning, a warm VM pool and a long-lived VM were not tried.
- Apple could change Virtualization.framework entitlements or Mac App Store rules. I read no review guidance on it.
- Guest kernel and image updates become a patching duty for the project.
