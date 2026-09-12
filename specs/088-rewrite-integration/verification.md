# Feature 088 verification record

Status: local implementation started, 2026-09-10. No full integrated feature, live staging, merge, deployment or release is claimed.

The owner's 2026-09-11 Android/Apple continuation and local native checks are
recorded in [the native checkpoint](native-checkpoint-20260911.md). Windows
redesign is excluded; remaining feature and release gates stay open.

## Source and environment

- Deep base `2de5d867413ce2aec5b7448dca8eeaa42674a7e3`; local feature branch `codex/088-rewrite-integration` after refreshed all-origin-tree ownership preflight. Source 081–087 branches remain untouched.
- Donor `daeea32b6e99b9f7cf0ff32abe372731744c639e` remains read-only; its data/services are not imported or changed.
- Plane `b20c8f3e06fc5302262fe3c8f049fa18a562e30f`; Projection `07e5c90cb310c48c2315997f74dc8dd6f5fa22ea`; Primitives `8dadde18ea84b511e130ccc332cf2f62de2990cf`; LETS `6245189920c686353c4ced7a208d56ec266f745c`.
- Host Python 3.11.15, Node 24.13.1, Corepack 0.34.6. Initial Deep environment lacked installed Plane/Projection, build, pytest-cov and PySide6. Pinned component checkouts are authoritative; sibling Projection has a different older tree.
- Only donor containers were running at the preparation observation. No Deep live verification has run.

## Baselines and setup actually executed

| Command/context | Result |
| --- | --- |
| Projection: Deep Python `-B -m pytest -p no:cacheprovider tests/chrome/test_menu_model_contract.py tests/chrome/test_shell_blocks.py tests/webrender/test_render_golden.py tests/webrender/test_escaping.py tests/rote/test_rote_identity_preservation.py tests/test_resources.py -q` | 75 passed, one packaging test failed because `build` was absent. Source baseline; no product edits. |
| Deep/backend: explicit pinned Projection source paths, `pytest -p no:cacheprovider tests/test_welcome.py tests/test_welcome_identity.py tests/test_shell_assets.py tests/test_client_js_contract.py -q` | 42 passed. Source baseline, not installed/live qualification. |
| `uv pip install --python .venv/Scripts/python.exe --require-hashes -r components/AstralProjection/tooling/python-ci/requirements.lock.txt` | Succeeded; installed declared build/coverage/lint test tooling, including build 1.5.0 and pytest-cov 7.0.0. |
| `uv pip install --python .venv/Scripts/python.exe pip==26.1.2 wheel==0.45.1 hatchling==1.27.0 uv_build==0.12.3` | Succeeded; local build tools match composition declaration; pip is local installation tooling. No runtime manifest change. |
| `.venv/Scripts/python.exe scripts/install_local_components.py sync` | Synchronized all four exact local component wheels; subsequent Plane/Projection/pytest_cov imports passed. |
| Spec/task structural check | 36 FR/SC identifiers, 67 unique tasks, no unmapped FR/SC. Story counts US1–US7: 8, 7, 10, 8, 7, 8, 8. Independent semantic analysis still required. |
| `git diff --check` | Passed for prepared artifacts at this checkpoint. |

Existing `.gitignore`/`.dockerignore` cover environment, secrets, generated data, caches and builds. No new package publishing/terraform/Helm ignore files are needed for the selected architecture. Existing Projection web ESLint tooling remains authoritative; added JS would have to enter its tracked lint input list.

## Required five-defect regressions

| Donor reproduced defect | Integrated test boundary / task |
| --- | --- |
| Credential mint uses stale pre-lock authority | Real owner-lock contention against local revoke/expiry at final mint, T046–T047. |
| Draft crosses owners in same tab | Verified owner A/logout/B, return to A and same-owner reconnect, T009. |
| Ten held oldest schedules starve another owner | Bounded eligibility/rotation through Plane and scheduler, T039–T041. |
| Discarded source bytes cannot retry after provider outage | Non-retention source refetch with preserved charges and fresh grounding, T025/T028/T029. |
| Public config fails because session changes generation | Config-first/session-first completion, true failure/retry and logout during startup, T009. |

Donor test passes and reproductions are historical review evidence, never integrated acceptance passes. Capability and detailed source requirement evidence remains pending until executed.

## Remaining qualification

