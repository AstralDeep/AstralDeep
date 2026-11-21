# Phase 0 result: Seatbelt enforce and the App Sandboxed build (T0.1 and T0.7)

## Answers

Seatbelt in enforce mode can confine an untrusted Python tool on macOS. A write outside the workspace, a read of the Keychains, and every network path except the loopback proxy are refused with EPERM.

The profile the app generates today is not enough on its own. It cannot spawn a child, it lets the tool read almost any file, and it leaves a few network paths open. Section "What phase 4 must add" lists the fixes.

The App Sandboxed Mac App Store build cannot host the node. It refuses to apply a profile to a child and refuses to run Python that is not inside the app bundle. The direct-download build can do all of it.

## What was run

The spike lives in the AstralProjection worktree at `apple-clients/Spikes/SeatbeltEnforce/` on branch `feat/106-p0-seatbelt`. Nothing in product code changed.

The Swift driver depends on `AstralCore` by path and calls the real `ComputerSandboxProfile.generate`. It compiles the real `MacComputerEgressProxy.swift` through a symlink. A 30-line shim supplies three small types that file expects from `MacComputerSandbox.swift`, which cannot build outside the app target.

The driver starts local fixtures (a TLS server, a plain TCP listener, a UDP echo) and runs `Tools/probe_tool.py` under `/usr/bin/sandbox-exec -f <profile>`. The tool attempts each operation once and prints only the outcome. File probes open a path and never read or print content.

Commands, from the worktree:

```
apple-clients/Spikes/SeatbeltEnforce/run.sh
apple-clients/Spikes/SeatbeltEnforce/AppSandbox/build_and_run.sh
```

Versions: macOS 27.2.1 (26B131), Xcode 27.2, Swift 6.4.2, Python 3.11.15 (uv standalone build, arm64), OpenSSL 4.1.0, SIP enabled. `man sandbox-exec` labels the command DEPRECATED.

The driver runs four rounds with the same tool. Enforce uses the profile as generated. Shadow uses the real shadow profile. "Strict-read" is a wrapper in the driver that rewrites the one line `(allow file-read* (subpath "/"))` into an allowlist and adds `(allow process-fork)`. A fourth round pairs the enforce profile with a proxy whose own policy is in shadow, so it forwards to the loopback fixture.

Two consecutive runs gave identical results after masking port numbers. Exit status 0 both times.

## T0.1 results

Each cell is the errno name the tool observed. `ok` means the call succeeded.

| Check | Enforce | Shadow | Strict-read |
|---|---|---|---|
| write inside the workspace | ok | ok | ok |
| write outside the workspace | EPERM | ok | EPERM |
| open `~/.zshrc` for write (protected home file) | EPERM | ok | EPERM |
| open `~/Library/Keychains/login.keychain-db` | EPERM | ok | EPERM |
| open `~/.ssh/id_ed25519` | ok | ok | EPERM |
| open `~/.zshrc` for read | ok | ok | EPERM |
| TCP to the proxy port | ok | ok | ok |
| TCP to another loopback port | EPERM | ok | EPERM |
| TCP direct to the TLS fixture, skipping the proxy | EPERM | ok | EPERM |
| TCP to `192.0.2.1:443` (documentation address) | EPERM | TimeoutError | EPERM |
| UDP DNS query to a loopback fixture | EPERM | ok | EPERM |
| UDP DNS query to `192.0.2.53:53` | EPERM | TimeoutError | EPERM |
| `connect` to `/private/var/run/mDNSResponder` | ok | ok | ok |
| listen on `127.0.0.1` | ok | ok | ok |
| listen on `0.0.0.0` | ok | ok | ok |
| TCP to `::1` on the proxy port | ECONNREFUSED | ECONNREFUSED | ECONNREFUSED |
| spawn `/bin/echo` as a child | EPERM | ok | ok |

The shadow column proves enforce is what refuses. The same operations succeed under `(allow default)`. A public address gives a timeout in shadow, which means the packet left the host toward a dead documentation address. No public service was contacted.

HTTPS through the proxy, over the loopback TLS fixture as upstream:

| Proxy egress policy | Tool saw |
|---|---|
| enforce (the real policy) | `HTTP/1.1 403 Forbidden`, decision `refuse loopback_destination` |
| shadow (forwards) | CONNECT established, TLS handshake verified against the fixture certificate, body `fixture-ok` |

The real proxy refuses a loopback upstream, so a loopback fixture can never be an allowed upstream. The enforce row still proves the tool reached the real proxy and the real policy ran. The forwarding row proves the tunnel works end to end under the enforce profile.

## Gaps in the real profile

- **No fork.** The enforce profile allows `process-exec*` but not `process-fork`. `os.fork` and `subprocess.run` both fail with EPERM. A tool that runs any helper process cannot work. Enforce is off by default, and the launcher's compile probe runs only `/usr/bin/true`, which needs no fork.
- **Reads are almost unrestricted.** The profile allows reads of `/` and denies only Keychains and exclusion patterns. `~/.ssh` and every dotfile are readable. The plan says the spike must refuse a read outside its policy, and the profile has no policy field for reads.
- **Loopback listening is allowed.** `network-bind` and `network-inbound` on localhost are open, so a tool can accept connections from other local programs. Binding `0.0.0.0` also succeeded, which the rule text did not predict.
- **The mDNSResponder socket is allowed.** A tool can talk to the system resolver, which queries DNS for it. Whether a name lookup leaves the host this way is UNVERIFIED. Only the socket connect was tested.
- **The loopback rule matches by port for both IPv4 and IPv6.** A listener on `::1` at the proxy's port number would be reachable.
- **Limits and time are not in the profile.** Memory, process count, open files and file size are outside what Seatbelt expresses. They need `setrlimit` in the launcher. UNVERIFIED here.
- **Hostname versus destination mismatch** at the proxy is untested, as the spec already says.

