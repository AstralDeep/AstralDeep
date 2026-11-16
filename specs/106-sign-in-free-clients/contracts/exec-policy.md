# Execution policy `astral.exec-policy/v1`

One policy describes everything an untrusted node tool may touch. A macOS Seatbelt backend and a Linux backend (bubblewrap, Landlock and seccomp) both render it. The node, the Vessel command line and the Apple clients refuse to run untrusted code when no backend can enforce the policy.

Untrusted code means Vessel `bash` and declared tools, draft agents, stdio MCP servers and Mac governed commands. Existing roster v2 stores and node data are untouched. This contract adds no event type, no roster field and no stored data.

Reference code: `reference/exec_policy.py`. Generator: `reference/generate_exec_vectors.py`. Vectors: `vectors/exec-policy-v1.json`. LETS-CJ/1 comes from the preserved mesh reference.

## 1. Policy

A JSON object with exactly these fields. Any other field is `policy.unknown_field`. There is no `listen` field in this version, so no child may accept inbound connections.

| Field | Meaning |
|---|---|
| `v` | The integer 1 |
| `reads` | 0 to 64 absolute paths the child may read and execute |
| `workspace` | One absolute path the child may read and write but never execute |
| `hosts` | 0 to 64 objects `{"host", "port"}` the child may reach through the egress proxy. Empty means no network at all |
| `limits` | `{"memory_bytes", "processes", "open_files", "file_bytes"}` |
| `time` | `{"wall_ms"}` |

### Paths

A path is a string of at most 4096 UTF-8 bytes. It starts with `/`, is not `/` itself, has no empty, `.` or `..` component, has no trailing slash and holds no control character (below U+0020 or U+007F). Anything else is `policy.path_invalid`. 
Paths are canonical. A caller passes the real path with every symlink resolved and, on macOS, the case and Unicode form the disk reports, because Seatbelt matches resolved paths. The reference cannot read a disk, so it does not check this. The executor's policy loader (it reads the disk) refuses a policy whose stated spelling differs from its canonical form. Reads need not be NFC. The disk's own form is the canonical one, and the reference keeps the stated bytes.

On `darwin` the reference compares paths after NFD normalization and case folding, because APFS is case and normalization insensitive. That fold applies to repeated read roots, to read roots against the workspace and to every data directory check. On any other platform comparison is by bytes. The platform is an input of parsing, and the vectors say which one a case uses.

`reads` is sorted by the UTF-8 bytes of each path and holds no repeats. A read root that equals, contains or sits inside the workspace is `policy.path_invalid`, because the workspace is never executable.

### Hosts

A `host` is a lowercase ASCII DNS name of at most 253 characters with at least two labels. Each label matches `[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?`. The last label is neither all digits nor `0x` followed by zero or more hex digits (the WHATWG "ends in a number" rule), so no IPv4 literal in any base passes. Wildcards, trailing dots, underscores and Unicode (write punycode) are refused. A `port` is the integer 80 or 443. Any other value is `policy.host_invalid`.

`hosts` is sorted by `host` then `port` and holds no repeats. A violation is `policy.host_invalid`. The egress proxy allowlist is exactly this list. Neither backend carries host names, because the proxy enforces them.

### Limits

| Field | Minimum | Maximum |
|---|---|---|
| `memory_bytes` | 16777216 | 68719476736 |
| `processes` | 1 | 4096 |
| `open_files` | 16 | 65536 |
| `file_bytes` | 4096 | 1099511627776 |
| `time.wall_ms` | 1 | 86400000 |

`processes` 1 means the child may not fork. A value outside its range is `policy.limit_out_of_range`, or `policy.time_out_of_range` for `wall_ms`. All values are integers. A boolean is not an integer.

## 2. Parsing and validation order

A verifier checks in this order and returns the first failure. A failure in an earlier field wins over any failure in a later field, including an unknown key inside a later object. An implementation that checks in another order disagrees with the vectors.

