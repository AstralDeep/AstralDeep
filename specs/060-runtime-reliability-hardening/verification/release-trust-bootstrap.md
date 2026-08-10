# Release Trust Bootstrap Record (T120)

**Feature**: 060 Runtime Reliability and Release Readiness
**Branch**: `060-runtime-reliability-hardening`
**Recorded**: 2026-07-16 (America/New_York)
**Status**: T120 open. Pre-merge protections exist, but checkpoint 1 (protected
default-branch landing), checkpoint 2 (caller/required-check activation), publisher
review separation, disposable-tag rollback compatibility, trusted-only signer-tag
creation, and durable orphan cleanup remain open.

This record contains no secret values, tokens, or credential material. All
identities below are public repository metadata.

## Trust model

Per the 2026-07-16 owner decision: no repository-scoped GitHub App,
installation token, or custom token broker exists or is planned. Every
mutating release-path job uses the built-in short-lived `GITHUB_TOKEN`
behind a protected environment with job-scoped permissions. Local evidence
preparation is diagnostic only (`protected_release_authorization: false`);
release authorization comes exclusively from the protected-CI decision job.

## Configured on 2026-07-16 (pre-merge layer)

### Protected environments

| Environment | ID | Required reviewer | Self-review |
|---|---|---|---|
| `release-publisher` | 18290265039 | `armstrongsam25` (user id 16158892) | **noncompliant / activation blocked**: live setting permits self-review, while T120 and the publisher require a distinct approver |
| `release-evidence-exception` | 18290265203 | `armstrongsam25` (user id 16158892) | **blocked** (`prevent_self_review: true`, layering the environment gate over the registrar job's own `requester_login != github.actor` refusal) |

Both environments were created with `can_admins_bypass: true` (GitHub default
for repository admins); the registrar and publisher jobs additionally verify
their own invariants in-job. The publisher now rejects the requester as reviewer,
so its current single-reviewer/self-review configuration cannot produce a valid
publication receipt and MUST be repaired rather than bypassed.

### Protected debt ledger

- Ref: `refs/heads/release-evidence-debt`
- Root commit (orphan, README only): `7f6d609caa118c1cfceba2e6bba85dfec794b2a3`
- Ruleset `19078547` "protect release-evidence-debt ledger" (active): blocks
  deletion and non-fast-forward pushes. Appends remain possible only as
  fast-forward descendants; the registrar workflow adds exactly one previously
  absent `debts/<uuid>.json` or `resolutions/<uuid>.json` path per commit.

### Release tag protection

- Ruleset `19078549` "protect release tags" (active) on `refs/tags/v*`:
  blocks deletion, update, and non-fast-forward. Creation remains possible
  (the environment-approved publisher creates `v<release_version>` exactly
  once); no bypass actors.

  **Activation blocker (verified 2026-08-01):** disposable publication and
  official failure/cancellation rollback both create a strict `v*` tag and then
  require deletion of that exact state-owned tag. This active no-bypass ruleset
  rejects the deletion. The publisher now reports and verifies cleanup failure
  instead of swallowing it, but T120/T128 cannot pass until a reviewed policy and
  tag-namespace design supports safe disposable rollback without weakening
  immutable official tags.

  **Signer-boundary blocker (verified 2026-08-01):** the ruleset does not restrict
  creation of a new `v*` tag. The shipped v0.3.0 updater accepts the Sigstore
  workflow identity `release-windows.yml@refs/tags/<tag>` but cannot pin that
  workflow file's commit digest. A repository writer could therefore tag a commit
  carrying different workflow bytes and dispatch that tag directly, outside the
  publisher's protected byte checks. T120 cannot activate until release-signing
  tag creation is enforceably limited to a trusted authority, or the deployed
  verifier/trust design is migrated. Broadly bypassing the ruleset for all GitHub
  Actions workflows is not an acceptable repair.

### Pre-existing

- Ruleset `15015805` "stop push to main" (active) protects the default branch.

## Workflow identities (authored, awaiting protected landing)

The six workflows are tracked on the candidate branch and contract-tested by
`backend/tests/test_release_workflows_060.py`:

| File | `name:` | Authority |
|---|---|---|
| `.github/workflows/release-readiness.yml` | `release-readiness` | candidate jobs read-only; `protected-decision` emits and attests the decision |
| `.github/workflows/release-trusted-builder.yml` | `release-trusted-builder` | id-token/attestations write only (manifest attestation) |
| `.github/workflows/release-evidence-exception.yml` | `release-evidence-exception` | registrar job: environment-gated contents write (ledger append only) |
| `.github/workflows/release-windows.yml` | `Release Windows client` | exact tag-ref dispatch bridge: contents/actions/attestations read + id-token write, no mutation |
| `.github/workflows/release-windows-publisher-controller.yml` | `release-windows-publisher-controller` | read-only verification job; calls the reusable publisher with capped contents/actions write and attestations/deployments read |
| `.github/workflows/release-windows-publisher.yml` | `release-windows-publisher` | intended tracked release-mutation job behind `release-publisher`; contents/actions write only, with attestations/deployments read |

## Remaining bootstrap steps

**Checkpoint 1 — protected default-branch landing.** Merge the reviewed
candidate (PR #143) to `main`, landing verifier
(`scripts/validate_release_evidence.py`), coverage policy
(`scripts/check_changed_coverage.py`), all three contract schemas, and the six
workflow files at one commit. Then record:

- `RELEASE_TRUSTED_BUILDER_SHA` (repo variable) = the main commit pinning both
  `release-trusted-builder.yml` and `release-readiness.yml`; it is the manifest
  trust root and the required signer digest for the final decision
- `RELEASE_TRUSTED_BUILDER_IDENTITY` (repo variable) = the builder's expected
  certificate identity
- `RELEASE_BRIDGE_WORKFLOW_SHA256` (repo variable) = SHA-256 of the
  `release-windows.yml` bridge bytes at that commit

**Checkpoint 2 — activation.** After the candidate branch rebases onto the
checkpoint-1 root and the protected staging credential issuer and durable
host-side cleanup controls described below pass their live
mint/expiry/revoke/cancel/restart/cleanup exercise, set both repository
variables `RELEASE_READINESS_ACTIVE=true` and
`RELEASE_EPHEMERAL_CREDENTIALS_READY=true`. The latter is unset/false by
default and MUST remain so until that exercise passes. Then add
`release-readiness / protected-decision` to the default-branch ruleset and add
`release-readiness` to `ci.yml` `publish.needs` (the tracked comment marks the
line).

Before checkpoint 2, configure at least one publisher reviewer distinct from
the requester and set `prevent_self_review: true`. Also resolve and live-prove
both release-tag contradictions above: safe disposable rollback and trusted-only
creation of tag-ref signing identities. A forced job
cancel, runner loss, or timeout can interrupt Actions cleanup, so the proof must
include orphan detection/recovery rather than treating `if: always()` as a
durable finally block.

**Staging prerequisites — activation blocked:** the qualifying matrix MUST stay
inactive until the persistent staging host and its separate protected
credential-issuer route exist. To activate later, provision:

- The labeled persistent host with Docker and non-loopback TLS ingress
  reachable from every producer runner. Record its exact canonical runner name
  in the non-sensitive repository variable `ASTRAL_STAGING_RUNNER_NAME`. The
  workflow validates that value before deployment and passes the validated
  name to cleanup as a job output; cleanup does not depend on staging-
  environment secrets or approval. The host MUST also carry a unique
  scheduling label that cannot select another runner; a shared
  `[self-hosted, astral-staging]` label is not sufficient by itself.
- A distinct TLS control route to a protected-policy-pinned staging credential
  issuer. It must authenticate the exact workflow/run/attempt/producer with
  GitHub OIDC, mint only request-scoped `user` identities or short-lived access
  tokens in the isolated staged Keycloak realm, and provide idempotent per-job
  revocation plus protected `revoke-all` before namespace teardown.
- Explicitly mapped stage-only infrastructure inputs: the staging endpoint,
  digest-pinned PostgreSQL/Keycloak/schema-baseline images, bounded bind ports,
  and per-run isolated database/Keycloak credentials generated for that one
  namespace. Namespace cleanup MUST revoke those credentials and destroy their
  backing data. A candidate speech worker may receive only a per-run narrow
  speech grant or access a separately protected bounded speech proxy; the
  durable provider key and operator runtime environment MUST never enter a
  candidate image, container, process, artifact, output, or log. The reusable
  caller MUST NOT use `secrets: inherit`.
- A protected host-side boot and periodic orphan reaper, independent of the
  GitHub Actions job DAG. It must recognize only canonical namespaces for this
  repository, bind them to run/attempt and an expiry, invoke issuer
  `revoke-all`, remove the corresponding Compose namespace/data, and be safe to
  repeat. Activation proof MUST cover workflow cancellation, runner process
  loss, host restart, a stale lease, and successful idempotent recovery. An
  `if: always()` cleanup job is defense in depth, not a durable finally block.

Durable repository probe tokens, provider keys, runtime environment files,
Windows/Android access tokens, or shared browser/Apple usernames and passwords
are forbidden activation prerequisites. Plaintext workflow outputs or
artifacts are not substitutes for the issuer.
The issuer is staging authentication infrastructure only; it has no release,
attestation, exception, debt-ledger, or publication authority.

The exception invariant is already explicit in tracked code: local and candidate
jobs cannot authorize an exception, the registrar runs behind its environment,
and `scripts/validate_release_evidence.py` refuses `--decision-output` outside
the `protected-decision` job context. The corresponding publication invariant is
a **target, not yet an enforced repository-wide fact**. The tracked publisher is
environment-gated and the protected decision is exact-identity bound, but the
unrestricted fresh-`v*` creation path can still dispatch changed tag-ref signer
bytes accepted by the legacy updater. T120 remains open until trusted-only tag
creation or a verifier migration closes that route and the other blockers above.

## Parked: the protected bridge signer (2026-08-07)

`cc15b033` (PR #168) replaced `.github/workflows/release-windows.yml` — the
feature-060 protected bridge — with the feature-039 DIRECT tag-push release
path. Windows client **v0.4.0 shipped on it the same day**. Owner-directed and
deliberate.

- **Recoverable at:** `git show d3cb9a51:.github/workflows/release-windows.yml`
- **Revival requires:** restoring that file, re-pinning
  `vars.RELEASE_BRIDGE_WORKFLOW_SHA256` to its SHA-256, and clearing the T120
  blockers listed above. Restoring the file also re-arms the four parked
  contract tests automatically — they skip on the absence of the `bridge-sign`
  job, so no test edit is needed.
- **Note for anyone reviving it:** the bridge as merged could never have run.
  Its job guard (`d3cb9a51` `release-windows.yml:42`) and its required
  `test "$GITHUB_REF" = "refs/tags/$TAG"` step (`:57-60`) are mutually
  exclusive, and T069 (the fresh-runner release proof) was never executed. The
  parked design is code-shaped and unexercised; revival means fixing it, not
  just restoring it.

### Requirements the shipping path does NOT meet

- **FR-048** (`spec.md:497-499`) — signing "MUST consume those exact archived
  unsigned bytes … without rebuilding." The direct flow rebuilds with
  PyInstaller inside the signing job, so nothing binds the shipped bytes to
  bytes any test matrix exercised.
- **Constitution v2.9.0 (`:870-884`)** — candidate-modifiable workflows MUST
  hold no release-mutation authority. The direct flow holds `contents: write`
  because it publishes the Release itself.
- **Candidate-code isolation** — the signing job builds from the tagged tree, so
  tagged repository code executes in a job holding `id-token: write`. Anyone who
  can push a `v*` tag can therefore have arbitrary code from that tag run in the
  signing job and be signed under the release identity; `v*` tag creation is
  unrestricted (see above).

These are **recorded divergences** pending either a constitution/spec amendment
or bridge revival. They are NOT resolved by this note.

### Controls still in force on the direct path

Hash-locked build + signing toolchain (`windows-client/requirements-release.lock.txt`,
installed `--require-hashes`, enforced by
`backend/tests/test_python_ci_supply_chain_060.py::test_windows_release_installs_only_hash_locked_build_and_signing_deps`
and `windows-client/tests/test_release_lock_060.py`); SHA-pinned third-party
actions; a default-branch-only manual-dispatch guard; a tag ↔
`astral_client.__version__` agreement check; and an in-job sigstore self-verify
against the exact client-pinned identity, asserted by
`test_release_windows_signing_identity_surface_matches_the_shipped_client`.

A green release-tooling lane therefore now means **five controls enforced, four
parked** — not nine enforced.

### Known-broken chain left by the swap

`release-windows-publisher.yml:97-98, 619-624` still hash-compares
`release-windows.yml` against a protected template and dispatches it with
`inputs[tag]` / `inputs[readiness_run_id]` / `inputs[decision_artifact_id]` —
inputs the direct workflow does not accept. That publisher chain is unreachable
at runtime today. It is gated off and is deliberately not repaired here; enabling
`RELEASE_READINESS_ACTIVE` without reading this note would walk into it.