Repeated Projection baseline after installing tooling and synchronizing components: **76 passed in 9.51s**, including packaging. Formal read-only Analyze found 0 critical issues and one high ordering inconsistency; all 36 FR/SC and 35 adopted/12 retained families have task coverage, with no unmapped tasks. The Accept contract now authenticates/resolves the original receipt before new-admission guidance expansion; integration tests must verify changed guidance after accepted submission.

Changed coverage, browser tests/live visual review, real institutional authenticated dispatch, representative migration/recovery, native compatibility/redesign, source grounding/human evaluation and full component/backend CI remain required. Public OIDC discovery confirms the issuer/S256 only; it does not verify client administration, mappers or authenticated journeys. No product push is authorized by this record.

## Local implementation checkpoint after UI feedback (2026-09-10)

The owner rejected the initial sparse-canvas/narrow-rail screenshots. The revised
web implementation uses one centered multiline composer, a restrained heading and
three compact examples with further examples disclosed. Reading/keyboard order
matches the layout through Projection-owned empty welcome placement hosts; the
original server-rendered nodes and composer/voice controls remain mounted. Send,
restoration, history or real output opens the existing responsive work layout;
New chat returns to the start arrangement. Semantic theme overrides remain intact.
No shared frame, primitive, IAM client or native layout contract changed.

### Executed UI checks

| Command/context | Result |
| --- | --- |
| Projection: `../../.venv/Scripts/python.exe -m pytest tests/chrome tests/webrender tests/rote tests/test_protocol.py tests/test_resources.py -q` | **958 passed, 1 skipped**, 7.51s. Skip requires the immutable extraction-source repository environment. Old single-line/voice-before-input expectations were updated to the new accessible multiline order. The extraction transformation ledger binds exact changed bytes and preserves original source tuples. |
| Deep: explicit Projection/backend/src and Deep/backend on `PYTHONPATH`; `pytest backend/tests/test_welcome.py backend/tests/test_welcome_identity.py --cov=orchestrator.welcome --cov-branch --cov-report=xml:build/088/welcome-coverage.xml -q` | **20 passed**, 100% statement coverage, 98% statement/branch combined (40 statements, 0 missed; one partial branch). An earlier malformed coverage module argument collected no coverage; this corrected run supersedes it. |
| Projection/web-ci: `node node_modules/@playwright/test/cli.js test tests/continuity-contract-060.spec.js --config=.088-playwright.config.mjs` | **59 passed**, 36.9s in installed Edge. Synthetic transport/DOM regression evidence, not pinned-Chromium or institutional release proof. Covers account/logout/reconnect, delayed uploads/export/share/clipboard, fresh owner transport, start/work transitions, welcome full/partial replacement and retirement, history at three widths, keyboard/IME and mounted-node identity. |
| Same Edge config: `tests/voice-conversation-065.spec.js --grep "auth\|logout\|cleanup\|typed\|new chat" --reporter=line` | **19 passed**, 16.7s. Existing voice authorization, typed fallback and logout cleanup regression fixtures; not live microphone/Keycloak verification. |
| `corepack npm run lint`; targeted Python Ruff; both repository `git diff --check` | Passed for touched UI sources/tests. |
| `python build/088/ui-preview/render.py`; `node build/088/ui-preview/check.mjs` | Current server-owned welcome and Projection shell rendered at 1440px, 320px and 320px with 200% root text. No horizontal overflow/page errors; all six examples remain accessible through disclosure. |
| `node build/088/ui-preview/live-layout.mjs` | **3 layouts passed**, 1440/768/320px, using actual shell + renderer + client source with isolated fake transport. Verified natural control order, Send to work, New chat to start, welcome re-entry and unchanged composer identity. This is browser runtime evidence, **not a live backend**. |

Images and diagnostic scripts are ignored local artifacts under
`build/088/ui-preview/`; the final screenshots are `runtime-1440.png`,
`runtime-768.png`, `runtime-320.png`, plus the enlarged-text `text200.png`.
The local Edge test config is untracked tooling and is not part of product packaging.

The broader Deep source attempt covering shell assets, client contract, first-turn,
history and wiring produced **73 passed, 17 skipped and 5 setup errors**. The errors
occur while constructing the PostgreSQL pool at the test environment's unavailable
host database; no qualifying backend-wide pass is claimed. Focused welcome,
surface/operation fixtures and Projection source checks above are independent.

### Security and durable-work increments

