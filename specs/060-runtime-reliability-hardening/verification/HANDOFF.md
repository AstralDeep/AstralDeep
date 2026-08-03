# Spec 060 — live-trial handoff (what's blocked on hardware/accounts)

**Recorded**: 2026-07-16/17 (America/New_York). Branch `060-runtime-reliability-hardening`, PR #143.

At the 2026-07-17 merge checkpoint, all **code** deliverables for US8 + Phase-12
authoring were committed and the release-tooling CI lane was green. The current
2026-08-01 feature-065 checkout adds uncommitted release-trust hardening and
reopens T120; the sections explicitly dated 2026-08-01 supersede the older
activation claims. What remains is **live verification** that needs protected
configuration, a staging host, native hardware, or interactive sign-ins. This
file is the punch list.

---

## 0. coverage-gate — DEFERRED ("circle back", 2026-07-17)

The feature-029 single-lane `coverage-gate` (diff-cover on the `test` job's
`backend/coverage.xml`) is temporarily **non-blocking** (`continue-on-error:
true` in `ci.yml`). Why: the 060 feature deliberately splits coverage across
lanes (in-image `test`, host `release-tooling-tests`, perf, integration,
per-client), so a single-lane diff-cover under-measures. The honest first step
is already committed — test modules are excluded from the diff (they run in
other lanes) — which lifts it from 77% to ~82% locally (higher in CI, where all
tests pass; my local measure was suppressed by ~23 clean-postgres env
failures). The residual to reach 90% is genuine changed-product-line coverage,
dominated by the 17k-line `orchestrator.py` (~63%), plus `scheduler/api.py`
(70%), `knowledge_synthesis.py` (78%), `llm_gate.py` (75%), `history.py` (83%).
To circle back: run the honest diff locally
(`diff-cover backend/coverage.xml --compare-branch origin/main --fail-under 90
--exclude '*/tests/*' 'backend/tests/*' '*/conftest.py'`), write targeted tests
for those files' uncovered changed lines, then delete the `continue-on-error:
true` line to re-enable enforcement. The spec's cross-lane authority
(`scripts/check_changed_coverage.py`, T125) is the eventual ≥90% gate once the
readiness workflow is active.

---

## A. Staging matrix — DEFERRED (updated 2026-08-01)

**Decision:** the dedicated persistent staging host will not be provisioned
(cert provider down + team opted out). The `release-readiness` matrix therefore
stays **INACTIVE** (`RELEASE_READINESS_ACTIVE` unset) and tasks **T111 / T125 /
T128** are deferred until an external staging host exists. A GitHub-hosted
runner alone can't host the shared endpoint across the matrix — its runners are
ephemeral and torn down between jobs, so the Windows/Android/Apple/web producer
jobs (each on a fresh runner) couldn't reach a stack deployed in `stage-deploy`.
The `stage-deploy` / `stage-cleanup` jobs run on the dedicated
`[self-hosted, astral-staging]` host and expose the resulting shared namespace
through the external `ASTRAL_STAGING_ENDPOINT` for the hosted producer runners.
The local pre-push diagnostic (`scripts/prepare_release_evidence.py`) needs none
of this and works today.

**Publisher activation is also blocked (2026-08-01).** T120 is open: the live
`release-publisher` environment has only the requester account as reviewer and
permits self-review, while the protected publisher requires a distinct reviewer.
In addition, active ruleset `19078549` forbids deletion of every `refs/tags/v*`
tag and has no bypass actor, so the required disposable/failure rollback cannot
remove the strict SemVer tag it creates. The workflow now serializes publisher
runs, binds the exact current-run approval/deployment, and refuses hidden cleanup
failure, but it MUST remain inactive until a distinct reviewer plus
`prevent_self_review` and a safe disposable-tag/immutable-official-tag policy are
configured and live-proven. The same ruleset currently leaves new `v*` tag
creation unrestricted; because the deployed v0.3.0 verifier pins only the
tag-ref workflow identity and not its workflow digest, a repository writer could
otherwise dispatch changed signer bytes directly at a fresh tag outside the
publisher. Trusted-only release-signing tag creation (or a verifier/trust
migration) is therefore a separate required proof. Do not grant a broad GitHub
Actions ruleset bypass.

**Security correction (2026-08-01):** the historical setup instructions below
that used durable repository probe, bearer, or shared login secrets are
superseded and MUST NOT be followed. The protected caller now inherits no
repository secrets and fails closed unless both `RELEASE_READINESS_ACTIVE=true`
and `RELEASE_EPHEMERAL_CREDENTIALS_READY=true`. The latter MUST remain unset or
false until the external credential issuer described below is implemented and
live-verified. The tracked workflow also contains a literal unconditional
failure as the first `stage-deploy` step, before runner binding, registry
access, checkout, or candidate execution. Removing that stop is forbidden
until every activation item below is complete.

To revisit later: stand up a persistent external Linux host with a public HTTPS
name, configure only non-credential deployment inputs, implement the protected
request-scoped credential issuer, and complete the activation proof below:

1. **A Linux host** (Docker + Docker Compose v2, ≥4 CPU / 8 GB, outbound HTTPS to
   github.com + ghcr.io). Root or docker-group access.
2. **Register it as a repo runner** with the `astral-staging` label:
   - GitHub → repo Settings → Actions → Runners → New self-hosted runner →
     follow the shown `./config.sh --url https://github.com/AstralDeep/AstralDeep
     --token <reg-token> --labels astral-staging` then `./run.sh` (or install as
     a service). I can generate the registration token via
     `gh api -X POST repos/AstralDeep/AstralDeep/actions/runners/registration-token`
     and hand it to you if you want.
   - Add a second, unique host label and change both `stage-deploy` and
     `stage-cleanup` to require it. Prove that no other runner can satisfy the
     full label set; the shared `astral-staging` label alone can schedule
     cleanup on the wrong host.
3. **A public TLS hostname** that resolves to the runner and terminates HTTPS in
   front of port `${STAGING_BIND_PORT}` (the staging stack binds
   `127.0.0.1:${STAGING_BIND_PORT}`). Any of: a UKY-issued cert + reverse proxy,
   a `*.ai.uky.edu` name, or a Cloudflare Tunnel / Tailscale Funnel. The endpoint
   must contain **no** userinfo/query/fragment and must not be loopback.
   → Give me: that hostname (e.g. `https://astral-staging.ai.uky.edu`).
