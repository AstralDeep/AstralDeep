# Private Codex Security synthetic workflow

This opt-in developer CLI qualifies report plumbing using the actual reviewed
[Codex Security source](https://github.com/openai/codex-security/tree/f894f1ff64b1a332001f48593c1622b85701f9f3)
(retrieved 2026-10-28). It runs the upstream `scan --mock` profile, which creates
fictional findings without security analysis, source execution, model calls or
provider credentials. Every wrapper report and exported artifact is labeled synthetic.
`complete` means that this synthetic workflow completed; it never means that the
checkout received a clean security review. There is no remediation, publishing,
preview server, product integration, mandatory CI gate or release decision.

Paid scans are **unavailable**. At this pin, upstream's `--max-cost` reacts to cost
progress and does not provide a hard call/cost reservation before provider work.
The wrapper therefore admits exactly zero model calls and zero provider cost,
fixes the mock profile and model, rejects nonzero call/cost policy values, and
rejects `--egress-approve` before invoking the scanner. It never forwards API keys,
Codex login state, provider environment variables or ambient Node/Python hooks.
A future paid profile requires a reviewed admission and egress boundary; changing
an environment variable or sharing the host network cannot enable one.

## Locked developer runtime

The wrapper uses Python 3.11 standard-library APIs, system Git, and Bubblewrap on
Linux with working unprivileged namespaces. Its separate scanner root filesystem
contains the real Node package `@openai/codex-security` 0.2.0 and plugin 0.1.95.
`runtime-lock.json` pins the upstream Git archive, full upstream pnpm dependency
lock, workspace settings, package manifest, CLI launcher and compiled CLI bytes.
The image and pnpm tarball are pinned by digest in `Runtime.Dockerfile`. Node is
24.22.1 and pnpm is 11.25.0. The source-plugin mock profile uses the upstream
Python helpers; it does not build or claim the native plugin's universal package
or paid SDK runtime. No scanner dependency enters a product or CI manifest.

Build in a dedicated temporary directory, with no credentials or private files in
its Docker build context. The archive must come from the exact upstream commit;
the Dockerfile checks its SHA-256 before extracting or building it:

```sh
git clone https://github.com/openai/codex-security /tmp/codex-security-source
git -C /tmp/codex-security-source checkout --detach f894f1ff64b1a332001f48593c1622b85701f9f3
mkdir -m 700 /tmp/codex-security-runtime-build
git -C /tmp/codex-security-source archive --format=tar \
  --output=/tmp/codex-security-runtime-build/upstream.tar \
  f894f1ff64b1a332001f48593c1622b85701f9f3
cp tooling/codex-security/Runtime.Dockerfile /tmp/codex-security-runtime-build/Dockerfile
cp tooling/codex-security/runtime-lock.json /tmp/codex-security-runtime-build/runtime-lock.json
docker build -t codex-security-mock:reviewed /tmp/codex-security-runtime-build
```

Export that dedicated image into a new, empty directory on the Linux scanner host:

```sh
docker create --name codex-security-runtime-export codex-security-mock:reviewed
docker export --output /tmp/codex-security-rootfs.tar codex-security-runtime-export
mkdir -m 700 /tmp/codex-security-rootfs
tar --extract --file /tmp/codex-security-rootfs.tar \
  --directory /tmp/codex-security-rootfs --no-same-owner
docker rm codex-security-runtime-export
```

The export container is never started. Extract only the dedicated image just
built, into that empty directory; the exported filesystem includes system links.
Do not use the workstation root or copy host directories, credentials, Git state
or private data into it. Keep the artifact immutable while reviewing and running
it. Calculate its exact approval digest:

```sh
PYTHONPATH=backend python3.11 -c \
  'from pathlib import Path; import sys; from devtools.codex_security_review import tree_digest; print(tree_digest(Path(sys.argv[1])))' \
  /path/to/dedicated-rootfs
```

The operator must approve this artifact digest with `--runtime-sha256`. Before
launch, the wrapper recomputes the entire rootfs tree digest, checks its embedded
lock and reviewed file/build hashes, and refuses substitutions. The approval
trusts the operator's dedicated image acquisition/build, not an arbitrary
`--provider-command` or a version string returned by a child. The rootfs must be
separate from the source checkout. Namespace/preflight failures remain denials;
macOS and Windows cannot run this Bubblewrap profile.

## Source, environment and network boundaries

The policy inventories only tracked regular files under its include roots. It
excludes credentials, `.env`, uploads, data/tmp/logs/knowledge, clinical artifacts,
attachments, generated directories and component submodules. Untracked files and
Git history never enter the snapshot. File reads reject symlinks in every path
component, hard links, special files and source changes during copying. Git
inventory runs with a cleared environment, fsmonitor disabled and no hooks.

The child sees a minimal root made from the approved runtime, the materialized
snapshot mounted read-only at `/source`, a private output mount at `/output`, and
fresh temporary/home/process/device mounts. The original checkout and host root
are never mounted. All namespaces, including network, are unshared and all
capabilities are dropped. Outbound egress is denied by the network namespace;
there is no shared-network fallback or advisory allowlist. Both the host launcher
and sandbox environment use explicit allowlists. The source snapshot is removed
after the child exits.

## Use and reports

```sh
python3.11 backend/devtools/codex_security_review.py plan
python3.11 backend/devtools/codex_security_review.py run --approve \
  --runtime-root /path/to/dedicated-rootfs --runtime-sha256 <approved-tree-sha256>
python3.11 backend/devtools/codex_security_review.py verify \
  --run-dir backend/tmp/codex-security/<run-id>
```

The private report root must be inside a git-ignored checkout directory, owned by
the current user, mode 0700, and free of symlink traversal. Raw and sanitized files
remain private; they are never committed. An exclusive admission lock serializes
runs. The ledger reserves each run **before** starting the child; failure,
interruption or a wrapper crash cannot refund that run. The watchdog terminates
the process group when the bounded execution duration expires. Artifact parsing
has a byte limit. Status distinguishes complete, partial, interrupted, stopped
(admission exhausted), error, and preflight denial.

The adapter reads upstream's actual `scan-manifest.json`, `findings.json`,
`coverage.json` and `exports/results.sarif`. It validates producer/schema/mock and
scan identities, checks the manifest's findings/coverage digests, and maps coverage
completeness to complete/partial status. There is no invented `result.json`
contract. Sanitized SARIF and coverage files strip code/snippets and reject
outside, traversal or excluded paths. Fictional `mock/` paths are upstream test
examples, not paths or vulnerabilities found in AstralDeep. The report binds the
source commit plus actual snapshot content, policy, approved runtime, and sanitized
artifact digests; `verify` checks those records and the ledger receipt.

Exit codes: 0 complete/partial or verified; 1 verification failed; 2 denied;
3 admission stopped; 4 interrupted; 5 error. `preview` always refuses.

## Evidence and limits

Frozen fixtures in `backend/tests/fixtures/codex_security/` come from an actual
execution of this source pin's CLI, built with its frozen dependency graph. They
exercise the real artifact schema. Recording sandbox doubles test launcher
arguments, admission, copying, cancellation and error handling; they do not prove
kernel containment. `FIXTURE-PROVENANCE.md` records the exact acquisition and
commands. Required CI remains offline and unchanged.

The dedicated locked runtime built and its real upstream mock scan completed in
a network-disabled Linux container. A real Bubblewrap 0.8.0 probe in an ordinary
unprivileged task container was denied because the host does not allow the needed
namespaces. **Real supported-host Bubblewrap execution and filesystem/network
canaries remain unqualified.** Preserve that failure and keep the workflow's
containment acceptance blocked until a supported Linux runner passes. No paid
scan, actual vulnerability finding, deployment or release qualification is claimed.
