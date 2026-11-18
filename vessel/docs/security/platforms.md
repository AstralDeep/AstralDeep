# Platform support

A user install runs the node and its tools as processes on your machine. It needs no container engine. Each tool call is confined by what the host operating system offers.

This page says which hosts work and what each one gives you.

| Host | Status |
|---|---|
| macOS on Apple silicon | Supported. Tool calls run under Seatbelt (`sandbox-exec`) with a policy rendered from the agent's profile. |
| Linux 6.12 or newer, x86_64 or arm64 | Supported. Tool calls run under bubblewrap with Landlock ABI 6 and seccomp. Needs unprivileged user namespaces. |
| Linux older than 6.12, or with Landlock off | `read`, `glob` and `list` work. `bash` and declared tools are refused with `exec.backend_unavailable`. |
| Other CPU architectures | Not supported. No node pack exists for them. |
| Windows | Not supported as a deployment host: no confinement backend exists for it, so `astralvessel up` refuses it with `platform_unsupported`. The Python package imports and the test suite run on Windows for development. See Developing on Windows. |

Every refusal names the missing piece: `landlock_abi`, `bwrap_missing`, `userns_denied`, `sandbox_exec_missing`, `app_sandboxed` or `platform_unsupported`. `astralvessel up` prints it.

## What macOS gives you less of

Seatbelt confines each call, but it has no cgroups. Memory, process and time limits apply to one call at a time. There is no cap on the total memory or CPU of an agent across calls.

On Linux the agent's systemd unit also sets `MemoryMax`, `TasksMax` and `CPUQuota` when your user manager delegates those controls.

## Ubuntu 24.04

Ubuntu 24.04 restricts unprivileged user namespaces with an AppArmor rule. Bubblewrap cannot start under it, and the refusal reads `userns_denied`.

The rule is the sysctl `kernel.apparmor_restrict_unprivileged_userns`. An administrator can allow `/usr/bin/bwrap` with an AppArmor profile, which keeps the restriction for everything else. Setting the sysctl to 0 also works and is broader.

The stock 24.04 kernel is 6.8, which offers Landlock ABI 4. Install the HWE kernel to get 6.14 or newer, then reboot. Check the result with `astralvessel check`.

## Check a host

```bash
astralvessel check
```

It runs the backend conformance suite from the node pack and prints one line per case. A case the host cannot run prints as skipped with the reason. A failed case exits with status 1. See [Isolation and probes](isolation.md).

## Developing on Windows

Windows is not a deployment host, but the package imports there and the test suite runs, so the
harness and its clients can be developed on one. Every test that needs a POSIX property carries a
skip with its reason (`needs_bash`, `needs_procfs`, `needs_posix_modes`, `needs_host_backend` and
the rest in `tests/conftest.py`). The process-exec backend and the sandbox conformance cases stay
Linux-only. A test that fails only some of the time is never skipped for that alone. It is
investigated first, because a real ordering fault can hide behind a platform difference.

## Python and the node pack

The `astralvessel` command needs Python 3.11 or newer. The node and every Vessel role run on the Python inside the node pack, so nothing else is installed. A Linux pack needs glibc 2.36 or newer, and the install says so when yours is older.

## Container mode

Container mode is the older way to run Vessel. It stays available for folders that already use it, and you opt in with `mode = "container"` under `[runtime]`.

It needs Docker with the Compose v2 plugin and a Linux kernel that offers Landlock ABI 6. On a Mac that means Docker Desktop, whose Linux VM must run kernel 6.12 or newer. Windows with Docker Desktop has not been checked.

Check the kernel from inside the image:

```bash
docker run --rm astralvessel:local python -c "from astralvessel import confine; print(confine.abi_version())"
```

It prints the Landlock ABI. `6` or higher works and `0` means Landlock is off.