4. **Protected deployment configuration** supplied outside candidate-controlled
   code and logs:
   - `ASTRAL_STAGING_ENDPOINT` = the public HTTPS URL from step 3
   - `STAGING_POSTGRES_IMAGE`, `STAGING_KEYCLOAK_IMAGE`,
     `STAGING_SCHEMA_BASELINE_IMAGE` = **digest-pinned** images
     (`host/repo@sha256:…`); the schema-baseline is a 057.001 image
   - non-secret endpoint, port, realm, and candidate metadata required to bind
     the exact run and staging namespace
   - no durable probe token, access token, password, runtime credential file,
     or shared producer identity in repository secrets
5. **External request-scoped issuer** on a separately protected TLS route. It
   must authenticate GitHub OIDC claims for the exact repository, workflow,
   run, attempt, job, candidate SHA, and environment; mint a one-use-JTI,
   narrow-scope, short-TTL, non-refreshable lease; create a distinct just-in-time
   Keycloak identity or equivalent short bearer for each producer; and support
   both per-job revoke and cleanup-time `revoke-all`. Candidate-controlled code
   must not receive the issuer's own authority or any durable staging/provider
   credential.
6. **Keycloak**: the staging realm import fixture is committed
   (`backend/tests/fixtures/runtime_reliability_060/staging/keycloak-realm.json`,
   PKCE, no secrets). Browser and Apple producers must still exercise real
   Authorization Code + PKCE with their just-in-time identities.
7. **Host-side orphan reaper**: install a protected boot-time and periodic
   service outside the Actions DAG. It must remove only canonical Astral
   staging namespaces whose run/attempt lease is expired or revoked, call the
   issuer's idempotent `revoke-all`, destroy namespace data, and emit
   content-free audit evidence. `if: always()` is not sufficient after manual
   cancellation, runner loss, or host restart.
8. **Activation proof**: live-test mint, exact-claim rejection, expiry,
   one-use replay rejection, per-job revoke, cleanup-time `revoke-all`, manual
   workflow cancellation, runner-process loss, host restart, stale-lease
   recovery, and idempotent reaping.
   Only after that proof may `RELEASE_EPHEMERAL_CREDENTIALS_READY=true` be set;
   enable `RELEASE_READINESS_ACTIVE=true` separately when the staging topology
   and protected evidence path are ready.

**Minimum to get started:** the external host, public HTTPS hostname, a unique
runner label, and designated owners for the separately protected OIDC-bound
issuer and host-side reaper. Do not enable either readiness variable merely
because the host or endpoint exists.

---

## B. Mac tasks (Apple — you pick these up)