- Provider keys: implicit reuse is bound to the same server-resolved endpoint for
  personal and system probe/model/save paths. Endpoint changes require explicit
  key entry; the admitted operation and terminal replay expose only the fixed safe
  validation message. **273 tests passed** across LLM configuration and chrome
  dispatch; **47/47 changed executable Python lines covered**. These are isolated
  operation/credential fixtures, with no live provider or IAM traffic.
- Browser privacy: logout/owner changes clear drafts, staged files, queued actions,
  private caches/transcripts/notices and owner-specific late callbacks. Owner change
  replaces the socket before another principal can receive queued old frames.
  Same-owner reconnect retains the draft. Deep's configuration is server-injected;
  the donor's competing public-config/session Promise.all bootstrap is not adopted.
  Existing shell bootstrap guards and delayed-session/logout fixtures cover the
  integrated boundary instead of adding an unnecessary public-config fetch.
- Plane foundation: typed one-shot admission, original-key receipts and guarded
  `079.001 → 088.001` schema edge are locally implemented. Independent review
  reproduced and corrected profile-unfiltered recovery and deleted-receipt UUID
  reuse; public predecessor metadata was also corrected. Final source run:
  **335 passed**, **175/183 changed executable lines covered (95.63%)**. Independent
  reviewer reran **11 targeted tests**, all passed. T024/T025 controls/transient
  actions and enhanced Deep runtime wiring remain unimplemented.

Runtime changes remain local and uncommitted on the named 088 component/Deep
branches at this checkpoint. Only the earlier planning checkpoint `146d339a` is
committed in Deep. Composition pins and installed baseline component wheels have
not been advanced to these candidates. Standalone rewrite data/services remain
untouched. Full navigation/work/guidance/monitoring/framework adoption, live
institutional IAM, representative full migration/recovery, native redesign,
release evidence and deployment remain open; this UI increment does not complete
the overall integration.

## Local test deployment and Docker cleanup, 2026-09-11

The owner requested removal of rewrite Docker containers/images, then explicitly
authorized its saved volumes too. Removed 32 donor containers, one disposable Plane
test container, 26 rewrite-owned images, `astral-rewrite_pgdata`, and
`astral-rewrite_ollama-models`. The disposable container's anonymous volume was
automatically removed. Original `astraldeep_pgdata`, legacy Deep files, unrelated
projects, shared upstream images, and donor source were preserved. Exact scoped
identities are recorded in ignored `.work/docker-cleanup-20260911.json`.

The runtime uses a separate clean checkout, `build/088/local-test`, on local branch
`codex/088-local-test-20260911` at Deep
`974dfd992bad8d50c4d100c3fa933ffdce36c1c9`. Projection's eight reviewed UI/privacy
files are locally committed at `aa9e0977b964d7ba770fa2917e26c5c040715a96`.
This candidate deliberately retains qualified Plane
`b20c8f3e06fc5302262fe3c8f049fa18a562e30f` / schema `079.001`; it does not deploy
the unfinished Plane-088 foundation. Original Deep/Plane working changes remain
preserved. Both composition checks pass; the candidate composition digest is
`95ab34684d8ce550b6682d0cb059848f71daa9b6a2a11e5b0ebf3ac210bddebf`.

Canonical startup command:

```powershell
& './build/088/local-runtime/start-local.ps1'
```

The build/start command exited **0**, built and verified four exact component
wheels, and passed `pip check`. Compose project `astraldeep-088` runs the app,
PostgreSQL, LiveKit, and voice worker with loopback-only published ports. The app
and PostgreSQL are healthy with zero restarts; the other two containers run with
zero restarts and have no Docker healthcheck. Worker preflight reports success.
The app image is
`sha256:2a41a1bded603d9cf57ea138183afc19588da3500156eb68f34187614fbf5977`;
the worker image is
`sha256:2910e475b90ee0c36027e626cf94385d05b1f3b4b6dd413c1e54902a621461b3`.
Storage uses isolated `astraldeep-088-test-pgdata` and ignored runtime data/tmp/
knowledge directories; its private audit key is separate from existing data.

`verify-public-surface.py` passed **18 live checks** at
`2026-09-11T14:47:34Z`: liveness/readiness 200, signed-out session posture, three
protected API 401 denials, exact committed CSS/JS bytes, and existing-Keycloak
login redirect with S256 and `http://localhost:8001/auth/callback`. Report digest:
`8e70eea7fdffa555f8f6b7c4bb7cb0c1b662bbabd8242c9347ec303c94018ead`.
A fresh in-app browser navigation reached the institutional **Sign in to Astral**
page. No user sign-in or provider credential entry was performed.