1. Text or bytes parse as strict JSON (LETS-CJ/1 rules: no duplicate keys, no floats, integers within 64 bits, valid UTF-8). Otherwise `policy.malformed`. The input need not be canonical.
2. The root is an object. Otherwise `policy.malformed`.
3. Any key outside the six fields is `policy.unknown_field`. This check comes before the missing key check.
4. A missing field is `policy.malformed`. `v` that is not the integer 1 is `policy.malformed`.
5. `reads`. A wrong type is `policy.malformed`. More than 64 entries is `policy.too_many_entries`, before any entry is read. Then each entry in order: type, then path rules, then sort and uniqueness against the entry before it.
6. `workspace`. Type, then path rules, then the overlap rule against every read root.
7. `hosts`. A wrong type is `policy.malformed`. More than 64 entries is `policy.too_many_entries`. Then each entry in order: it must be an object, any extra key is `policy.unknown_field`, a missing key or a wrong type is `policy.malformed`, then the host rules, then the port rule, then sort and uniqueness against the entry before it.
8. `limits`. It must be an object. An extra key is `policy.unknown_field`, a missing key is `policy.malformed`. Then in the table order, each value must be an integer (`policy.malformed`) and in range.
9. `time`. The same rules, for the single key `wall_ms`.

## 3. Digest

The policy digest is `sha256:` followed by the lowercase hex SHA-256 of the policy's LETS-CJ/1 bytes. It appears in every launch log line. Two policies that differ only in whitespace have one digest.

## 4. macOS Seatbelt profile

`sandbox-exec -f <profile>` runs the child under the text this section fixes. The renderer takes the policy, the user's home directory (an absolute normalized path) and the proxy port. A policy with `hosts` needs a proxy port from 1 to 65535. A missing or bad value is a caller error, not a policy error. Line order and spelling are part of the contract.

Strings use the escaping of `ComputerSandboxProfile.quote`: wrap in double quotes, replace `\` with `\\`, then `"` with `\"`. Nothing else is escaped, and paths hold no control characters.

```
(version 1)
(deny default)
(import "system.sb")

(allow process-exec* (subpath "<root>"))              base roots, then read roots, then /bin and /sbin
(allow process-fork)                                  only when processes is above 1
(allow process-info* (target self))
(allow signal (target self))
(allow signal (target children))
(allow sysctl-read)
(allow file-ioctl)

(allow file-read* (subpath "<base root>"))            one line per base root, then one per read root
(allow file-read* file-write* (subpath "<workspace>"))

(deny process-exec* (subpath "<workspace>"))
(deny file-map-executable (subpath "<workspace>"))
(deny mach-lookup (global-name "com.apple.dnssd.service"))
(deny file-read* (subpath "<home>/Library/Keychains"))
(deny file-read* (subpath "/Library/Keychains"))
(deny file-read* (subpath "/System/Library/Keychains"))

(allow network-outbound (remote ip "localhost:<proxy port>"))   only when hosts is not empty
```

The text ends with one newline and the annotations on the right are not part of it. A blank line separates sections, and the last section and its blank line are absent when `hosts` is empty. The base roots, in order, are `/usr`, `/System`, `/Library`, `/private/etc`, `/private/var/db/dyld` and `/dev`.

Rules the text encodes:

- Later rules win, so the deny lines follow every allow.
- Executing is allowed only under the base roots, the read roots and `/bin` and `/sbin`. `/bin` and `/sbin` are exec-only and add no read access. Linux matches this, since its base roots hold `/bin` and `/sbin` and nothing else outside the read roots can execute.
- The workspace refuses `execve` and executable mappings (`file-map-executable`), so a library written there cannot load. A dylib in a read root still loads.
- There is no unfiltered `mach-lookup` allow. The import supplies the services Python and `/bin/sh` need, and the one deny closes the DNS service so the child cannot resolve names itself. A service the child needs later is a named `global-name` in a new policy version. Any profile text with an unfiltered `mach-lookup` or `process-exec*` allow is a defect, and the reference tests refuse it.
- Signals reach the child itself and its children only.
- Nothing allows `network-bind`, `network-inbound` or the mDNSResponder socket. The proxy resolves names.
- SBPL accepts only `localhost` or `*` as a remote host name, so the one outbound rule covers `127.0.0.1` and `::1` on the proxy port. The proxy binds both addresses on that port, which keeps any other listener off it.
- Residual risk: a sandboxed child can read the arguments and environment of same-user processes through `kern.procargs2`, and no profile rule closes it. The node never puts a secret in any process's argv or environment, and the conformance suite checks the node launcher.
- Memory, `processes` above 1, `open_files`, `file_bytes` and `wall_ms` are outside what Seatbelt expresses. The launcher applies them.

