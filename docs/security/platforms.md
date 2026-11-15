# Platform support

Sandboxes depend on Linux kernel features, so where AstralVessel can run safely is decided by the host kernel. This
page says what is supported and what has not been checked.

| Platform | Status |
|---|---|
| Linux 6.12 or newer, x86_64 or arm64, with Landlock ABI 6 | Supported. CI runs the tests and the isolation probes on Ubuntu 24.04 runners (x86_64). |
| Linux older than 6.12, or with Landlock disabled | Not supported. Sandboxes refuse to start. |
| Other CPU architectures | Not supported. The seccomp filter exists only for x86_64 and aarch64; elsewhere every confined call fails. |
| macOS with Docker Desktop, Apple silicon | Supported. Containers run in Docker Desktop's Linux VM, whose kernel offers Landlock ABI 6. Checked on Docker Desktop 29.5 (VM kernel 6.12) with `astralvessel up --example`: all isolation probes passed. Older Docker Desktop versions may ship an older kernel; the sandbox then refuses to start and `up` shows why. |
| Windows with Docker Desktop | **Unverified.** Containers run in Docker Desktop's Linux VM (WSL 2); whether its kernel offers Landlock ABI 6 has not been checked. |

Released images are built for amd64 and arm64.

## Check a host

Landlock ABI 6 is what the sandbox checks for. Run the same check the sandbox makes, inside the AstralVessel image:

```bash
docker run --rm astralvessel:local python -c "from astralvessel import confine; print(confine.abi_version())"
```

It prints the kernel's Landlock ABI version; `6` or higher is required. `0` means Landlock is unavailable. If the
deployment is already running, a sandbox that cannot confine exits at startup with:

```text
this kernel lacks Landlock ABI 6; per-call isolation is unavailable (set VESSEL_UNCONFINED=1 only for development)
```

## Python

The `astralvessel` command needs Python 3.11 or newer; CI tests 3.11 and 3.12. The executor and sandbox image uses
Python 3.11, the egress image Python 3.12. The node image brings its own Python.