> **2026-07-17 status — items 1–3 DONE on the Mac.** (1) The producers are
> swift-formatted and the strict recursive lint is clean. (2) Both producers
> compile-verify (`build-for-testing` green for AstralAppUITests on the
> iOS 26.5 sim and AstralWatchTests on the watchOS 26.5 sim). (3) The macOS
> first-login "status text never transitions" bug is ROOT-CAUSED AND FIXED:
> `.accessibilityElement(children: .ignore)` mints a generic AXGroup even on a
> Text, and macOS AXGroups drop AXValue — every phase read as an empty value.
> The contract now lives directly on the status Text (a real AXStaticText);
> macOS first-login is 4/4 locally. Two sibling instances of the same
> platform-semantics class were also fixed: the continuity semantic matcher
> now accepts label OR value (macOS puts static-text content in the VALUE),
> unblocking the first-ever macOS deterministic relaunch result (20/20, mean
> 1.64 s), and the system-IME composer contract is explicitly skipped on
> macOS. Deterministic portions of items 4–5 are recorded in
> `us3-continuity.md` / `us5-apple-first-login.md`; the live-authenticated
> portions still need the provider-configured account below.

Xcode 26.6 (build 17F113), iOS/watchOS 26.5 runtimes. From repo root:

1. **swift-format the new producer files** (blocks the `swift-format` required
   gate; I authored them on Windows and cannot run swift-format):
   ```
   xcrun swift-format format -i --recursive --configuration apple-clients/.swift-format \
     apple-clients/AstralApp/AstralAppUITests/ReleaseEvidenceUITests.swift \
     apple-clients/AstralWatchTests/ReleaseEvidenceTests.swift
   git add -A && git commit -m "style(060): swift-format release-evidence producers"
   ```
   Then re-run the lint to confirm clean:
   `xcrun swift-format lint --strict --recursive --configuration apple-clients/.swift-format apple-clients/AstralCore apple-clients/AstralApp apple-clients/AstralWatch`
2. **Compile-verify the new producers** (authored blind on Windows — never
   compiled): build the `AstralAppUITests` and `AstralWatchTests` targets and
   fix any compile errors in `ReleaseEvidenceUITests.swift` /
   `ReleaseEvidenceTests.swift` (watch for: `XCTAttachment` availability on the
   watchOS test bundle, `ASWebAuthenticationSession` element access on macOS,
   springboard consent-alert handling). The files auto-join their targets via
   the synchronized-group mechanism (no pbxproj edit was needed).
3. **Debug macOS `LLMFirstLoginUITests`** — the real open bug. On GitHub
   `macos-26` runners the status text never transitions (all of "Check your
   provider credentials", "Provider unavailable", "Unable to confirm;
   reconnecting" absent; the 10s watchdog measured 12.4s > the 11.5s ceiling).
   iOS only misses the 250 ms ack window under VM latency; **macOS looks like a
   real fixture/app-side bug** — the us5 record only ever showed iOS UI
   automation passing, so macOS UI automation may never have passed. Reproduce
   with `-only-testing:AstralAppUITests/LLMFirstLoginUITests -destination
   'platform=macOS'` and trace the `--astral-ui-test-first-login` fixture states
   on macOS.
4. **T078** — 30 first-login trials on iOS *and* macOS, record timing
   distributions in `verification/us5-apple-first-login.md`.
5. **T057 / T102 / T124 Apple portions** — 20 continuity trials per Apple client,
   lifecycle sequences, and the final Apple validation lanes; record in the
   `us3-continuity.md`, `us7-operability.md`, and `final-apple.md` records.

Note: the two watchOS/AstralCore apple-ci **infra** failures (simulator device,
codecov upload) were fixed in `.github/workflows/apple-ci.yml` (deterministic
`astral-watch-060` device by UDID; staged codecov path) — verify they pass on
the next apple-ci run; they are not Swift-code issues.

---

## C. Windows host — reached its ceiling here

- Source suite + deployment-profile logic: **green** (108 profile/integrity
  tests; full `windows-client/tests` suite run locally).
- The **0.4.0 frozen EXE cannot be built on this host**: it needs Python 3.11
  (host has 3.10/3.8 only), and per the spec the release EXE is **built once in
  CI** by `build-windows-candidate.yml` and consumed unmodified by the readiness
  matrix + bridge/publisher. Local rebuilds are explicitly out of the trust
  path. So T069's fresh-EXE proof runs on the CI Windows runner, not here.
- One-time sign-in still pending for any Windows *client* live trial (the app
  had no stored session).

---

## D. One-time interactive sign-ins (for local live trials)

- **Web (T057/T102 web)**: the Chrome extension is connected, but driving a live
  chat turn via browser automation was unreliable here — the WS authenticates
  and `ws.chat_message` dispatches (audit confirms `ws_register` /
  `session_resumed` / `chat_message` all *success* for the real GLM-configured
  user `58e0d4ff…`), but form submits didn't consistently transmit and the one
  turn that dispatched produced no completion within minutes. Best driven by a
  human with a fresh interactive session, or re-run once the staging endpoint is
  the target. The stack itself is proven (the two day-old chats render).
- **Android**: the debug APK is built and installed on the running emulator
  (`emulator-5554`); it needs one Keycloak login to be usable for continuity
  trials.