## 5. Linux plan

The renderer takes the policy and, when `hosts` is not empty, the host path of the egress Unix socket. It returns this object. Every key is always present.

| Key | Content |
|---|---|
| `bwrap` | The bubblewrap arguments, in order |
| `landlock` | `abi` 6, `exec_read`, `device_files`, `workspace`, `workspace_rights`, `tcp_connect`, `tcp_bind`, `scopes` |
| `workspace_noexec` | Always `true`. The workspace is mounted `noexec`, so `execve` and executable mappings of its files fail, including `ld.so <file>` |
| `egress` | `null`, or `{"listen": "127.0.0.1:3128", "socket": "/run/astral/egress.sock"}` |
| `seccomp` | `deny_syscalls`, `deny_fork`, `fork_syscalls`, `inet_stream_sockets`, `deny_socket_types` |
| `rlimits` | `RLIMIT_AS`, `RLIMIT_NOFILE`, `RLIMIT_FSIZE` |
| `watchdog` | `wall_ms` and `processes`, enforced by the monitoring parent |

The `bwrap` arguments are, in this order:

1. `--unshare-user --unshare-pid --unshare-net --unshare-ipc --unshare-uts --unshare-cgroup --die-with-parent --new-session --clearenv`
2. `--ro-bind-try P P` for each base root, in order. The base roots are `/usr`, `/bin`, `/sbin`, `/lib`, `/lib64` and `/etc`. Linux has no `/opt` root, because a host does not control it.
3. `--ro-bind P P` for each read root.
4. `--bind W W --proc /proc --dev /dev` with `W` the workspace.
5. `--bind <host socket> /run/astral/egress.sock`, only when `hosts` is not empty.
6. `--chdir W`

Landlock grants `exec_read` (the base roots then the read roots) read and execute, the four `device_files` (`/dev/null`, `/dev/zero`, `/dev/urandom`, `/dev/random`) read and write, and the workspace every right except execute and `MAKE_SOCK`. It handles TCP bind and connect with no bind grant, and allows connect only to port 3128 when `hosts` is not empty. It sets both scopes at ABI 6. The ABI never drops below 6.

Seccomp denies `deny_syscalls`, which are the SysV IPC calls, the POSIX message queue calls, `setsid` and `setpgid`. It allows AF_INET and AF_INET6 sockets only when `inet_stream_sockets` is true, and then only `SOCK_STREAM`. Datagram and raw sockets stay denied. `deny_socket_domains` is `["AF_UNIX"]`, so the child cannot create Unix sockets. The egress forwarder runs outside the child's filter, and the child reaches it over TCP port 3128. When `deny_fork` is true (`processes` is 1), `fork_syscalls` are denied and `clone` is denied unless it creates a thread.

`RLIMIT_AS`, `RLIMIT_NOFILE` and `RLIMIT_FSIZE` come from `memory_bytes`, `open_files` and `file_bytes`. The `watchdog` holds the wall clock and, for `processes` above 1, the process count the parent enforces over the child tree.

## 6. Backend selection

The selector takes a validated policy, platform facts and the node data directory (an absolute normalized path). The facts are an object with exactly these keys, all required except `mode`. A key outside this table is refused. A probe that errors reports its fact as absent, which is `false` or 0, and a null or text value is refused.

