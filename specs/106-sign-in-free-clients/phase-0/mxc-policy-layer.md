# Microsoft Execution Containers as a policy layer

Task T0.4. Written 2026-11-15. Research only, no code run.

## Call

Borrow ideas. Skip MXC as the shared layer for macOS and Linux. Keep it as the leading candidate for the later Windows backend.

This supports C29: one Astral policy document and one interface, with a backend per platform. It also supports C28, since MXC confirms that hostname filtering belongs in a proxy.

## What MXC is

Sources are the Microsoft Windows developer blog of 2026-10-07 and github.com/microsoft/mxc.

- The repository calls it "Microsoft eXecution Container". The blog calls it Microsoft Execution Containers. It sandboxes agents, their tools and model-generated code.
- The blog says it is generally available. GitHub releases show v0.8.0 and v0.9.0 as prereleases, v1.0.0 on 2026-10-07, v1.0.1 on 2026-10-17 and v1.1.0 on 2026-11-15.
- The license is MIT, copyright Microsoft Corporation (LICENSE.md).
- The repository is 3.1k stars, created 2026-02-06 and active today.
- SDKs. Rust crate `mxc-sdk`, NuGet `Microsoft.Mxc.Sdk` and npm `@microsoft/mxc-sdk`. A JSON executor binary exists for other callers. Building needs Rust 1.93 and Node 24 or newer.

| Platform | Default backend | Others |
|---|---|---|
| Windows 11 | Process container (AppContainer) | Session container, WSLc, Windows Sandbox (experimental), MicroVM (experimental), Hyperlight (experimental) |
| Linux x64 and arm64 | Bubblewrap | LXC, Hyperlight |
| macOS arm64 and x64 | Seatbelt, needs macOS 15 or later | None |

## What the policy language expresses

The schema covers containment, process, file system, network and UI. Modes are enforcement, learning and permissive. Learning mode and the activity report work on Windows process containers only.

| Astral field (section 10.3) | MXC |
|---|---|
| File reads | Read-only path list. Deny list wins over read-only, which wins over read-write |
| One writable workspace | Read-write path list |
| Allowed hosts | No hostnames. Linux accepts IP and CIDR rules only. Seatbelt has no per-address rules. Both accept a loopback proxy endpoint and leave hostname filtering to that proxy |
| Limits (memory, processes, open files, file size) | Not in the bubblewrap or Seatbelt pages. Windows process container page not read, so UNVERIFIED there |
| Time | `process.timeout` |

MXC also has things Astral's five fields lack. Deny paths that survive symlinks, a UI and clipboard block, an ingress default and a learning mode.

## What it cannot do for us

- Linux uses bubblewrap and a seccomp filter on socket families. The bubblewrap page does not mention Landlock. Vessel's Landlock rules, scoping and rlimits would stay outside MXC, so the layer would not replace `confine.py`.
- Hostname egress is absent on both Unix backends. The proxy in C28 still does that work. MXC's proxy-only mode on Linux matches the design in section 10.4.
- The Linux backend needs unprivileged user namespaces. Network rule modes also need slirp4netns, iptables and `nf_conntrack`. Ruleless deny needs only `--unshare-net`.
- Seatbelt egress through a proxy port is cooperative, the same limit as section 10.4.
- Seatbelt needs macOS 15 or later. Our Mac profile code already exists in `ComputerSandboxProfile.swift`.
- Required telemetry events ship on eligible Windows builds. They are a no-op on macOS and Linux. Whether that fits a clinical deployment is an owner question and UNVERIFIED beyond the README.
- Native Rust and Node assets would join the bundle (C27). The Python node would call a Node, .NET or Rust SDK or the JSON executor binary.

## Windows, later

Section 10.3 says AppContainer filters network by capability and Job Objects limit resources only. MXC's process container wraps AppContainer for this, so adopting it for Windows saves writing that layer. It does not add hostname filtering there either. I read no Windows backend page, so its limits, ACL handling and minimum OS build are UNVERIFIED. The Codex docs list Windows 11 build 26100.9278 or 26200.9278 for their MXC use (research D123, not re-read).

No Windows code exists in this spec's early phases, so the decision can wait.

## Why not adopt now

- The Linux backend would give up Landlock and the executor limits we already have.
- It adds a new Rust and Node dependency for two platforms where we already have working or planned backends.
- Release history is three weeks past GA. Several backends carry experimental labels.
- It would not remove the egress proxy or the conformance suite (C30).

## Ideas to borrow

1. Deny paths that resolve symlinks first and win over all allows.
2. Proxy-only network mode with a loopback endpoint and no other route.
3. Learning mode as a way to write a policy from observed use.
4. Failing closed at start when a mask or mount cannot be applied.
5. A stable, versioned JSON schema for the policy, which T4.1 can model.

## Follow-ups

1. T4.1 names the Astral policy document and can mirror MXC's field names where they match.
2. Re-evaluate MXC's process container when the Windows backend phase starts.
3. Read `docs/backends/process-container/` then. Check limits, licensing of native assets and the telemetry terms.

## Open risks

- GA is recent. Backends and schema may still move.
- I read through a summarizing fetch tool. I read the README, the bubblewrap page and the Seatbelt page, and did not clone the repository or build anything.
- The `LICENSE.md` result was one fetch. Third-party notices for native assets are unchecked.

## Sources

- https://blogs.windows.com/windowsdeveloper/2026/10/07/microsoft-execution-containers-policy-driven-containment-for-ai-agents/
- https://github.com/microsoft/mxc
- https://raw.githubusercontent.com/microsoft/mxc/main/README.md
- https://raw.githubusercontent.com/microsoft/mxc/main/LICENSE.md
- https://raw.githubusercontent.com/microsoft/mxc/main/docs/backends/bwrap/bubblewrap-backend.md
- https://raw.githubusercontent.com/microsoft/mxc/main/docs/backends/seatbelt/seatbelt-backend.md
- https://api.github.com/repos/microsoft/mxc and its releases endpoint