## T0.7 results

The probe is a small C program signed ad hoc once per entitlement set. Variants mirror the named files. `sandboxed` carries the keys of `AstralApp-macOS.entitlements`. `sandboxed_no_server` drops `network.server`. `direct` is `AstralApp-DirectDownload.entitlements` with the hardened runtime and no sandbox.

| Attempt | Sandboxed, with network.server | Sandboxed, no network.server | Direct download |
|---|---|---|---|
| (a) spawn Python bundled inside the app | yes | yes | yes |
| (a) spawn Python outside the bundle | no, EPERM | no, EPERM | yes |
| (a) spawn Python copied into the app container | no, EPERM | no, EPERM | not applicable |
| (b) `sandbox-exec` on a child | no, `sandbox_apply: Operation not permitted`, exit 71 | no, same | yes |
| (b) `sandbox_init` inside a child | no, rc -1 `Operation not permitted` | no, same | yes, rc 0 |
| (c) listen on loopback | yes | no, `bind` EPERM | yes |
| connect to a loopback port | yes | yes | yes |

Helper executables inside the sandboxed bundle:

- A helper signed with `app-sandbox` and no network key crashes at launch with SIGTRAP (EXC_BREAKPOINT) when the sandboxed parent spawns it. Run directly from a shell it starts, and `connect` and `bind` both fail with EPERM. The cause of the crash is UNVERIFIED.
- A helper signed with `app-sandbox` and `com.apple.security.inherit` starts and inherits the parent's network rights. It still cannot apply a profile.

A child of a sandboxed app inherits the app's sandbox and cannot be made stricter or different. Every tool would share one policy and `network.client` allows any host, so the proxy-only rule cannot hold.

The container result matters for the bundle design. Python copied into the container, as a first-launch download would place it, cannot execute. Only code inside the signed app bundle runs. This test copied the files with `ditto`. A real download adds a quarantine attribute, which was not tested and is UNVERIFIED.

**Answer.** No, the App Sandboxed Mac App Store build cannot host the node as designed. It can run bundled Python and listen on loopback with `network.server`, but it cannot confine tool children per call and cannot run a downloaded bundle.

The direct-download build does everything the node needs. It spawns Python from any path, applies profiles with `sandbox-exec` and `sandbox_init`, and listens on loopback. That build already is the one that hosts the governed-command host.

## Calls supported or reversed

- **C29, macOS row: supported with changes.** Seatbelt enforce works. The profile needs the additions below before it can serve as the executor backend. The VM fallback was not evaluated here.
- **C28, egress as a loopback proxy: supported.** The sandbox denied every other TCP, UDP and DNS path tested. Of the proxy's checks, only the loopback-destination refusal ran under the real policy. The allowlist held only `127.0.0.1`, so the hostname filter was never exercised.
- **C27 and O24, download the bundle on first launch: reversed for the App Sandboxed build.** The sandbox refuses to exec code outside the app bundle. It holds for the direct-download build, which the spec already names.
- **C31: unchanged.** Nothing here needed Docker.
- **Spec section 10.3, "whether the App Sandboxed build could host the node":** answered no.
- **Spec section 10.1 is stale on supervision.** It says no Python node supervisor exists in the Apple clients. `AstralCore/Sources/AstralCore/NodeHost/NodeSupervisor.swift` exists and is a real node-process supervisor. It launches the process, waits for health and restarts after a crash.

## What phase 4 must add (T4.2)

1. Add `process-fork` to the enforce profile, with a policy switch for tools that need no children.
2. Add a file-read allowlist to the policy document and generate it in place of the `/` read rule. The strict-read wrapper refused `~/.ssh` and `~/.zshrc` while Python still ran, using `/usr`, `/System`, `/Library`, `/private/var/db`, `/private/etc`, `/dev`, `/private/var/run`, the interpreter prefix and the tool and workspace directories.
3. Drop `network-bind`, `network-inbound` and the mDNSResponder rule unless the policy asks for them. The proxy does name resolution.
4. Pin the proxy rule to `127.0.0.1` so a `::1` listener on the same port number is out of reach.
5. Apply `setrlimit` limits and a wall-clock kill in the launcher, then test them in the conformance suite.
6. Give the conformance suite a proxy test mode that can forward to a loopback fixture. The real policy refuses loopback upstreams.
7. Test hostname-versus-destination mismatch at the proxy and a name lookup through mDNSResponder.
8. Refuse to select the App Sandboxed build as a node host and say why in the app.

## Open risks

- Seatbelt profile syntax is undocumented and the command is deprecated. The conformance suite is the guard against a macOS update that changes behavior.
- Results come from one machine, macOS 27.2.1, one Python build, and ad-hoc signing. A notarized, Developer ID signed build is UNVERIFIED.
- Nothing ran in CI or on a live device, as directed.