The test URL is **http://localhost:8001**. Full commands, image/storage identities,
and safe stop/restart instructions are in ignored `build/088/local-runtime/README.md`.
Authenticated UI/Send/provider flows, callback completion, account switching,
microphone/media and native clients remain pending. This is a locally running
UI/provider increment, not the full integration or a production release. No
product branch was pushed or merged; local runtime files have no remote backup.

## September 11 research review and authorized PR preparation

This checkpoint supersedes the earlier local-only publication scope: the owner
explicitly requested review PRs for every touched rewrite repository and approved
merging/closing LETS dependency updates. Rewrite PRs remain drafts; full integration,
native qualification, institutional signed-in staging and release evidence remain
open. No rewrite merge or production release is authorized or claimed.

- Combined current Deep regression command: `python build/088/research-review/run_checks.py`:
  **1549 passed** in 240 seconds. One collection warning concerns the existing
  `TestConnectionResponse` Pydantic class, not a failed test.
- Final HITL proposal-race amendment: **188 passed**, including real UI-event,
  direct/parallel dispatch, public-read denials, policy/hook mutation, revocation,
  session changes, duplicate clicks, and one-attempt physical execution. Independent
  review reproduced the three original admission/effect/retry defects and verified
  each fix; 113 earlier exact-source tests also passed independently.
- Changed Deep executable Python: **848/856 lines (99.1%) across 23 modules**;
  every changed module exceeds 90%. The final HITL helper line map comes from its
  final-source targeted run, replacing the earlier map rather than combining
  different source revisions. Research-specific source was independently 110/110.
- Actual Projection renderer/CSS showed complete pending/declined/approved cards
  at 1440 and 393 pixels, with internal code-preview scrolling, visible actions,
  no page overflow and no browser errors. These use synthetic DTOs and transport;
  they do not claim user sign-in or a live user-approved external effect.
- Ruff and Git diff checks pass. Checksum-verified Gitleaks 8.30.1 found no leaks in
  the five local Deep increment commits or the two Projection increment commits.
- Installed local source: Deep `5893f8617e33e5f40683452dcee3a62e43e8fa0f`,
  Projection `cae52a2a743423561f9c46546cd2341f6af66aba`, Plane
  `8924bd4ba154a00184218c5b50c3de3bab9d13c4` / schema `079.001`.
  App image `sha256:16665c5b424870090898d7f7ec27f5891bb582148a3ba1b809df75a5d253f824`.
  All 23 reviewed production files match the running container; scheduler execution
  remains enabled. Installed arxiv 4.0.1 returns three actual public papers through
  the updated MCP adapter. A direct keyless-search probe observes HTTP 202 and the
  short `SEARCH_BLOCKED` API-key remedy, with `retryable=False`.
- LETS PR #49 merged at `78d035f44213d1eb5c13ea6a61b1a36d37ab7d4b` after all
  eleven hosted checks passed. Its seven superseded Dependabot PRs #42–#48 are
  closed; zero open PRs and zero open security alerts were verified, with no alert
  dismissed. The explicit libuuid security fix leaves zero HIGH/CRITICAL scan
  findings. Existing v1.0.11 and the Deep pin remain unchanged. A local Windows
  coverage timing failure was reproduced on unchanged LETS main; hosted Linux
  and Windows qualification passed.

Diagnostic logs, source hashes, exact test manifests and local deployment evidence
are retained under ignored `build/088/research-review/`. User credentials, pending
approval arguments and raw upstream bodies are not included in this checkpoint.

### Published review drafts and CI test repair

