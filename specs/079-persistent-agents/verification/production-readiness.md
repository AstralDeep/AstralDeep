# Feature 079 production preparation

This is a deployment preparation record, not production or release approval.
The owner authorized local commits, PRs and merges where the repository gates
permit them on 2026-09-05. The feature remains disabled by default. Production
promotion, signing, publishing and external service changes have not occurred.

## Runtime changes under qualification

- Persistent assignments retain owner instructions, source receipts, action
  outcomes, activity and task results in the guarded Plane 079.001 schema.
  Every physical action still goes through normal authorization and dispatch.
- Browser and background consumers coordinate before exchanging the same
  refresh credential. New offline grants store encrypted references to that
  canonical session rather than copies of a rotating token. Rotation must be
  durable before a caller receives its access token. Revocation, hard expiry,
  cancellation and ambiguous exchanges fail closed; uncertain credentials need
  fresh sign-in and consent. Legacy grants convert only on an exact live match.
- Reviewed public URLs have a bounded classifier representation that preserves
  and decodes identifying components. Longer, unreviewed URL prefixes receive
  no exception. Owner instructions and action arguments are rejected when
  sensitive; they are never silently rewritten.
- Source evidence is scanned for injection before truncation or redaction,
  including raw string leaves inside JSON. Existing redaction patterns and
  validated Presidio spans remove identifiers, followed by the unchanged full
  privacy gate. Evidence flags record redaction/truncation. Durable source
  digests describe sanitized evidence. Ledger hashes stay outside model prose.
- Public page reads preserve resource trailing slashes and resolve redirects
  relative to the actual resource. Every hop retains the approved HTTP/egress
  checks and a shrinking shared timeout budget.

No new third-party runtime dependency, primitive, schema revision or migration
is introduced by this hardening. The original additive 079.001 migration and
its recovery obligations remain in force.

## Candidate and local evidence

The owner subsequently authorized necessary draft publication after being
informed of the unmet pre-push ordering. Deep's actual tested feature is being
prepared as a diagnostic draft under that user instruction. This is not a
provider-native Constitution-X bootstrap approval, passing canonical evidence
verdict, independent review, or merge/release authorization. No policy or
protected setting is weakened. The exact failed parser inventory, source-bound
coverage and publication identities remain outside the candidate tree in
`build/079/release-preflight/`.

Current local runtime is `48f8ea5f`, image
`sha256:c565761d9048eb786deed5c04990db97c6a78c7fa7d3d68007078f1bdfca2d0c`.
It uses low reasoning effort for new bounded steps and retains legacy requests.
It passes 591 runtime file bindings, health/readiness/authentication denials,
both isolated production exit-78 checks, and unchanged dependency metadata.
A real owner-approved episode completed and incorporated a child result;
unchanged polling avoided additional model calls, and the completed result
survived an app restart. The test is stopped with no pending check or reservation
and all original limits respected. The result reports no available version in the supplied
fallback view. Actual release-change, mid-effect recovery, load/control-latency,
sensitive-action and every affected authenticated native-client scenario still
need their designated evidence. See `live-observations.md` for exact limits,
observations and the original stopped test.

