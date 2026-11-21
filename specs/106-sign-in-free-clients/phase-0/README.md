# Phase 0 results

**Date:** 2026-11-15. Phase 0 of [plan.md](../plan.md) is done in code. Its two live checks run in phase 8
(T8.7), with the owner present. Every result below was reviewed against its brief before it was filed here.

Each spike is a throwaway prototype. Later phases take ideas and tests from it, never the code as is.

## Results

| Task | Result | Call |
|---|---|---|
| T0.1 Seatbelt enforce | [seatbelt-enforce.md](seatbelt-enforce.md). Enforce mode confines a Python tool to its workspace and a loopback proxy. Writes outside, Keychain reads and every other TCP, UDP and DNS path get EPERM | C28 and C29 supported. Today's profile needs fork, tighter reads and no loopback bind before it serves untrusted code |
| T0.2 VM fallback | [vz-fallback.md](vz-fallback.md). An Alpine guest under Virtualization.framework boots in 220 ms cold (308 ms with Python), runs a command in 2 ms warm and passes 12 confinement checks with a NAT positive control | C29 supported. Recommends Seatbelt as the default and the VM as the contingency for question 1 |
| T0.3 Landlock minimum | [landlock-minimum.md](landlock-minimum.md). Keep ABI 6 (Linux 6.12). Debian 13 and RHEL 10 meet it. Ubuntu 24.04 GA and Amazon Linux 2023 do not | Below ABI 6 untrusted code is refused, as the plan says. T4.3 must keep first-party tools running |
| T0.4 MXC | [mxc-policy-layer.md](mxc-policy-layer.md). MXC exists, is MIT licensed and reached 1.1.0. Its Linux backend is bubblewrap with no Landlock and no hostname filter | C28 and C29 supported. Borrow ideas, keep it as the Windows backend candidate |
| T0.5 Node bundle | [node-bundle.md](node-bundle.md). A relocatable macOS arm64 bundle builds in 330 s, weighs 1,486 MB (514 MB compressed) and imports the node, the Kernel binding, libmagic, poppler and spaCy from a moved copy | O24 supported for the direct-download build, with two blockers below |
| T0.6 Signed download | The hardened runtime part ran in T0.5: `import numpy` needs `disable-library-validation` under an ad hoc signature. The clean-account download runs in T8.7 | Live, deferred |
| T0.7 App Sandbox hosting | [seatbelt-enforce.md](seatbelt-enforce.md). No. The App Sandboxed build cannot put a child under a profile or run Python outside the app bundle | Confirms reading 3 in spec section 16: the downloaded node is for the direct-download Mac build only |
| T0.8 MCP adapter | [mcp-oauth.md](mcp-oauth.md). Against a local fixture on revision 2026-07-28: discovery from a 401, required S256, the registration order, the resource parameter and the RFC 9207 iss check all work, and no token reaches a log | C8 to C12, C16 and O16 supported |
| T0.9 HTTPS callback | Needs the associated domains entitlement and a real Mac and iPhone. Runs in T8.7 | Live, deferred |
| T0.10 A2A reader | [a2a-reader.md](a2a-reader.md). Reads every A2A 1.0.1 scheme, verifies a signed card against a pinned key and pins an unsigned card by hash and origin, requirements included | O12 supported. B97 holds: Deep has no scheme reader today |
| T0.11 Metadata drafts | [site-drafts/](site-drafts/) with a validator and tests | Feeds T0.13 and T2.15 |
| T0.13 Publish | Live on 2026-11-15: `/.well-known/apple-app-site-association` and `/oauth/client-metadata.json` on astraldeep.github.io, both HTTP 200 with no redirect | O16 met. Apple's CDN acceptance is checked in T8.7 |

## What later phases inherit

- **Phase 2.** The adapter's flow order and refusals. A server without S256 is skipped while an issuer mismatch aborts the flow. T2.6 decides whether that difference stays.
- **Phase 3.** The reader's canonical form keeps empty requirement entries. The a2a-sdk drops them, so an SDK signature does not cover a requirement change. Cards that match only the SDK form are reported as not covering requirements and are refused.
- **Phase 4.** The Seatbelt profile fixes in seatbelt-enforce.md. Poppler and libmagic must be built from source with `MACOSX_DEPLOYMENT_TARGET=14.0`, since the Homebrew bottles require macOS 27. The bundle needs a committed hash lock and a signing pass over every Mach-O.
- **Spec correction.** Section 10.1 says the Apple clients have no node supervisor. `AstralCore/Sources/AstralCore/NodeHost/NodeSupervisor.swift` is one, so T4.8 builds on it.

## For the owner

These need a decision, and none blocks work today.

1. Question 1, the Mac sandbox if `sandbox-exec` goes away. The recommendation is Seatbelt now and the VM as the named contingency. If the trigger fires, Mac-native governed commands would have no sandbox under the VM plan.
2. Landlock ABI 6 means a stock Ubuntu 24.04 kernel refuses untrusted code. The HWE kernel passes.
3. The spaCy model `en_core_web_lg` is 445 MB, a third of the bundle. A smaller model shrinks the download and changes PHI detection quality.