The owner-authorized drafts are [Deep #195](https://github.com/AstralDeep/AstralDeep/pull/195),
[Projection #15](https://github.com/AstralDeep/AstralProjection/pull/15) and
[Plane #8](https://github.com/AstralDeep/AstralPlane/pull/8). No rewrite merge or
release occurred. The separate donor repository has no remote; Primitives did
not change. LETS maintenance is the separately merged PR described above.

Initial Deep CI failed one stale exact-component assertion shared by two jobs;
the test now checks the reviewed pins while retaining exact schema, migration,
protocol and gitlink assertions. Initial Android instrumentation likewise still
expected the intentionally removed grounded badge. Projection test-only commit
`72553e3b533a5284efb6cf425711be0fb8b6d2a9` checks its absence, retained stamped
metadata and actions, and visible estimated/generated warnings. Local Android
lint and instrumented Kotlin compilation passed, along with 13 targeted JVM
tests. A local emulator could not start because its disk-space check failed;
hosted instrumentation passed on the corrected test revision, including the
Android aggregate. A subsequent metadata-only Projection commit
`416ce6ce97b0af4cf812ffc8c53b9e2718b2aa70` adds the exact transformation record
for this imported test and updates the expected ledger counts. The changed-byte
ledger and all 519 immutable source tuples pass the two targeted replay checks.
The Deep manifest pins that metadata revision; the installed candidate retains
byte-identical runtime sources at its previously recorded Projection commit.

The broader Projection Python command on Windows passed 1187 tests and failed
17 unchanged POSIX/Apple tooling cases: one filesystem executable-bit assertion
and sixteen `select()` calls on Windows subprocess pipes. The failing scripts,
tests and Gradle wrapper are unchanged from the prior Linux-CI-passing revision.
The ledger checks pass independently; the hosted Linux suite qualifies this
metadata repair. The Windows result is retained as a failed local run, not a pass.

Both exact Deep CI cohorts passed locally in a source-free isolated Linux
checkout with Python 3.11.16 and the hash-locked CI tools: component contracts
274 passed / three deselected; release tooling 833 passed / five skipped / four
deselected. Tooling coverage was 92.01% against 90%; the documentation link check
passed for all 25 maintained Markdown files. Coverage XML and exact commands
are retained under ignored `build/088/ci-pin-repair/`.

Plane hosted CI passed at `c8303a2e5a2243388d195c2d325523ba4228efd7`: 2251 tests
passed, nine Windows-only tests skipped on Linux, total branch coverage 89.26%
against 88.75%, and changed-line coverage 203/211 (96%) against 90%. This includes
both tests that failed the earlier local diagnostic, which ended at 671 passes
and two failures. One local failure was caused by that diagnostic's custom
five-second advisory-lock timeout; the other was an intermittent empty claim
with two fresh focused passes. Neither failure was hidden or weakened. T024/T025,
enhanced Deep wiring and full integrated acceptance remain open; the running app
continues to use Plane 079.001 and does not consume the unfinished 088 foundation.

## 2026-09-12: combined Android and Apple component actions

Projection `eff1b3ad779b570c0fe4824f370407efc2a37047` integrates the
Android and Apple consumers with the preceding server, navigation and Watch
changes. Plane remains `718021ba019abcd3a04ffbd6b80b88e51395bda6`;
the protocol and schema identities in the following checkpoint are unchanged.

- Both consumers parse the same immutable component-action corpus, preserve
  exact owner/session/chat/component context, and send Refine/Restore only on
  the current established socket through ordinary operation correlation.
  Neither decision enters the reconnect queue. CSV and private share links
  retain bounded authenticated requests, explicit user delivery and cleanup.
  Native footers use the web labels, ordering and typography. The Apple merge
  preserves the independently qualified navigation test factories and behavior.
- Frozen Android `fe6bfdf` passed 126 Core and 334 app JVM tests, normal
  lint/build/coverage checks and 21 actual component/footer UI tests. An
  integrated provenance presentation follow-up passed Kotlin formatting and
  compilation; its final device run remains pending. Frozen Apple `8fd112d`
  passed 224 Core tests plus eight model and ten actual iOS UI tests with zero
  skips. These focused diagnostics do not replace the combined native-domain
  or live-backend qualification.
- Independent native review found no additional actionable issue. Root verified
  both handoffs against committed source blobs, retained original failed
  attempts, and resolved the Apple fixture overlap without dropping either
  scenario family. Strict recursive Swift formatting, staged secret scanning
  and all 21 protocol/source replay tests passed on the combined source. The
  extraction ledger now has 185 exact transformations, 16 removals and 334
  unchanged source entries; the original 519-entry extraction is unchanged.
- The preceding server checkpoint passed 1,827 Projection tests with zero
  skips, 329 integrated Deep/PostgreSQL tests and 1,089 portable Windows tests
  with ten existing skips. Deep's diagnostic changed coverage is 182/182 lines.
  A fresh isolated Linux Python 3.11 install qualified all four component
  wheels and 186 packaged source/resource files against the exact preceding
  pins. Native-only follow-up changes still require an exact new-pin package
  comparison; previous measurements are retained under their original SHAs.
- Fresh combined Android raw JVM/device coverage and unsigned AAB, Apple
  Core/App/UI domain qualification, unsigned native archives and isolated Watch
  qualification are being collected separately. Actual staging, protected
  provider evidence, full remaining 088 stories, final live parity and store
  readiness remain open. Saved feature checkouts and the signed-in backend are
  still at their previously documented checkpoints. No product push, draft
  promotion, signing or store submission occurred.

## 2026-09-12: component action delivery, native navigation and request wait bounds

This local checkpoint composes Projection
`d41b62fd13764aa4f4cbdf0e2e7b1345e573c3b2` and Plane
`718021ba019abcd3a04ffbd6b80b88e51395bda6`. The additive component-action
presentation contract has canonical digest
`cf34f5eaf86a8802344f0a29a78a64a29738a67c511fe6da33058d0bad4d95fd`.
Schema remains `088.001` with the existing migration digest. No new primitive,
frame, action, runtime dependency, route or runner is enabled.

- Projection builds one bounded refine/history/CSV/share inventory from original
  component identity and type, current host flags, and receiver capabilities.
  Deep stamps only outbound copies after ROTE on full canvas, snapshot, upsert,
  targeted resize and legacy resize delivery. Same-socket owner changes retire
  the raw cache; a resize resumed after registration changes cannot publish its
  captured view. Native consumers are being implemented separately and remain
  pending integration at this checkpoint. Windows retains its existing layout;
  Watch and voice omit component actions.
- Independent review replayed 16 immutable baseline web cases byte for byte.
  The frozen cross-client parser corpus is SHA-256
  `0fad08ce1881c4f6fe1d87aa684765e7f2e2764fb4da6b09bafd777eecb6a73d`.
  Projection's complete Python suite passed 1,817 tests with one missing-source
  replay skip; the explicit immutable-source replay then passed all 21 protocol
  tests. The final focused model suite passed 80, including two additional null
  and nonempty-text compatibility cases. These cohorts overlap.
- The nine relevant Deep delivery, registration, component action, export,
  history and viewport modules passed 137 tests on Linux Python 3.11 with
  isolated PostgreSQL. Independent actual-handler/auth replay passed 31 cases.
  The canonical changed-line checker passed in diagnostic partial mode:
  Projection `6eab76d` to `d07b54a` covers 138/138 Python lines; Deep `88be198`
  to `9eeb79e` covers 35/35. Ruff, diff and staged secret checks passed.
- Apple navigation `b75c0d9` preserves the draft/result when Recent chats closes,
  exposes Close during pending settings loads, rearms repeated Retry timers and
  preserves mandatory-screen restrictions. The isolated iPhone regression first
  reproduced three failures, then passed eight model and four actual UI cases.
  Source and artifact hashes were independently checked. These are diagnostic
  builds, not a replacement for final native-domain qualification.
- Watch `00a7e84` advertises its existing history/loading renderers and preserves
  passive key/value hints and detailed list text. Actual Deep history-builder
  output passes through the real ROTE fixture and shared Watch decoder. SwiftCore
  passed 218 tests and ROTE passed 594; the helper covers 68/70 executable lines.
  Watch apphost and final live interaction remain unverified for this change.
- Deep `d15c231` adds a request-local signed-cookie forced-refresh prerequisite
  with ordinary IAM verification, exact persisted credential fences and a
  database-clock authority limit. Plane's opt-in transaction-local limits bound
  each lock wait to 100 ms and each statement to 1,000 ms while preserving stricter
  settings and ordinary session defaults. Pool checkout and connection deadlines
  are separate; async cancellation is not physical worker termination. No work
  ingress or continuation uses this prerequisite yet.
- Request/auth compatibility passed 192 tests; Plane's focused cohort passed
  239, with changed Python coverage 147/147 and 3/3 respectively. Full Plane
  recorded 2,605 passes, nine Windows-only skips and one stale provenance hash
  failure. After the exact ledger repair, all 22 provenance/API/architecture
  cases passed. The original failure remains retained. The final offline Python
  3.11 wheel/install checks passed; all 69 packaged Python sources were checked
  against the wheel, whose SHA-256 is
  `86cc9c0996f5e3f26186b1c1d79d581676cd23e9580ecc29799538c9e7d3cec2`.

The native component-action consumers, final exact native builds/coverage,
authenticated staging, immutable issued-session incarnation binding and the
remaining full 088 runtime/guidance/monitoring/framework work are still open.
Existing native archives and the Android bundle are historical unsigned
artifacts; they do not qualify the new shipping sources. The user-signed-in live
backend/apps, product remote heads and draft PR states remain unchanged. No
product push, PR promotion, signing, release or store submission was performed.