| Key | Meaning |
|---|---|
| `platform` | `darwin`, `linux` or any other string |
| `mode` | `enforce` (default) or `shadow` |
| `landlock_abi` | The kernel's Landlock ABI, 0 when absent |
| `bwrap` | bubblewrap is installed |
| `userns` | Unprivileged user namespaces work |
| `sandbox_exec` | `/usr/bin/sandbox-exec` exists |
| `app_sandboxed` | The process runs in an App Sandbox container |

A bad facts object or data directory is a caller error, and the caller refuses to run the tool. The result is `{"ok": true, "backend": "seatbelt"}`, `{"ok": true, "backend": "linux"}` or a refusal. A refusal is `{"ok": false, "code", "message"}` with `reason` and its extra keys when the code is `exec.backend_unavailable`. The first row that matches wins.

| Order | Condition | Code and reason |
|---|---|---|
| 1 | A read root or a base root of the platform lies inside, equals or contains the data directory, or the workspace equals or contains it, or lies inside it anywhere except under `<data>/exec/` | `exec.policy_overlaps_data` |
| 2 | `mode` is `shadow` | `exec.shadow_refused` |
| 3 | Linux and `landlock_abi` below 6 | `exec.backend_unavailable`, reason `landlock_abi`, with `observed` and `required` |
| 4 | Linux and no bubblewrap | `exec.backend_unavailable`, reason `bwrap_missing` |
| 5 | Linux and no user namespaces | `exec.backend_unavailable`, reason `userns_denied` |
| 6 | Linux | `linux` |
| 7 | macOS and no `sandbox-exec` | `exec.backend_unavailable`, reason `sandbox_exec_missing` |
| 8 | macOS and App Sandboxed | `exec.backend_unavailable`, reason `app_sandboxed` |
| 9 | macOS | `seatbelt` |
| 10 | Any other platform | `exec.backend_unavailable`, reason `platform_unsupported`, with `platform` |

The workspace may lie inside the data directory only under `<data>/exec/`, and `<data>/exec` itself does not qualify. Comparisons use the platform's fold from section 1, so on `darwin` a case or Unicode variant of the data directory still overlaps.

## 7. Codes and messages

Validation codes: `policy.malformed`, `policy.unknown_field`, `policy.path_invalid`, `policy.too_many_entries`, `policy.host_invalid`, `policy.limit_out_of_range` and `policy.time_out_of_range`.

Run codes: `exec.backend_unavailable`, `exec.agent_server_unsupported`, `exec.shadow_refused`, `exec.policy_overlaps_data`, `exec.time_limit`, `exec.memory_limit`, `exec.process_limit`, `exec.file_limit` and `exec.launch_failed`.

Every message names the missing piece in plain words. The exact texts, including the per-reason texts with `{observed}`, `{required}` and `{platform}` slots, are in `constants.messages` of the vectors. A client shows the message and never the code alone.

## 8. Vectors

`vectors/exec-policy-v1.json` holds four parts.

- `constants`: the base roots, limit ranges, codes, ports and messages.
- `validation`: named cases with a `policy` object or `raw` text, an optional `platform` and the expected `code` (`ok` when valid, with the `digest`).
- `profiles`: valid policies with their context (`home`, `proxy_port`, `egress_socket`), the digest, the exact Seatbelt text and the Linux plan. Cases cover `processes` 1 and 2, empty and non-empty `hosts` and a path that needs quoting.
- `selection`: policies, facts and data directory with the exact result. Cases cover every refusal reason, both backends, the shadow refusal, data directory overlap (case and Unicode variants, read roots inside and containing, the `exec/` rule both ways) and the order of checks.
- `context_errors`: facts or data directories the selector refuses as caller errors, such as an unknown key or a null probe result.

## 9. Versioning

A change to a rule, a base root, a limit range or the profile text raises the format version. This document, the vectors and the reference module change together, and `generate_exec_vectors.py --check` and the reference tests pass before a change merges.
