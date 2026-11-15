# Isolation and probes

This page lists the isolation each container and each tool call gets, and the probes that check it on a running
deployment.

## Containers

Every generated container runs with a read-only root filesystem, all capabilities dropped, `no-new-privileges`, and
limits on memory and processes.

| Container | Limits | Can reach |
|---|---|---|
| sandbox | `memory`, `cpus`, `pids` and `scratch` from its profile. `/tmp` is `noexec,nosuid,nodev`. | Only its `tools-<agent>` network, where only the executor answers |
| executor | 1 GB, 256 processes, 64 MB `/tmp`. Listens only on `front`. | The node and every sandbox |
| node | 2048 processes. Capabilities dropped. Its root is not read-only because the node writes its own data. | `front` and `outside` |
| egress | 512 MB, 128 processes | The outside network |

Each sandbox holds its own token, derived from the master secret, and nothing of the node.

## Each tool call

`grep`, `bash` and declared command tools run as separate processes. Before each one runs, it is confined:

| Mechanism | Effect |
|---|---|
| Landlock (filesystem) | Read and execute only the system directories (`/usr`, `/bin`, `/sbin`, `/lib`, `/lib64`, `/etc`, `/opt`), the agent folder, the `workdir`, the mounts, and a few devices (`/dev/null`, `/dev/zero`, `/dev/urandom`, `/dev/random`). Write only the turn's workspace. No `/proc`, so no view of other processes. Socket files cannot be created. |
| Landlock (network) | No TCP bind or connect. |
| Landlock (scoping) | No signals to processes outside the call, no abstract Unix sockets outside it. |
| seccomp | No SysV IPC or POSIX message queues (shared by everything running as one user), no IPv4 or IPv6 sockets. No `setsid` or `setpgid`, so every process stays in the call's session. |
| `no_new_privs` | Set before the rules apply. |
| Environment | Only `PATH`, `LANG`, `HOME`, `TMPDIR` and `VESSEL_OUTBOX`. |
| Time and output | The tool's timeout, then the whole process group is killed; output is capped (see [Limits](../reference/limits.md)). |
| Strays | Processes a call leaves behind are killed. |
| Files and disk | At most 4,096 open files; no file larger than the turn's disk limit; [disk limits](../build/workspaces-and-files.md#disk-limits) per turn and per user. |

If confinement cannot be applied, the call fails with exit code 126 and does not run. A sandbox whose kernel lacks
Landlock ABI 6 refuses to start.

`read`, `glob` and `list` run in the sandbox's server process instead. They are held to the same readable paths and
check where an opened file or folder really points, so links cannot lead them outside.

## Run the probes

`astralvessel check` probes a running deployment from inside its containers and prints `PASS` or `FAIL` for each
claim. `astralvessel up` runs the same probes every time it starts a deployment.

```bash
astralvessel check
```

It probes every agent in the deployment, with that agent's own mounts and limits, and exits non-zero if any probe fails.
Run it while the deployment is idle: a probe runs real tool calls and fills a turn's disk limit, and a user's tool call
arriving meanwhile can stop a probe part way, which shows as a failure. CI runs it against the example deployment on
every pull request. The scripts run inside the containers through `docker compose exec`, with their input on stdin, so
no secret appears in a process listing.

| Area | Probes |
|---|---|
| Sandbox network | no route to the internet, cannot resolve or reach the egress, cannot reach the model host directly, cannot reach the node API or its mailbox, cannot reach another agent's sandbox |
| Sandbox contents | holds no task or conversation store, holds no node secret or model key, holds its own token and not the master, root filesystem is read-only, scratch space is `noexec`, every mount is read-only |
| Tool calls | a background process does not outlive its call, a call cannot read another call's scratch, cannot list processes, cannot open TCP connections, cannot create a socket file, cannot use shared memory, cannot open UDP sockets, `glob` cannot climb out of the agent's data |
| Workspaces | calls in one turn share its workspace, a call cannot read another turn's workspace, a turn's workspace is gone once it ends, the sandbox refuses a malformed workspace key, a turn over its disk limit is stopped and emptied |
| Files | a file written to the outbox reaches the executor, the outbox refuses other types, big files and links, an outbox swapped for a link exposes nothing |
| Executor | no direct route to the internet, refuses a caller without its token, lists what its agents may run, keeps its keys private, refuses a forged ticket with a dotted code and runs nothing, refuses a tool the agent does not offer, refuses an unknown agent |
| Egress | refuses the cloud metadata address, private addresses and shared addresses, refuses odd ports, allows a public site on 443, refuses a model call without the token |

`check` also runs five node checks: the node reports ready, its descriptor names the owner's issuer, and its data directory, database file and signing key are private.

The example deployment with one agent runs 33 probes and 5 node checks. Each further agent adds about 25 probes. The disk limit probe is skipped, and says so, when `turn_disk` is too close to `scratch` to overfill one without filling the other.
