# Landlock minimum for the Docker-free node

Task T0.3. Written 2026-11-15. Research only, no code run.

## Call

Keep `REQUIRED_ABI = 6` in `astralvessel/confine.py`. Below ABI 6 the executor refuses untrusted code.

This supports C29 as written (Linux row of section 10.3). It reverses nothing.

## What confine.py uses at ABI 6

I read `/Users/sam/Desktop/Work/AstralVessel/astralvessel/confine.py` (Vessel at 25a056c).

- Filesystem. It handles every filesystem right the kernel knows (13 bits at ABI 1 up to 16 bits at ABI 5 and above). It grants read and execute on system paths and the agent's data, read and write on four device files, and all rights except `MAKE_SOCK` on the one scratch directory. Anything not granted is denied.
- TCP. It handles bind and connect for TCP (ABI 4) and grants no port.
- Scoping. It sets `LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET` and `LANDLOCK_SCOPE_SIGNAL` (ABI 6).
- Gaps it covers by other means. A seccomp filter blocks SysV IPC, POSIX message queues, `setsid`, `setpgid` and AF_INET and AF_INET6 sockets. It sets `NO_NEW_PRIVS`, `RLIMIT_FSIZE` and `RLIMIT_NOFILE`.

The ruleset struct has three fields. Older kernels likely reject a struct of that size, so the call would fail even without the check (UNVERIFIED, from memory of the kernel size check).

## What it does below the minimum today

`abi_version()` returns the kernel's ABI, or 0 when Landlock is off or missing. `apply()` raises `OSError` when the value is under 6. `main()` catches it, prints "the sandbox could not confine this call" and exits 126 before running the command.

`sandbox.py` line 155 checks the same value when a sandbox server starts. It exits with "this kernel lacks Landlock ABI 6" unless `VESSEL_UNCONFINED=1` is set, which the docs limit to development.

Vessel has no trusted-tool path. Every agent tool runs through that sandbox server, so on an old kernel every agent in Vessel fails to start. Vessel's `docs/security/platforms.md` says the same.

## Landlock ABI table

Kernel versions come from the man-pages table in landlock(7). Features come from kernel.org.

| ABI | Adds | First mainline kernel |
|---|---|---|
| 1 | Filesystem rights | 5.13 |
| 2 | `FS_REFER` (rename and link across directories) | 5.19 |
| 3 | `FS_TRUNCATE` | 6.2 |
| 4 | TCP bind and connect by port | 6.7 |
| 5 | `FS_IOCTL_DEV` | 6.10 |
| 6 | Scopes for abstract unix sockets and signals | 6.12 |
| 7 | Audit logging flags on `landlock_restrict_self` | 6.15 (a third-party table says 6.13, UNVERIFIED) |
| 8 | `RESTRICT_SELF_TSYNC` (all threads) | 7.0 |
| 9 | `FS_RESOLVE_UNIX` (pathname unix sockets) | 7.1 |
| 10 | UDP bind, connect and send, quiet rule flag | UNVERIFIED |
| 11 | `RESTRICT_SELF_NO_NEW_PRIVS` | UNVERIFIED |
| 12 | Capability and namespace rules | UNVERIFIED |

The man page table stops at ABI 9. The kernel doc describes up to ABI 12 and gives no kernel numbers for any ABI.

## Kernels on target distributions

Fetched 2026-11-15. "Landlock built in and enabled" is UNVERIFIED for every row. The ABI is what the kernel version implies upstream. Distributions can backport or disable features, and landlock(7) says to test the ABI at run time.

