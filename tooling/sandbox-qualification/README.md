# sandbox-runtime qualification pilot

This opt-in Linux runner qualifies one harmless local scratch-file write and its denial
profile through `orchestrator/generated_lets_executor.py` and
`shared.process_supervision.ProcessSupervisor`. It does not change production launches,
authority, schema, contracts, component pins, flags, or release artifacts. A declaration,
unit-test double, CLI smoke, or unavailable result is not containment qualification.

## Reproducible installation

`manifest.json`, `package.json`, and lockfile v3 `package-lock.json` declare the complete
runtime graph: `@anthropic-ai/sandbox-runtime@0.0.78`,
`@pondwader/socks5-server@1.0.10`, `commander@12.1.0`, `node-forge@1.4.2`, and
`zod@3.25.76`. Each tarball has an exact npm registry URL and SHA-512 integrity. The
manifest also binds the lockfile's SHA-256. Retrieved 2026-10-28 from the npm registry;
the upstream [pinned installation instructions](https://github.com/anthropic-experimental/sandbox-runtime/blob/12a2be92c110cbc3c19584c4c8387e7e754c8179/README.md#installation)
use npm, and this runner uses the supported [npm ci](https://docs.npmjs.com/cli/v11/commands/npm-ci/)
installation path.

With explicit download consent, the runner acquires bounded tarballs through
`shared.external_http` using verified TLS, no redirects, no credentials, and no inherited
proxy configuration. It verifies every digest before seeding a fresh private npm cache,
then runs `npm ci --offline --ignore-scripts --no-audit --no-fund` with the committed
manifests. No registry request, lifecycle script, unpinned `npx`, ambient `NODE_PATH`, user
npm configuration, or pre-existing module tree supplies the installation. Preparation
and support probing both run the actual installed `node .../dist/cli.js --help`; a
missing dependency fails before an effect can start. Failed installations are removed.

Candidate supported profile: native **Ubuntu 24.04, linux/amd64 or linux/arm64**, a
non-root user, Node >=22.12, npm >=10.9, distribution `bubblewrap`, `socat`, and `ripgrep`,
unprivileged user/network/PID namespaces, and the pinned vendored `apply-seccomp` helper.
The host must already permit these primitives. Do not weaken AppArmor, Docker seccomp,
or user-namespace policy, enable privileged containers, or select SRT's weak nested mode.
Record the exact host/kernel and installed tool versions alongside candidate evidence.
Unsupported hosts report exit 4 (`unavailable`) before receipt consumption or effects.
Python 3.11 must include `tarfile.data_filter`; older patch releases without safe filtered
extraction report `python_data_filter_unavailable` instead of using an unsafe fallback.

The actual Debian 12/Linux 6.1 x64 pilot at candidate `88069940` failed the scratch-only
write policy. The pinned vendor deliberately permits writes in private overlays created
by read denial, including `/tmp`, `/dev/shm`, and the hidden session directory. The
corresponding host paths remained absent. This is a failed, non-promotable policy
qualification; an authorized scratch write succeeding does not override it.

Each launch has a separate 30-second startup admission. Hanging payloads have a bounded
two-second execution interval after their readiness witness. Descendant witnesses bind
the namespace PID to one host descendant's current ancestry, start ticks, namespace,
expected executable, exact arguments, and opaque per-launch identity. Ambiguous matches
fail closed. Namespace-local PID numbers alone never identify a host process.

From the repository root with declared Python/component dependencies installed:

```bash
export PYTHONPATH=backend
python -m orchestrator.sandbox_qualification --probe-only \
  --allow-artifact-download --artifact-cache-dir /private/operator/srt-cache
```

Acquisition is a separate opt-in preparation step. Repeating the command without
`--allow-artifact-download` must succeed entirely from verified cached tarballs. Ordinary
CI does not download this graph or contact a third-party service.

## Immutable filesystem and network policy

Each invocation creates a fresh `0700` session directory containing the `0400` SRT profile and
immutable effect source. A supplied existing session directory, including a symlink, is
refused before writes or permission changes. Source, secret, and profile creation is
exclusive with `O_NOFOLLOW`; per-mode specs use exclusive private temporary files and
atomic replacement, so artifact links cannot redirect writes or chmod an unrelated file. Scratch is a separate `0700` directory, never beneath the denied
session tree. Explicit scratch/session overlap and any write deny covering scratch are
rejected. The session tree and SRT's `/tmp/claude` convenience remain write-denied;
SRT's deny-over-allow precedence is preserved.

Reads are denied at `/` and restored only for explicit runtime trees, the vendored
seccomp helper, effect source/spec, and scratch. Writes are allowed only to scratch.
The network allowlist is empty with deny-all and strict allowlist enabled; Unix sockets
and local binding remain disabled. The child receives a fixed minimal environment and
only its standard pipes. Missing primitives and upstream degradation warnings are hard
failures, with no unsandboxed fallback.

## Permit plan/bundle and outstanding dispatch integration

Normal dispatch registration and owner/tool-policy permit issuance for this qualification
tool remain unimplemented and unqualified. The plan/bundle path preserves standard
executor verification of supplied permits; it does not establish an ordinary dispatch
route. That route and its real authorization evidence remain a separate merge hold.

Prepare the exact wire arguments for an eventual qualified owner-bound authorization adapter:

```bash
python -m orchestrator.sandbox_qualification \
  --write-permit-plan /private/operator/permit-plan.json
```

This creates a new `0600` file containing the tool ID, scope, random qualification ID,
and arguments for every mode; it starts no effect and mints no receipt. A registered,
qualified normal authorization adapter must obtain one standard LETS protected permit per
listed mode before this operator flow is usable.
Do not reuse a permit, sign inside the runner, or put private keys in the bundle. Store
the returned permits privately as:

```json
{
  "qualification_id": "<the exact 32-character ID in the plan>",
  "permits": {
    "authorized": "<standard permit object, not a string>",
    "read_secret": "<standard permit object>",
    "write_outside": "<standard permit object>",
    "network_connect": "<standard permit object>",
    "unix_ipc": "<standard permit object>",
    "symlink_escape": "<standard permit object>",
    "exhaust_output": "<standard permit object>",
    "hang": "<standard permit object>",
    "spawn_descendant": "<standard permit object>"
  }
}
```

After qualifying that ordinary authorization route, configure the normal LETS executor
environment and run:

```bash
python -m orchestrator.sandbox_qualification \
  --permit-bundle /private/operator/permits.json \
  --artifact-cache-dir /private/operator/srt-cache \
  --record-log /private/operator/qualification-log.jsonl
```

Every permit is checked and claimed by the existing executor for audience, owner, scope,
arguments, digest, expiry, authority state, and replay before its mode starts. Bundle
entries are consumed once and bound to the planned ID; the bundle cannot authorize an
argument change. `--permit-file` also requires its planned `--qualification-id` and runs
only the authorized smoke; a successful partial run has status `probe`, not `passed`.
A full `passed` record requires all modes in order and no injected runtime/supervisor.
Exit codes are 0 for passed/probe/plan, 2 usage, 3 failed, and 4 unavailable.

## Independent synthetic Linux qualification

The focused suite's existing isolated LETS fixture authority creates fresh Ed25519
keys, a signed trust manifest, and independent replay/authority stores. These fixtures
are test-only and pass genuine signed permits through the existing receipt verifier;
there is no synthetic runtime authorization mode. To require the actual SRT profile,
prepare the default cache on the non-root supported runner, then execute:

```bash
PYTHONPATH=backend python -m orchestrator.sandbox_qualification \
  --probe-only --allow-artifact-download
ASTRAL_SRT_REQUIRE_QUALIFICATION=1 \
ASTRAL_SRT_QUALIFICATION_RECORD=/private/operator/actual-srt-record.json \
PYTHONPATH=backend python -m pytest \
  backend/tests/test_sandbox_runtime_qualification.py::test_full_qualification_passes_on_supported_host \
  backend/tests/test_sandbox_runtime_qualification.py::test_descendant_cleanup_with_genuine_srt_pid_namespace -q
```

The required flag makes missing primitives fail the test instead of skipping it. The
record is written before result assertions and retains support checks, the full
dependency-lock digest, artifact/profile/receipt digests, and audit correlation. Bind it
to the exact candidate SHA and host/tool identities outside the candidate tree. Retain
receipt-denial/replay and lifecycle test results separately; no unsupported-host or
injected-runtime result substitutes for this record.

Descendant readiness is observed while the child lives. The parent maps its namespace
PID to a unique host process through a revalidated supervisor ancestry, `/proc` start
ticks, a distinct PID-namespace inode and deeper `NSpid` chain. Cleanup checks the captured
host PID plus creation identity; a missing or ambiguous mapping fails closed. A
namespace-local PID is never used directly for host kill/proc checks. The dedicated real
SRT regression requires a genuinely separate PID namespace. Both `await_effect` and the
runner finally terminate/join session-owned supervised processes before an interruption
propagates; unrelated supervisor entries are preserved.

Parent-side checks independently inspect authorized output digests, denied secret reads,
absence of writes across `/tmp`, `/var/tmp`, `/dev/shm` and the session boundary, zero
accepted loopback/Unix-socket connections, unchanged symlink targets, bounded output,
deadline termination and descendant cleanup, plus upstream degradation markers. Fixed
fixtures use only synthetic data and local controlled listeners. Out of scope remain
control-fd updates, persistent agents, interactive terminals, Windows/macOS, weak nested
containers, AppleEvents, provider egress, and production integration. Preserve private
session/permit artifacts according to operator policy; only sanitized records belong in
review evidence.