Plane draft [#7](https://github.com/AstralDeep/AstralPlane/pull/7), at
`b20c8f3e06fc5302262fe3c8f049fa18a562e30f`, and Projection draft
[#14](https://github.com/AstralDeep/AstralProjection/pull/14), at
`07e5c90cb310c48c2315997f74dc8dd6f5fa22ea`, retain separate owner CI.
Plane's required checks pass. Projection's earlier `6c7a9e2` checks passed;
the terminal-guidance repair at `07e5c90` is undergoing updated hosted checks.
Projection's optional next-major Android lane is skipped by its existing
conditions. These results do not replace independent review or authenticated
Feature 079 evidence on the affected clients. Neither PR is merged.

`config/astral-composition.json` and its exact gitlinks identify the candidate
component commits. Use the current committed record in `results.md` for the
latest image, source manifest and test results. Older passing reports remain
historical evidence; they do not qualify later changed source automatically.

The first clean qualification snapshot at Deep `3c5696f2` passed 773 root CI
tooling tests with 92% coverage, 413 supplementary script tests and documentation
links. That snapshot predates the hardening above. Its full backend run exposed
stale schema/dispatch fixtures and additional failures undergoing baseline
classification. Final checks must use the later committed source.

Projection's exact `d2ce8be6` candidate passed the native Windows suite (1,080
tests, nine skips), C# helper tests (37), Node tests (28), and a five-case real
client.js browser fixture. The previously recorded Android gates passed. Its
native C# coverage is accepted only after validating the method/class/root
witnesses; the parser correction retains all thresholds and conflict denials.
Apple coverage and authenticated client/device flows are independent gaps.

## Reproducible build and cutover

1. Finish component owner gates, commit exact component revisions, and update
   Deep's declaration and gitlinks together. Run both composition validation
   and local component validation on a clean checkout.
2. Build from clean Git LF bytes. The earlier Windows worktree's CRLF composition
   bytes differed from the same commit's Linux bytes; do not relabel its image
   or hashes as a clean Linux build. Preserve the final source manifest, image
   identity, component wheels and `pip check` result.
3. Verify production configuration separately. Unset `ASTRAL_ENV` means
   production; missing/placeholder secrets or mock authentication must exit 78.
   Preserve real Keycloak, encrypted in-product LLM credentials, normal egress,
   audit, permissions and foreground-only sensitive approvals.
4. Quiesce durable writers and take a paired PostgreSQL/blob snapshot before an
   actual schema cutover. Rehearse restore in an isolated environment; a readable
   dump and matching copy hashes alone do not prove recoverability. Use the
   guarded Plane registry, never deployed ad-hoc SQL.
5. The local application already upgraded from 075 to 079 through normal startup
   after a paired snapshot. Ordinary subsequent 079 code rebuilds do not require
   another schema migration. Do not point the old 075 image at the upgraded DB.
6. Recreate the app with the built source and intended environment; restarting
   a container does not copy edited code or reread Compose environment changes.
   Bind `/healthz`, `/readyz`, installed component metadata and actual runtime
   hashes to the final image before authenticated testing.
7. Enable `FF_PERSISTENT_AGENTS` only in the qualified candidate/staging scope.
   Exercise owner creation, quiet unchanged polls, retained work after restart,
   revisions, pause/resume/stop/revoke, resource exhaustion and foreground
   approval denials through the real dispatcher. Complete all affected client
   and form-factor flows before claiming T030/T031 complete.
8. Verify the separately configured System LLM before unattended activation.
   Feature 054 keeps background billing/credentials separate from personal chat;
   never copy or fall back to a user's provider key. Missing model configuration
   must produce an honest hold without charging a model call.

The paired pre-cutover backup has now been restored in an isolated,
network-disabled PostgreSQL container with private restored file roots.
All 83 tables / 2,570 rows, 16 sequences, 467 catalog object/owner entries and
five roots / 1,335 files match. The restored database remains at 075.001;
application/IAM recovery and a new guarded 079 migration are separate checks.
The original backup and production data were not modified. See
[restore-rehearsal.md](restore-rehearsal.md) for evidence and limitations.

## Approved local live test

The owner approved reading only `https://www.python.org/downloads/` every 60
seconds, capped at eight model calls, 12 tool calls, 32,000 tokens and 300,000 ms
of active execution. Monetary cost is explicitly unpriced. No external changes
are allowed. The test assignment must be stopped afterward. A delegation depth
of zero respects the existing disabled recursive-delegation flag and narrows
the approved limit. Login alone was not treated as offline consent.

The test is now **stopped**. It verified real consent, public reads, model calls,
revisions, pause/resume/stop and durable history/spending across rebuilds. Its
latest result was rejected by the privacy gate; no completed baseline or finding
was accepted. Rejected content was discarded, so the exact offending content is
unknown. The remaining token allowance cannot reserve another planner call.
Lifetime usage is 6/8 model calls, 5/12 tool calls, 27,143/32,000 tokens and
108,817/300,000 ms, with no outstanding reservations. The stopped state and
unchanged usage survive a full app restart. No budget was extended or reset.

The test exposed and qualified recovery of proven-unstarted actions, early
production validation, bounded failure diagnostics and sufficient completion
capacity for reasoning models. New intents reserve 4,096 completion tokens;
existing intents retain their exact old cap and identity. These changes pass
their focused tests and independent review; they do not bypass privacy or allow
replay of begun work. The final clean persistent-agent/voice run passed 461 tests
with no failures or skips. See [live-observations.md](live-observations.md) and
`results.md` for exact source-bound evidence.

A read-only synthetic audit reproduced the existing broad date/DOB prefilter
rejecting an ISO public-release date; six other authored planner examples passed.
This does not identify the discarded live response. A further live diagnosis
requires another explicitly bounded owner-approved test. Successful baseline,
quiet polling, completed-result recovery and actual affected-client evidence
remain required before promotion.

## Dependency and build qualification

Plane's active CI pytest pin is updated from 8.4.2 to 9.1.1, and Plane/Projection
build-only setuptools pins and their exact hash constraints are updated to
83.0.0. Deep's isolated component build uses the same patched setuptools version.
Owner tests and package builds qualify those changes; no runtime dependency is
introduced. Projection's open legacy JavaScript alerts name a removed root lock;
the active tools omit nanoid/postcss-selector-parser and already contain the
patched humanfs 0.16.8. The read-only advisory assessment retains exact graphs and
primary links in `build/079/release-preflight/dependency-advisories.md`. No alert
was dismissed, and this bounded assessment is not a full final-image scan.

An exact runtime Python inventory audit subsequently found four unique open
advisories in `cryptography==48.0.1` and `ecdsa==0.19.2`. Source/API inspection
did not demonstrate an affected application path, but it is not an exhaustive
unreachability proof or release waiver. Presidio requires cryptography below
49, preventing a simple upgrade to the listed fixes; python-jose requires
ecdsa. The spaCy model could not be audited through PyPI, and OS/native-library
advisories were not scanned. All 145 installed distributions and dependency
metadata match between images from `0dfc768f`, `9913610f` and `7c8cd1b7`, allowing the
retained scan to be reused for that exact package set. See
[runtime-advisories.md](runtime-advisories.md) for primary advisory links,
version-range discrepancies and retained scanner identities. No alert or
dependency constraint was suppressed.

## Publication and protected production gates

Publish independently owned component PRs using their actual owner gates; do
not invent client producers for Plane or misrepresent a local report as a
provider attestation. Keep incomplete candidates draft. Deep and Plane main
currently require one approving review and code-owner review. Do not use the
authenticated account's administrator bypass to replace those requirements.

The Deep release bootstrap verifier loaded from main `34609998` requires an
already-existing same-repository draft PR/head, a clean advancing candidate and
a provider-native, exact-SHA/scope/expiry-bound lead approval. It cannot authorize
the first publication of a new branch as currently implemented. Ordinary local
release preparation or a reviewed default-policy/process resolution is needed;
generic PR authorization is not a fabricated provider approval. Bootstrap
authorizes no merge, production promotion or release.

At Deep `7c8cd1b7`, all three owner coverage producers pass the strict
changed-code gate (2,784/2,873 executable lines, 96.90%). The actual
`--repository-profile deep` pre-push parser still fails: that option narrows
coverage ownership, while canonical release evidence still requires backend,
web, Windows, Android, macOS, iOS, watchOS and documentation targets. Actual
same-candidate staging/platform/provider inputs are missing. The concrete
draft description, parser failure receipt and eight-target inventory are
retained under `build/079/release-preflight/`; no Deep PR or initial feature
push has occurred. Resolving this requires the missing canonical inputs or a
reviewed default-policy/process decision; a seed push or unrelated PR cannot
satisfy the existing bootstrap verifier.

The inspected provider configuration has no `release-readiness-staging`
environment and lacks `RELEASE_READINESS_ACTIVE`,
`RELEASE_EPHEMERAL_CREDENTIALS_READY`, `VOICE_WORKER_CLOSURE_APPROVED`,
`RELEASE_TRUSTED_BUILDER_SHA` and `RELEASE_TRUSTED_BUILDER_IDENTITY` repository
variables. Effective main rules also lack the required protected readiness
workflow binding. Installing flags alone would not establish trusted staging,
credentials, builder identity or independent review. These must be configured
and verified before the final protected release decision.

The read-only provider snapshots, exact default-policy bytes, PR drafts and
diagnostic missing-input inventory are retained in ignored
`build/079/release-preflight/`. They are preparation artifacts and cannot serve
as an external bootstrap approval or release authorization.