| Platform | Kernel | Implied ABI | Source |
|---|---|---|---|
| Ubuntu 24.04 GA | 6.8 | 4 | ubuntu.com/kernel/lifecycle |
| Ubuntu 24.04 HWE (24.04.5) | 7.0 | 8 | ubuntu.com/kernel/lifecycle |
| Ubuntu 26.04 | 7.0 | 8 | ubuntu.com/kernel/lifecycle |
| Debian 13 | 6.12 series | 6 | debian.org/releases/trixie/release-notes/whats-new.en.html |
| RHEL 10.0 to 10.2 | 6.12.0 (el10 builds) | 6 | access.redhat.com/articles/3078 |
| Fedora 43, 44, 45 | 7.3.3 | 9 or higher | packages.fedoraproject.org/pkgs/kernel/kernel/ |
| Raspberry Pi OS | 6.18 appears in the docs' examples. Shipped branch UNVERIFIED | 6 or higher | raspberrypi.com/documentation/computers/linux_kernel.html |
| Amazon Linux 2023 | 6.1 (default), 6.12 and 6.18 builds exist. Which one an instance gets is UNVERIFIED | 3, or 6 or higher | docs.aws.amazon.com/linux/al2023/ug/compare-with-al2-kernel.html |
| GitHub hosted ubuntu-24.04 runner | 7.0.0-1017-azure | 8 | actions/runner-images Ubuntu2404-Readme.md |

Two points follow. Debian 13 and RHEL 10 sit exactly on ABI 6, so a higher minimum would exclude both. Ubuntu 24.04 on the GA kernel and AL2023 on its default kernel fall below 6.

## Why 6 and not 4

ABI 4 would admit Ubuntu 24.04 GA. It would also drop signal and abstract-socket scoping. A confined call could then signal its siblings and the node and connect to abstract sockets in the host network namespace.

The Linux backend in T4.3 also uses bubblewrap. A pid namespace and a network namespace would close both gaps without Landlock scopes. That needs unprivileged user namespaces, and Ubuntu 24.04 needs an AppArmor change for them (section 10.3). Whether that combination holds up is untested, so I do not lower the floor on a guess.

Raising it has no payoff yet. ABI 9 would close the `MAKE_SOCK` workaround and ABI 10 would add UDP, but only Fedora and Ubuntu 26.04 class kernels have them.

## What the user sees below ABI 6

The spec (plan.md, phase 4) says untrusted code is refused below the minimum. Three things follow from the code and the spec.

- Untrusted code. The executor refuses to start the child and exits 126. The node needs to turn that into a plain message that names the kernel's ABI and the required value.
- Trusted first-party tools. The spec runs them in the node or kernel, not in an executor child (C28 and section 10.4). They keep running. Today's Vessel has no such split, so this is a requirement on T4.3 and not current behavior.
- Mac and iPhone. Not affected. Their backend is Seatbelt.

## Follow-ups for the owner

1. Keep `REQUIRED_ABI = 6`. No Vessel edit is needed now.
2. T4.3 splits the check into trusted and untrusted paths, and returns a structured refusal with the observed ABI.
3. Optional. After T4.3 qualifies bubblewrap pid and net namespaces on Ubuntu 24.04, test whether ABI 4 could be the minimum with those namespaces required.
4. When the fleet is on ABI 9 kernels, handle `FS_RESOLVE_UNIX` and drop the `MAKE_SOCK` workaround. `_fs_rights` in `confine.py` stops at 16 bits and needs the new bit then.

## Open risks

- Distribution kernels may disable Landlock in the LSM list. UNVERIFIED for every row. T4.3 needs a runtime probe on each target.
- Kernel numbers for ABI 10 to 12 and the ABI 7 conflict are UNVERIFIED.
- I fetched pages through a summarizing fetch tool and did not read raw kernel source.

## Sources

- https://docs.kernel.org/userspace-api/landlock.html
- https://man7.org/linux/man-pages/man7/landlock.7.html
- https://docs.curiosity.ai/landlock-sharp/core-concepts/abi-versions (third party, ABI 7 conflict)
- https://ubuntu.com/kernel/lifecycle
- https://www.debian.org/releases/trixie/release-notes/whats-new.en.html
- https://access.redhat.com/articles/3078
- https://packages.fedoraproject.org/pkgs/kernel/kernel/
- https://www.raspberrypi.com/documentation/computers/linux_kernel.html
- https://docs.aws.amazon.com/linux/al2023/ug/compare-with-al2-kernel.html
- https://raw.githubusercontent.com/actions/runner-images/main/images/ubuntu/Ubuntu2404-Readme.md
