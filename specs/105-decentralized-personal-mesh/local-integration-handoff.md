# Spec 105 integration and qualification handoff

## Current verified handoff — 2026-11-15

This checkpoint supersedes the historical status language below, which is retained verbatim with its original dates and outcomes. Product source is frozen at the exact commits listed here; this update changes documentation only.

| Component | Exact source | Verified state |
|---|---|---|
| Deep | D11 `528467d1bd602ab3219a426ac9c82eda3b5bc4d5` | Hosted CI run `43716072357` green 19/19. Strict resume 1767/1833=96.40% (base `ced61ce087ca8b1a1d1e4339e521ef2c8f59718a`, report SHA-256 `84722e6a4b0fb1bdc7eee5cfd9abf4016b192c1bd3e03611e41d1738e2419117`); full-feature 5960/6118=97.42% (base `a3eb724aa15fcdcb2c590b859c5f5104c41a4daa`, report SHA-256 `aafd3f64d72b1b957d4209eda5e04ea0d1cdf28d8f3efe80d60dbf8d6657c35a`). |
| Projection | P18 `6dbe0a717efc0b49d89beaecf44988f657c599d2` | Primary `43716070032`, Android `43716069975`, Apple `43716069974` green; Apple 21/21 provider jobs. Native totals, declared skip identities and Watch retry are recorded in `qualification.md` and P18 summary JSON. |
| Kernel | `ed5ad2231f445497082750103326242b24fcbcb0` | Hosted CI [43680163653](https://github.com/AstralDeep/AstralKernel/actions/runs/43680163653) green; 1,310 workspace tests, one existing multicast ignore, strict Clippy, 17 source-bound payloads. Manifest SHA-256 `7bf28406a4a02433033527627febf9b46349934017f234d34001884d9183e976`. |
| Vessel | `7d3b6f29f688812a1cb1a88861ae487b933b38cb` | Hosted CI [43680628723](https://github.com/AstralDeep/AstralVessel/actions/runs/43680628723) green with the exact adopted Kernel source/artifact pins. |
| Public Mac release | `macos-v2.0.23-96` | Protected publisher run `43718280618` succeeded; all 11 assets passed publisher validation; Root independently verified the public source graph after publication. DMG SHA-256 `50c89a80eb75c7b58bc9b3bfcd47593282978f546b79825520272ed662373bcc` (32,257,605 bytes); NodePack `37157527cab5d625fc3aa8c4b550b1c505ffa010fcfc58e02711e0590cbc6bb4` (146,494,149 bytes); descriptor `7c6f67ae96278525a8f413883007b6fff7850b79c74c4d257ee368a979d9c901`; manifest `a3d1932364828c36d54fda87d0160f980a250dd1e9e38393c709ebff0f119bff`. Read-only app/DMG signature and stapler checks plus Ed25519 policy/DMG signatures passed. Raw proof: `evidence-105/verify-public-macos-96.log`. These public identities differ from the local N11 diagnostic pack. |

The normal local D11/P18 qualification also records 4,603 Python passes, 8 skips, 33 subtests, helper 16/16, 303 composition/release checks, 77 recipe passes with one existing skip, and 159 workflow-contract passes with 4 skips. The P18 native run records the 6 declared skips and one permitted Watch test retry; preserve the first-attempt failure rather than describing an all-first-attempt pass.

The approved cleanup deleted 29 merged task-owned local/remote branch pairs and verified absence independently (Deep 16, Projection 10, Kernel 2, Vessel 1), with evidence SHA-256 `6b747be513722f0fbcf592ab60d6526d4a225b161163e6b6f2fc461829fdf751` in `evidence-105/merged-spec105-ref-cleanup-final.json`. Scoped temp cleanup recorded 103.65 GiB reclaimed while retaining source, current artifacts, models, stores, and evidence.

## Current qualification commands and logs

Deep’s authoritative hosted job log is run [43716072357](https://github.com/AstralDeep/AstralDeep/actions/runs/43716072357); strict coverage reports and command records are retained under `evidence-105/codex-20261114-live/institution-final/release-candidate/hosted-d11/`. Kernel workspace/Clippy results are in [43680163653](https://github.com/AstralDeep/AstralKernel/actions/runs/43680163653), Vessel results in [43680628723](https://github.com/AstralDeep/AstralVessel/actions/runs/43680628723), and Projection logs in [43716070032](https://github.com/AstralDeep/AstralProjection/actions/runs/43716070032), [43716069975](https://github.com/AstralDeep/AstralProjection/actions/runs/43716069975), and [43716069974](https://github.com/AstralDeep/AstralProjection/actions/runs/43716069974). The protected release verification log is `evidence-105/verify-public-macos-96.log`; native totals and report hashes are listed in [qualification.md](qualification.md).

## Current handoff closure

The website update is deployed and verified. Release-sync run `43723268187` passed sync/build/deploy at site SHA `efd9097c98ba85267eb0c29f01226eb6709947c0`. The canonical URL `https://astraldeep.github.io/downloads.html` returned 200 with release JSON version 2.0.23 (96), matching DMG and optional NodePack links, and iPhone preparation label. Root inspected desktop 1200px and mobile 390px layouts: scroll width equaled each viewport and both download controls were visible. Screenshots are retained at `evidence-105/site-mac96-desktop.png` and `evidence-105/site-mac96-mobile.png`. No App Store submission, production APNs registration/delivery, physical owner-device demonstration, or live provider sign-in is claimed. Live observations remain optional under the acceptance clarification; deterministic gates are complete and were not waived.

## Historical handoff records — original text preserved verbatim from 2026-11-14 onward

Updated 2026-11-14. The current work is the "Codex continuation" section at the end of this file; it supersedes earlier checkpoint claims for changed source. The fifth checkpoint remains evidence for its exact earlier revisions. Work resumed after the historical stops below. Local implementation and qualification remain in progress. The final candidate is identified by an external exact-SHA evidence inventory after the clean source freeze; no complete final same-SHA gate set exists yet, and the feature is not production-ready. The owner approved spec section 17. The owner is away with the phone disconnected. Physical checks remain pending; phone mirroring and automation are not authorized. No new product push, Kernel publication, merge, deployment or release is authorized by this handoff.

## Current source and pending adoption

This candidate integrates Deep `ee604daa3b66b5034ed898999f7c0ce6f908de5e`, including the stream/voice guard repairs, deterministic missing-socat fixture and incoming-peer final-dispatch revalidation. Resolve actual worktrees from Git before resuming; an older path or revision in the historical sections is not a current instruction.

| Component | Inspected pin or qualified checkpoint | Remaining adoption work |
|---|---|---|
| Deep | `feat/105-integration`, named `final` worktree, through `ee604daa`; exact clean freeze is recorded externally after this adoption | Run all four canonical groups and both strict changed-line comparisons against one newly built exact-source image. Incoming-peer authority repair is passing; the local Python model loop remains separately bounded below. |
| Kernel | `a49905544244c79dd0f5b847ac4c1618e4a055e5`, local-only; 1,186 workspace tests, one existing multicast ignore, formatting and strict all-target/all-feature Clippy pass | All 14 rebuilt artifacts are digest-bound to this source; accepted wheel/mailbox digests are in `config/astral-kernel-binding.json`. Duplicate settled transfers now preserve acknowledgement semantics without forcing another sync. |
| Kernel persisted notes | Test-only `e2db832b68d4a0b016b0f2272c2159ace01ac474` is incorporated in c015c8a | Real `LogRunStore` compaction/reopen retains Untrusted notes, omits original tool text in a later prompt and denies outside dispatch without consent. All 17 runs-service tests pass; criterion 3.9 is qualified for persisted notes only. |
| Projection | `fb5de54fd5fb0193bf72e2e6a7975e2f7d212114` adopts the a499 Kernel, native mesh entry, current voice repairs and duplicate-acknowledgement fix `b82536` | Final full native gates and live duplicate-feedback recheck remain required. The earlier full Mac attempt has 18 accessibility failures under investigation. NodePack Deep source/descriptor update follows this freeze separately, avoiding a reciprocal gitlink cycle. |
| Plane | `f8f2a637f7b026f200b6e925a651faada13a4116`, package 0.1.12, schema `089.022` | Existing PostgreSQL/SQLite pins remain authoritative; no new Plane migration is introduced by the test/doc repairs. MCP registry/credentials use existing current-device sealed KeyCredential items, with no new SQL schema. |
| Primitives / LETS | `f4314c8258c09383f9474a8c9f1ce35df44d62b9` (0.13.0) / `13213061b6ebfc5fa1c81d0c65046b60fa1b7e3e` (1.1.2) | No incidental release or publication. Windows' independently locked public LETS client remains its reviewed 1.0.11 closure. |
| Vessel | `936dc1ff38c6f605a9506b94b3045883d7d27d7c` adopts exact a499 source/wheel | Fresh network-disabled Linux Python 3.11 image/tests are recorded externally. Historical c015 evidence had 147 passes and a missing-FastAPI skip; the final test image includes the declared egress test dependencies. Hosted distribution remains unqualified. |

The adopted Linux aarch64 wheel SHA-256 is `4fc2f5beeafbbf8d425fcc33c9e44715e3809e12282f00da46246254c4c6de34` (12,817,312 bytes); rebuilt mailbox digest remains `86f6e873bba8f2c601bc1ec2abe2a59190bcd049c3e54b20d088b8d3ccdc61c3` (19,309,920 bytes). Both were rebuilt from a499055. The verified 14-artifact inventory is `AstralKernel-wt/integration/artifacts/qualified-a499055-20261113/artifact-manifest.json`, SHA-256 `c7803a378f588af0c3cc519f00d39f9344e65db124fe635b23788362e3c7c6f9`; the minimal Linux build context is `/tmp/ad105-a499-kernel-artifacts`.

Exact accepted artifact hashes remain in `config/astral-kernel-binding.json`; component/schema/contract identities remain in `config/astral-composition.json`. The unchanged Plane pin has PostgreSQL migration digest `dde302967743cf8f6f5d55e82f8355ef405823dfee638909c3a2f11b85f5cb27`, SQLite migration digest `a6681e870de6af81e8d21664b75b9e496fc92514b29dc2aac9e8a9e69d919a84`, and SQLite structure digest `5f45079d23dc20e1dc0bb1367e8076c55a582ba7df7a76224fc0cdbe1a2b4d0b`. Verify these against the eventual candidate rather than copying an older stopping-point digest.

## Executed backend diagnostics and repairs

All results below are local diagnostics. The different source revisions must not be relabeled or merged into a passing final candidate.

| Source / scope | Result | Retained evidence |
|---|---|---|
| `4844c545`, initial four groups | `tests_1`: 11,978 passed/35 failed/three skipped. `tests_2`: no executed cases; random ECDSA parameter IDs caused collection disagreement. Modules: 9,046 passed/one failed/17 skipped. Persistent agents: 1,008 passed. Initial root-user harness also invalidated permission-sensitive expectations. | `/tmp/ad105-resume-evidence/final/attempt-summary.json`, original group directories and collection forensic files. |
| `b2dd9c31`, first nonroot `tests_2` | Wrong scratch permissions caused 286 failures/errors and 47 permission-dependent skips. This is a harness failure, not a product result. | `/tmp/ad105-resume-evidence/diagnostics-b2/tests_2-permission-harness/`. Original collection-summary/coverage copies and preservation provenance: `/tmp/ad105-resume-evidence/previous-tests_2-pre-b2-runtime/`. The original initial-attempt XML/log/inventory were superseded before archival; they have not been reconstructed. |
| `b2dd9c31`, corrected UID501/mode1777 `tests_2` | 11,653 passed/two failed/13 skipped, 39 passing subtests, 687.44 s. All 12 real Kernel/mailbox cases passed. Failures were stream terminal-completion timing and work-runtime foreign-owner model-call count zero. | `/tmp/ad105-resume-evidence/diagnostics-b2/tests_2/`: JUnit, full collection inventory, raw branch coverage, backend/tooling XML, suite plan and results. |
| Stream synchronization repair `c7eeeb3e` (integrated into `13b96dbb`) | Original full file: 11 passed. Controlled terminal-return delay reproduced the exact `persist_done` failure. Explicit handled-value acknowledgments/final task completion then passed the same delay case and all 11 file tests; Ruff/diff checks passed. No production code, retries or increased sleeps. | `/tmp/ad105-resume-evidence/stream-test-synchronization/{baseline-valid,delayed-terminal-red,delayed-terminal-green,stream-full-green}/`, `repair-handoff.json`. |
| Work-runtime investigation | The original six-test file passed both serially (49.80 s) and on four workers (81.11 s). No demonstrated cause or repair yet explains the failed full-group foreign-owner case. It remains a required final full-group check. | Same evidence root, `work-runtime-baseline/` and `work-runtime-four-workers/`. |
| `13b96dbb`, corrected `tests_1` | 12,015 passed/two failed/three skipped, five passing subtests, 738.90 s. The two voice dependency guards rejected newly approved canonical local wheel inputs and the updated reviewed runtime-lock digest. | `/tmp/ad105-resume-evidence/diagnostics-13b/tests_1/`. This group used the earlier exact-dependency test image; subsequent image qualification is separate. |
| Voice dependency guard repair `7402d9d3` | Red: two failed/three passed. Green: ten passed, including five mismatched/unknown reference refusals; Ruff/diff checks passed. Reviewed local wheel versions/hashes, closure 72/71 and the one changed runtime-lock inventory digest stay explicit. | `/tmp/ad105-resume-evidence/stream-test-synchronization/voice-lock-{red,green}/`. |
| Existing hosted voice worker | 382 passed/two skipped; 4,219/4,669 executable lines covered (90.36%). This is hosted-worker coverage, not app on-device voice qualification. | `/tmp/ad105-resume-evidence/final/voice/voice-worker.xml`, `voice-worker-tests.xml`, `test.log`, and runtime/test image identities. |

Executed runner commands were `bash /tmp/ad105-component-adoption/run-final-group.sh b2dd9c31e0dded11ef0384a47d6a8e1618a8a77a tests_2` (bad scratch harness), then `run-b2-group.sh` with that SHA/group (corrected scratch), and `run-13b-group.sh 13b96dbbd82692df92452563d58a58a0943acbfe tests_1`. The scripts execute `python scripts/run_backend_web_tests.py --root <checkout> --group <group> --output <evidence>`; recorded exact pytest commands are in each `test-results.json`. Focused repair commands and the controlled-delay plugin are retained beside their XML.

## Latest security, sync and native evidence

- Incoming peer dispatch at Deep `ee604daa`: the original real paired-Kernel late-stop reproduction failed twice because a stop raised after ticket admission still allowed the bundled tool to run and record success. The repair freezes trusted incoming owner/request/tool identity and rechecks Kernel authority both at gate entry and immediately before physical dispatch. Ten focused files passed 247 tests; changed coverage was 119/120 lines (99.17%), with the new admission module fully covered. Root independently reviewed the seam and ran all 35 actual/boundary cases successfully in 25.80 s (`/tmp/spec105-node-admission-root-review`). This qualifies incoming peer calls, not every Python-originated model turn.
- The real Python-local `NodeMesh` diagnostic at ee604daa/c015 passed two diagnostic assertions, but both the normal and Kernel-stopped controls dispatched zero tools because `derive_machine_authority` supplied an empty consented-scope ceiling. The current unfenced virtual socket also selected encrypted system configuration rather than proving owner-provider custody. This is an existing fail-closed capability/custody limitation, not proof of stop safety or completed 3.11 conformance. Keep `/tmp/spec105-node-local-diagnostic/handoff-summary.json`; do not widen the ceiling or reuse effect receipts merely to make a golden test pass.
- Kernel a499055: duplicate voucher and acknowledgement replay each reproduced an unchanged log clock with a new sync notification (two failures). Atomic committed-change flags then passed both same regressions and all 19 holder tests, including first acceptance/settlement, duplicate/reopen behavior, invalid signatures and failed persistence. `cargo test --workspace --all-features --locked` passed 1,186 tests with zero failures and one existing host-multicast ignore; strict all-target/all-feature Clippy and formatting passed. Evidence: `/tmp/spec105-sync-quiescence-evidence/qualification.json`. This is deterministic runtime qualification; post-update canonical Mac idle CPU still needs live verification.
- Root observed real owner Mac ASR and the controlled arithmetic reply successfully; this is narrower than complete offline/interruption qualification. Audible duplicate feedback then exposed a separate acknowledgement bug; b82536 has passing source tests, while its live recheck remains pending. The full Mac native attempt retains 18 accessibility failures, with the activation harness/mesh-mode follow-up still under test. A physical iPhone normal-mode UI case passed in 17.9 s; hardware mesh voice, airplane mode and APNs are not thereby qualified.

## Adopted MCP source qualification

Kernel `c015c8a` passed `cargo test --workspace --all-features --locked`: 1,182 passed, zero failed, 103 suite results; `cargo clippy --workspace --all-targets --all-features --locked -- -D warnings` and formatting passed. `/tmp/spec105-remote-mcp-effects-handoff.json` retains exact log digests, including workspace `ba01a4260171b84a7cbc293c53f041de621dcf54c7167de7b1617dde815247aa` and Clippy `fc3ae9b5d10fa0347a23fbab3273864c37fbc272b31a18c72104f466efcaeb20`.

Every remote MCP tool is an outside effect even with malicious `readOnlyHint=true`. Actual guarded-loopback FFI/turn tests bind endpoint, opaque configuration revision and definition hash through approval, normal turns, reopen, stored-surface action and explicit unknown retry. Denial makes no call, signature, outside charge or receipt; approval signs the exact action digest and yields its outside-effect receipt. Guarded DNS/endpoint validation refuses proxies/redirects, and JSON/SSE/cumulative catalog data is bounded. Registry, credentials, pins and immutable turn bindings use current-device sealed storage. Generic MCP uses its third-party OAuth bearer plus local gates and signed journal; it does not invent Astral ticket metadata for arbitrary servers. Astral peer calls retain their existing ticket/receipt/consent/label path.

The new FFI management surface sets/lists/removes a server, refreshes its tools, approves exact server/tool/hash/revision and revokes a tool. No new mesh event type, SQL migration, primitive or cross-client frame is introduced. Native manager implementation is present; an actual public-internet/no-peer iPhone turn remains unqualified.

## Client, pack and native evidence

| Scope | Result and practical limit | Evidence |
|---|---|---|
| Projection Python / Windows attempts | Projection: 4,427 passed/one failed/seven skipped/33 passing subtests. Windows: 2,566 passed/one failed/20 skipped. Missing nested-wheel fixture dependencies and stale Windows lock constants were repaired at `5abd809`; entire affected files then had 57 passed/two skipped. These are repaired full attempts, not later complete green suites or native Windows PE execution. | `/tmp/ad105-windows/{projection-owner,windows-owner}.{log,xml}`, `handoff-summary.json`. |
| Projection Python/NodePack feature coverage | At `8abce633` against `ca7ba29c5115684fad0d79aa781c0c4ec24ad60d`: 616/628 changed executable Python lines, 98.09%; 230 focused tests passed/five skipped/one active Apple provenance guard deselected. That exclusion does not waive the final provenance gate. | `/tmp/ad105-windows/changed-feature-nodepack.json`, `owner-nodepack-union.xml`, `nodepack-qualified-final-tests.log`; 118-file digest inventory in `handoff-evidence-manifest.json`. Earlier `spec105-nodepack-coverage.json` is superseded diagnostic coverage. |
| Actual Mac node pack | Deep `13b96dbb`/Kernel `d02e4ea`, Python 3.11.18: 47 packaging tests passed; Swift `NodePackRealRunTests/testDownloadInstallStartHealthDescriptorAndStop` passed in 41.8 s. Archive is 146,234,210 bytes, SHA-256 `66b24ec894b1c6b8b4650458fb8f53881b7f1260fd0c9e387e19adbd269e9396`, with 461,482,171 installed bytes. Five digest-bound local components verified. | `/tmp/spec105-nodepack-13b-{build,tests,realrun}.log`; descriptor under the task cache `pack-13b96dbb/node-pack.json`. |
| Actual packed owner tool turn | Missing/forged credentials: 401/401; owner: 200. Normal encrypted provider configuration acknowledged; owner WebSocket offered 74 tools, executed `generate_dynamic_chart`, completed and persisted chart/final history. Issuer/model fixtures were loopback-only. Native mesh catalog and provider custody still need their own check, and the pack must be regenerated after the final MCP pin. | `/tmp/spec105_mac_pack_toolturn_13b.py`, `/tmp/spec105-mac-pack-toolturn-13b.log`. |
| Kernel persisted notes | Test-only `e2db832b68d4a0b016b0f2272c2159ace01ac474` is incorporated in c015c8a | Real `LogRunStore` compaction/reopen retains Untrusted notes, omits original tool text in a later prompt and denies outside dispatch without consent. All 17 runs-service tests pass; criterion 3.9 is qualified for persisted notes only. |
| Native stored/A2UI surface | Projection `2916899`: Mac 39 unit + one UI pass; iPhone simulator five unit + one UI pass. Both render real stored imported event `sha256:ca3e200bd8a6f2f6614ca6d029cf22c931b228c38a9c6ca2a119d4d7aa09c8fb` with byte-identical identity attachments, accessible Untrusted provenance, removed link and inert outside control. Native FFI reimports the input for desktop/mobile profiles. | `/tmp/spec105-native-surface-handoff.json`, final Mac/iPhone xcresults, screenshots and accessibility captures. This was f960 artifact-era fixture qualification, not final c015 combined binary or physical owner-device sync proof. |
| Latest voice source-side checks | Projection `730d82e`: 115 focused Mac tests executed, one optional real-model skip, zero failures. Bounded ASR completion, capture format, buffering/Stop Voice, Release defaults and A15 text fallback are implemented. | `/tmp/spec105-voice-mono-final-main-native-mac.log`. Actual owner microphone/speaker input, offline completion and interruption on final Mac/iPhone binaries remain unverified. |
| Actual voice weights / offline package | All 13 installed files verified at immutable revision `65619d48c5e4bc1552db24e07bc4a2ff2aa3cf49`, 1,419,703,438 bytes. Package suite: 39 tests with one optional skip; seven real-weight parity checks. The earlier manifest-verification failure against a different local source directory is not installed-model evidence. | `/tmp/spec105-voice-installed-model-verification.json`, `/tmp/spec105-voice-real-offline-tests.log`. |
| Optimized voice diagnostic | Network-denied Release/optimized diagnostic passed in 6.84 s; ASR 0.159–0.174 s for 2.924 s audio, TTS 1.27–2.03 s for 4.8 s (RTF 0.264–0.422). Debug overhead explains the earlier slow decode/synthesis diagnostic, but no full live voice session is qualified. Release app defaults, bounded final-ASR wait, buffering/Stop Voice and A15 fallback have source-side checks at 730d82e; final live qualification remains pending. | `/tmp/spec105-voice-optimized-stream-diagnostic.log`. These measurements are diagnostic, not shared-runner performance gates. |
| Identity / physical device / push | Stable Mac signing passed two relaunches and a changed-binary rebuild with zero Keychain prompts. Connected iPhone 13 Pro provisioning is corrected to the intended PersonalizedAI team; final rebuilt installation and real device checks remain pending. Warm APNs handler `7339507`: 96 focused iOS tests passed; normal owner mesh push registration/access custody and actual background delivery/sync remain missing. | `/tmp/spec105-mac-stable-requirement-check.txt`, `/tmp/spec105-build-localtest-script-mac-stable-final.log`, `/tmp/spec105-mesh-wake-voice-green-ios.log`. |

The package install can be reproduced with `swift test --package-path apple-clients/AstralCore --filter NodePackRealRunTests` only after setting the retained descriptor/fixture environment from the recorded runner. The standalone packed tool turn used `python /tmp/spec105_mac_pack_toolturn_13b.py`. Logs without a retained invocation establish only their recorded run, not an invented exact command. Do not rerun owner sign-in, presence approval, model downloads or hardware actions without preserving their normal user flow.

## Final freeze and gate procedure — pending

Adopt the exact source/artifact pins above, then select one clean final Deep SHA for the backend gate. Remaining native/live checks stay explicit and do not become backend-pass claims. No later source mutation may occur while the four canonical groups execute. Keep the final SHA, image identities, result/coverage digests and residual inventory outside the candidate tree, bound to that SHA. Do not amend the candidate simply to insert its own SHA or final reports into these documents: a documentation-only follow-up would otherwise force an endless qualification/commit loop. Any required product/source change instead invalidates the freeze and requires a new exact candidate. The prepared local runner is:

```text
bash /tmp/ad105-backend-candidate-gate/run-frozen-group.sh FINAL_SHA GROUP UNIQUE_REPORT_ROOT FINAL_TEST_IMAGE
```

Run `tests_2`, `tests_1`, `modules` and `persistent_agents` serially, keeping at most four pytest workers active. The image must be newly built from the frozen source plus the a499 artifact context; its test layer installs both `tooling/python-ci/requirements.lock.txt` and `tooling/backend-ci/requirements.lock.txt` with hashes, checks every active installed pin and `pip check`, and carries Node/npm, bubblewrap, socat and ripgrep. Earlier 13b/9a/505 images are diagnostic predecessors. The runner guards exact SHA/clean tree, captures image identity, uses UID501:GID20 and init, probes all five mode1777 scratch mounts and restores their exact tracked placeholder bytes before checking the in-container tree. This avoids hiding tracked `backend/audit/retry_queue/pending.jsonl` under tmpfs. Only task-owned disposable PostgreSQL fixtures are provided; shared services and credentials are untouched.

The 9a57c391 diagnostic `tests_2` run had 11,651 passes, 16 skips, one host-dependent socat test failure and one missing-validator collection error; original stream and foreign-owner cases passed. Its incomplete test image lacked the full backend CI lock. The 50585132 test-only commit made the socat denial explicit, and the complete guarded closure passed targeted checks. The subsequent stop-race repair and Kernel refresh intentionally create a new source/artifact candidate; these corrected-candidate groups are not unchanged-suite retries. All superseded reports remain under `/tmp/ad105-resume-evidence/frozen-{9a57c391,50585132}`.

When every group passes, run `scripts/merge_backend_web_coverage.py` with exactly those four `--evidence` directories, `--recorded-root` equal to the checkout path used inside the containers, and a new output directory. It refuses mixed source/runner/reporter identities or failed/missing suites. Retain JUnit, every nonskipped failure and skip reason, full collection inventories, raw branch coverage and XML; a targeted retest cannot replace a complete same-SHA group.

Run `scripts/check_changed_coverage.py` against both `ced61ce087ca8b1a1d1e4339e521ef2c8f59718a` (resume delta) and `a3eb724aa15fcdcb2c590b859c5f5104c41a4daa` (full feature), using `--repository-profile deep --coverage-mode strict --fail-under 90 --event-name manual`, the exact `--candidate-sha`, combined `--backend-python` and `--tooling-python` XML, and qualified `--voice-worker-python` XML. Refresh the voice-worker phase if final source/provenance requires it. Do not reuse the historical 90.71% baseline as final coverage or lower a threshold/exclude changed code.

Current operating switches remain explicit: `ASTRAL_NODE_PROFILE=personal` selects the personal SQLite posture; institution storage remains PostgreSQL. `ASTRAL_NODE_MESH` controls personal-node mesh activation, and `FF_INPROCESS_AGENTS` controls bundled transport placement without bypassing authorization. `ASTRAL_ENV` unset remains production and fail-closed. Fixture-only development posture is not production configuration. Documentation/test repairs introduce no new feature flag or migration. MCP changes add the FFI/registry/sealed-secret/authority/egress contracts described above, without a new SQL migration or mesh event vocabulary.

## Remaining exit criteria and handoff boundary

[qualification.md](qualification.md) records 58 criteria: 45 automated, six partial, six live-only and one owner-approved. The persisted-note proof closes only 3.9. Shared 18-argument/seven-repeat fixtures and 25 pure gate vectors already exist; full composed-host conformance (3.11), final native/live standalone MCP (3.2), native catalog/provider custody, hosted-voice isolation and physical voice/offline/push/attack-rate demonstrations remain bounded gaps. Do not interpret local package/model timings or a configured-model surface diagnostic as those live demonstrations.

The final owner summary must name the adopted SHAs, flags/migrations/contracts, exact passing commands and reports, remaining skips/failures and live checks, and any required vault sync. Keep local source recoverable on the named branches. Current authorization does not permit another product push or any Kernel publication; older pushed branches do not authorize new external mutations. Refresh the curated knowledge vault under its own rules before declaring a major checkpoint complete.

## Historical stopping records

The following dated records are retained for recovery and explain older pins and failures. Their stop instructions and current-state claims were superseded by the resumed work above; they do not override present owner instructions.

**Date:** 2026-11-12. **State:** stopped at the owner's explicit request. All task agents and active
checks have stopped. No further integration is authorized by this handoff itself; resume only on a
new owner instruction. Product changes are local and unpushed. No phase, protected-CI, deployment or
release completion is claimed.

## Exact stopping state

Deep's primary composition pin remains unchanged. The qualified Plane update, portable contention
adapter and schema-reference security fix are preserved in a separate candidate; they were **not**
merged into the primary branch. Apple operations/wrap-key custody is also a separate, tested branch.
The production mesh runtime is not wired end to end.

| Repository / checkout | Branch and frozen revision | State |
|---|---|---|
| Deep primary | `feat/105-personal-mesh`; implementation through `5097101d`, prior handoff `f552b4e9` | Reliability/node/protocol lanes and authorization repairs integrated. Canonical verification harness requires the newer cleanup API; old-pin setup refuses before fixture writes. This stopping handoff follows those commits. |
| Deep candidate | `codex/105-plane-candidate` at `db602926`; worktree `/tmp/astral105-verification-candidate` | Clean, named local recovery anchor. Contains `5100bd29` (Plane pin/floor/schema guards + portable retry), `fa8d2ab3` (schema reference fix), `db602926` (dependency authority hashes). Not adopted by primary. |
| Deep schema lane | `codex/105-schema-egress` at `02dc072` | Same security fix already cherry-picked into the candidate; do not apply it twice. Implicit reference retrieval disabled, bounded errors returned before dispatch. |
| Kernel | `c015c8adfaa4360847f3f6e54106814ba2726d9a`, local-only; 1,182 workspace tests, formatting and strict all-target/all-feature Clippy pass | Exact replacement Apple/Linux/Mac artifact identities remain independently verified; accepted wheel/mailbox digests are in `config/astral-kernel-binding.json`. Do not substitute ABI-compatible f960 binaries, which lack the conservative effect change. |
| Projection primary | `feat/105-personal-mesh` at `5762791` | Identity, voice, ROTE, exact consent binding and voice setup/synthesis follow-up integrated locally. Full native gate remains failing. |
| Projection custody | `codex/105-apple-custody` at `1cb707c` (includes `758b093`) | Not merged. Device-only Ed25519/X25519 keys, stable marker access, pending/ready provisioning and fail-closed legacy/corruption/restore handling. Only process-local initialization serialization is claimed. |
| Primitives | `feat/105-personal-mesh` at `f4314c8` | Stable A2UI 1.0 schemas pinned to upstream `8bf644a37d680a227296b8c0849a7f4b328b8b73`; unreleased 0.13.0. |
| Plane | `feat/105-plane-sqlite` at `cbf621a35018c2491dc6ee5865a6798fe2d8a396` | Clean/frozen separate worktree `AstralPlane-wt/sqlite`. SQLite implementation and shared qualification complete within the scope below; primary Deep pin unchanged. |
| LETS / Vessel | LETS `1321306`; Vessel `fd0f086` | Device-profile work exists in LETS. Vessel convergence and Mac node packaging remain unfinished. |

Preserved unrelated Deep working-tree state: `AGENTS.md`, the pre-existing Plane/Projection component
pointer changes and `.playwright-mcp/`. Do not stage these blindly. Deep's Plane checkout remains at
`2762386`; it is an ancestor of `cbf621a`, but it was not advanced. The ROTE composition lane is still
separate. Do not recreate another machine's work because these commits are absent from a remote.

## Last recorded verification

| Scope | Result and limit |
|---|---|
| Deep root suite at `236354d0` | **23,248 passed**, 16 skipped, 44 passing subtests; 775.73s. Changed executable Python coverage 1,563/1,723 = **90.71%**. Precedes later harness/security work. |
| Initial Plane/portable-retry candidate full root run | **23,251 passed, two failed**, 16 skipped, 44 passing subtests; 381.17s. Failures were exact gitlink/config mismatch and dependency inventory hash; both corrected afterward. This run preceded the schema-reference fix. |
| Corrected candidate guards | **49 passed** after staging the exact Plane gitlink and refreshing the reviewed dependency hash. Historical cutover evidence was not rewritten. |
| Latest complete candidate focused run | **154 passed** in 18.14s: schema/reference/dispatch checks, dependency guards, full verification suite and real canonical cleanup probe. A full root rerun against final `db602926` is still required. |
| Canonical verification | Focused 50 passed; new source 33/33 executable lines covered. Candidate full verification 83 passed; real cleanup probe preserves neighboring issuer/run rows. Primary old pin: 75 passed, four failures/four setup errors at intentional preflight before writes. |
| Schema-reference fix | **71 focused passed**, 97% module coverage, 6/6 changed executable production lines covered. Existing `referencing==0.36.2` promoted to direct backend/CI dependency; every resolved package version/hash unchanged. Candidate authority hashes refreshed. |
| Portable contention | **28 passed**, including real PostgreSQL concurrent voice checks and new genuine/forged/post-callback/exhaustion regressions. Retries stay bounded to four attempts before callback start. |
| Plane exact frozen Linux source | **4,671 passed**, 169 explicit PostgreSQL/Win32 skips; 79.49s. Final PostgreSQL/Linux seams **307 passed**, seven backend/platform skips. Changed-line gate **1,605/1,692 = 94.85%**. Publication predicates/savepoint rollback, native lock classification, full cursor deadlines and fresh clocks covered. |
| Kernel exact integrated workspace | **657 passed**; formatting and strict workspace/all-target clippy passed. Existing `proc-macro-error2` future-compatibility warning remains. |
| Built Kernel bindings | All five Apple targets rebuilt in 413s; wrapper six tests passed on each platform. Linux arm64 wheel built with two compiler jobs in 226s; Python 3.11 store/signature/canonical-refusal/local-endpoint-reopen smoke passed with external networking disabled. |
| Primitives | **2,477 passed**; schema generation and Ruff passed; A2UI module coverage 96%. Test-only `regex` dependency declared in its owning development/CI groups. No publication. |
| Combined Apple consent/voice | **51 passed on each platform** using final Kernel artifacts; voice UI **three passed on each platform**. Voice model suite 39 tests with one optional diagnostic skip; seven real-weight parity checks passed. |
| Unmerged Apple custody | **38 passed on macOS and 38 on iOS 26.5**, zero failures/skips. Real Keychain round-trip and hardware-reporting tests were explicitly not run for this change. |
| Latest primary full macOS gate | **1,345 tests, one skip, 32 failure records across 23 testcases**, exit 65; 283.116s. Four records are known scheduled-work baseline failures; **28 accessibility/window/action records remain unqualified**. No fixture workaround or weakened assertion landed. |
| Mac local-input comparison | The formerly failing CGEvent testcase passed on exact baseline `102ea6a`, primary `5762791`, and within the latest primary full run. Its earlier six assertions were not reproduced; this does not qualify the full native gate. |

Earlier combined nested-module diagnostic: 3,471 passed, five failures/four setup errors. Two timing
failures passed unchanged independently; canonical-owner failures drove the repaired harness and
candidate proof above. Run nested suites separately as CI does. A prior full run killed by overlapping
Docker Rust compilation is invalid and must never be counted as a pass or a product failure.

No live backend/client end-to-end mesh conversation, real biometric approval, interactive enrollment,
real microphone/speaker/route qualification, protected CI, deployment or release was performed.

## Plane metadata and adoption boundary

- Package/lockfile: `0.1.12`; schema: `089.022`.
- PostgreSQL migration digest: `dde302967743cf8f6f5d55e82f8355ef405823dfee638909c3a2f11b85f5cb27`.
- SQLite migration digest: `e4ae392ed2951f1446cf065185a6b348bbed49c3e8cf37ca81546874887f6ef5`.
- SQLite structure digest: `5f45079d23dc20e1dc0bb1367e8076c55a582ba7df7a76224fc0cdbe1a2b4d0b`.

A Plane wheel was built into `/tmp/astral105-plane-wheel`; it was not installed or smoke-tested in a
new task image before stopping. Source-overlay qualification is not a production image rebuild.
Deep still calls `create_postgres_runtime` and expects PostgreSQL's registry. Its SQLite host selection
and exact SQLite composition metadata remain to implement; changing the library pin alone does not
enable a SQLite node. PostgreSQL timeline/failover/joint-restore APIs remain backend-specific. Mac
native no-replace publication failures reproduce on Plane base `529120e`; Linux counterparts pass.

## Resume order

1. Read current repository/vault instructions and statuses before changing anything. Inspect the
   named candidate rather than redoing finished lanes. Qualify its final source with the full backend
   root suite and separate touched module suites, verify the built Plane wheel/new API in an isolated
   image, then deliberately integrate the three candidate commits and exact Plane checkout/pin/floor.
   Preserve the unrelated working tree. Product push remains unauthorized.
2. Review/integrate Apple custody branch `1cb707c` with its parent; it is not in primary. Complete real
   Keychain/Secure Enclave verification and investigate the latest full native accessibility failures.
   Do not classify all failures as baseline or weaken native gates.
3. Continue [runtime-composition-plan.md](runtime-composition-plan.md): verified durable roster/key
   providers, enrollment/feed/restore coordination, engine/gate/consent/tool composition and complete
   FFI; then the actual native chat/voice link. `LogEventSink` exists but is not a full runtime.
4. Resolve the uncapped-read/action-receipt contract seam and model-token accounting across attempts.
   Preserve current authorization/PHI/egress/confirmation gates and unknown-effect outcomes.
5. Finish Deep SQLite composition, ROTE component adoption, supervised Mac node packaging and Vessel
   loop convergence. Voice release needs the owner-published immutable model revision; live sign-in
   and biometric decisions stay with the owner. Android/Windows runtime ports retain their scoped plan.

## Retained local evidence and services

Durable local diagnostic copies and SHA-256 manifest:
`.superpowers/sdd/handoff-2026-11-12/retained-evidence.json` (gitignored). It retains 21 reports, logs,
runners and the Plane wheel. These are diagnostics, not canonical protected release evidence.
Original XCResults remain under their `/tmp/astral*` derived-data paths and may be purged; important
counts are preserved here. The named candidate branch keeps its commits recoverable if `/tmp` is lost.

Backend qualification used only isolated PostgreSQL 17/task containers, unprivileged init-enabled
workers, exact read-only component sources, Node 24, scratch tmpfs and a masked `.env`. The shared
`astraldeep` stack/database were not recreated or modified. The task-owned `ad105-codex-pg` fixture is
stopped without removal for this handoff; restarting it is a future test setup action, not deployment.
No model weights, credentials, generated user code or private runtime data were staged.

## Second stop, later on 2026-11-12

The owner said "continue" after the first stop, then asked for a stopping point again. Every agent and
every task build is stopped, task containers are stopped (not removed), and nothing is pushed. The
first stop's state above still holds except where this section says otherwise.

| Lane | Branch and revision | State |
|---|---|---|
| Kernel runtime | `k/runtime` at `55ad4fd`, from `codex/105-kernel-integration` | Real facade and FFI for mesh creation with attestation, pairing over `astral/pair/1`, durable roster and keys, chats, sync with relays configurable, listener. Eight two- and three-runtime mesh flow tests pass. Rust traits `HostKeys`, `PresenceSigner`, `ConsentPromptSpec` in `astral-kernel/src/consent.rs`; FFI `HostSigner`, `ConsentPrompt` in `astral-kernel-ffi/src/host.rs`. `Kernel.open(config, signer, listener)` replaces the spike constructors. Not done: mailbox client in the sync loop, restore marker after a pairing join, recovery-word add flow, `preview_pairing`, `record_spoken`, Apple and Linux binding builds, the Python binding test (written, uncommitted). Uncommitted follow-up work is saved as `wip/k-runtime-followups` (`6740e9f`). |
| Kernel turns | `k/turns` at `024c301` | Committed: the receipt seam (effect kind is a verifier input, reads carry no receipt, `ticket.receipt_unexpected`, contract commit `3e146215` here), model-token metering, per-attempt model admission that fails closed with `admission.no_context`, label sources on gate requests, no redirects to model or MCP servers. `astral-engine` 180 tests pass. `TurnService`, providers, egress validation, the SQLite allowance store and the turn FFI were in progress and are uncommitted and untested; they are saved as `wip/k-turns-turnservice` (`7cb66f8`). |
| Apple mesh app | Projection `feat/105-mesh-app` at `55a83ec` | Custody branch merged. Keychain service names derived from the bundle identifier. `MeshKernelAPI` protocol mirroring the FFI, an in-memory kernel, `MeshRuntime`, and mesh screens (create with recovery words, join with code and QR, add device by paste or scan, devices with remove and stop, chats from the log, consent cards, provider form, allowance, voice link). 81 mesh tests pass on macOS and iOS. Full macOS `AstralAppTests`: 1,426 tests, six records in one CGEvent test that passes alone. Full iOS: four records in the known scheduled-work test. Not bound to the real kernel: a production build says the mesh runtime is not included yet. Asks for FFI `previewPairing` and `recordSpoken` and a handle resolver. |
| Deep SQLite node | `feat/105-sqlite-node` at `dd61b1a6` | Merged the Plane 0.1.12 candidate (`4cbdcff0`), added SQLite or PostgreSQL selection for Plane with the exact SQLite registry pin (`e7240511`), composed AstralKernel and `astral-rote` (`044c90b8`, `dd61b1a6`). The full root suite on this branch and the SQLite node smoke were not finished. One uncommitted test edit is saved as `wip/feat-105-sqlite-node` (`12949f8f`). |

### Resume order now

1. Finish `k/turns` from `wip/k-turns-turnservice` (review it, test it, commit), then the runtime
   follow-ups from `wip/k-runtime-followups`.
2. Merge `k/runtime` and `k/turns` into one kernel branch: adapt `PresenceSigner` to the turns lane's
   consent signer, append the turn variants to `KernelChange`, reconcile the `ticket_vectors` count,
   then build the Apple bindings and the Linux wheel and run the Python binding test.
3. Bind Projection `feat/105-mesh-app` to the generated bindings and update the wrapper's
   `KernelService` for `Kernel.open`.
4. Finish `feat/105-sqlite-node`: the full root suite and module suites, then boot a personal node on
   SQLite end to end.
5. Then the command-line peer that loads the model credentials from `.env` as a sealed item, the node
   joining the mesh through the Python binding, the Mac node pack, Vessel convergence, and the live
   tests on the Mac and the iPhone simulator, which need the owner for Keycloak sign-in, Touch ID and
   the Hugging Face upload of the LFM3 weights.

Task containers stopped without removal: `ad105-codex-pg`, `adwt-pg-phase-r`, `adwt-pg-rote`,
`plane-pls-pg`, `plane-sqlite-contract-pg`, `sqlite-node-issuer`. The shared `astraldeep` stack was not
touched.

## Third stop, 2026-11-13

The owner asked for a stopping point and approved pushing every spec 105 branch except
AstralKernel. All agents are stopped. Nothing is merged to a default branch, released or deployed.

### Where everything is

| Repository | Branch and head | State |
|---|---|---|
| Deep | `feat/105-integration`, named `final` worktree; stream synchronization plus reviewed voice dependency guard repair | Freeze this clean candidate after exact pins/docs, record its SHA outside the candidate tree, rebuild runtime/test images and execute all four canonical groups unchanged. No final full-group pass is claimed here. |
| Deep | `feat/105-integration`, named `final` worktree; stream synchronization plus reviewed voice dependency guard repair | Freeze this clean candidate after exact pins/docs, record its SHA outside the candidate tree, rebuild runtime/test images and execute all four canonical groups unchanged. No final full-group pass is claimed here. |
| Kernel (local only) | `k/integration` 34ef4a5 | Merges `k/node-mint` and `k/attach` with a schema 2 to 3 log upgrade test. The full test run, flake loops and artifact rebuild after the merge did NOT finish. Last verified artifacts are 22513f9 (1,054 tests) and the two lanes alone (1,084 and 1,111 tests). |
| Projection | `228a58fb` adopts the NodePack Kernel c015 pin and contains source checkpoint `730d82e` (voice), `2916899` (native surfaces), and native MCP management | Final combined native gates and physical checks remain required against exact rebuilt artifacts. Its NodePack Deep source stays at the historical 13b checkpoint until a separate post-freeze descriptor-only update; do not create a mutual Deep/Projection gitlink cycle or treat that pack as the final runtime. |
| Vessel | Exact c015c8a wheel/source adoption; historical `76bb18a8` had 147 passed/one skip in network-disabled Linux Python 3.11 | Current exact adoption SHA/test result belongs in the external candidate inventory after commit. Hosted distribution qualification remains separate. |
| Plane | `feat/105-plane-sqlite` f8f2a63 | SQLite backend with the write-lock fix. |
| LETS, Primitives | `feat/105-personal-mesh` 1321306, f4314c8 | Unchanged since the second stop. |

### Live state on the owner's Mac

- The redesigned localtest apps (503988a, kernel 22513f9) are staged. The iPhone simulator
  33EFFC80 opens the existing mesh with its chats and three devices.
- The Mac app waits on a Keychain prompt for `com.personalailabs.astraldeep.localtest.mesh-device`
  caused by the new ad-hoc signature. The owner answers it, then checks chats, devices and provider.
- Backup of both apps' data before staging:
  `AstralProjection-wt/localtest-backup-20261113/` (no Keychain items). Restoring
  `mac/ApplicationSupport` is safe if the Mac fails after the prompt.

### Resume order

1. Kernel `k/integration` 34ef4a5: run `cargo test --workspace --locked --no-fail-fast` with
   vectors from this branch, loop the known flaky tests 20 times, then rebuild the Apple
   xcframework, the Linux wheel (smoke), the Linux mailbox and the macOS CLI with SHA256SUMS.
2. Deep: pin the new wheel and mailbox, let the node call `mint_ticket_for_request` for executor
   tools, and trace why a Vessel node listed no tools ("node signing key is missing").
3. App: bind attachments and the merged artifacts, run the AstralCore package tests, re-stage with
   a fresh backup.
4. Write the missing automated tests listed in [qualification.md](qualification.md), smallest first.
5. Measure the live valid-surface rate with the provider credentials from `.env` through a script.
6. Owner: the UX review in the staged apps, invitation pairing, and the voice test on the Mac.

### Open gaps

- No mesh event tells an owner device that a node needs consent, so node tools that need it fail
  closed with `consent.required`.
- The model does not see attachments, and nothing imports A2UI from tool results automatically.
- There is no owner-initiated allowance transfer and no way to create a PHI chat in the app.
- No Android or Windows kernel build. Vessel CI needs an arm runner and the wheel artifact.
- The website downloads change stays unpushed until the new client is ready to deploy.

## Fourth stop — 2026-11-13

The owner requested a quick stopping point, saving the work to the wiki and GitHub, then stopping. The owner explicitly confirmed **keep AstralKernel local**. All task agents, tests, builds, metadata streams and task PostgreSQL fixtures are stopped. No main-branch merge, deployment, package publication or app-store submission occurred.

Source qualification before this documentation commit was Deep `6d5307b1`; Kernel `a499055` remains local and is also preserved in `AstralKernel-wt/spec105-stop-20261113.bundle`. Vessel `936dc1ff` and Projection `feat/105-mesh-final` `5529983` are handoff anchors. Projection's unqualified accessibility fixture proposal is separate on `codex/105-stopped-ax-readiness` (`e255ee8`), excluded from the feature branch. Kernel's rebuilt 14-artifact manifest SHA-256 is `c7803a378f588af0c3cc519f00d39f9344e65db124fe635b23788362e3c7c6f9`; it is copied beside the structured [stopping summary](evidence/stop-2026-11-13/summary.json). No private runtime data, invitation payloads, credentials, model weights or compiled artifacts are included in these commits.

### Verified work and incomplete qualification

- Kernel: 1,186 passed, zero failures, one existing multicast diagnostic ignored; strict Clippy/formatting passed. Idempotent budget transfers no longer force another sync. All five Apple targets, Linux arm64 wheel/mailbox and Mac CLI were rebuilt and digest-verified; offline Python runtime smoke passed. Live canonical-Mac CPU improvement remains unchecked.
- Incoming peer calls: a real stop-after-admission execution defect was repaired with trusted context/lifetime and final authority checks. Focused 247 tests and root's independent 35 tests passed; changed Python coverage was 119/120 (99.17%). The final precise peer-marker repair passed 114 tests and 100% admission-module line/branch coverage. Python-local whole-turn requests still fail closed at empty consented scopes; their two zero-dispatch diagnostics do not prove a working local tool or complete shared-host parity.
- Final Deep candidate: production/test images built and fail-closed production exit 78 verified. `tests_2` completed with **11,693 passed, one failed, 13 skipped and 39 passing subtests**. Sole failure: `test_work_runtime_postgres_088.py::test_completed_result_requires_current_read_identity_without_reusing_execution_authority[logout_during_delivery-operation0]`, expected one model call but got zero. Root cause remains unproven; the traceback's source action state is not proof the submitted operation succeeded. `tests_1` and `persistent_agents` were explicitly interrupted; `modules` and combined strict coverage were not run. Do not reuse mixed candidates as a passing four-group qualification. Detailed local evidence: `/tmp/ad105-resume-evidence/frozen-6d5307b1/`.
- Voice worker: fresh source-bound image suite 382 passed, two explicit isolation skips, 90.36% coverage. Vessel: 150 passed, zero failures/skips, network disabled. Projection/NodePack Python: 4,487 passed, seven skips, 33 passing subtests; Windows Python: 2,567 passed, 20 skips; immutable-source replay one passed. Fresh changed-line coverage was 98.09% full feature and 97.81% resume at Python-equivalent source through `44f00a9`.
- The owner verified correct Mac microphone transcription and reply. It spoke twice: a factual fast acknowledgement followed by the full answer. The neutral acknowledgement catalogue fix passed 36 focused tests, but the canonical Mac still has the earlier installed `44f00a9`/C015 build. The physical phone has a499 and normal mesh-mode entry passed a real UI test while preserving its existing server credentials. Physical voice/model download, offline operation and APNs delivery remain unqualified.
- Pairing did not finish. The owner scanned the Mac QR and clicked Approve; no second confirmation appeared. The Mac eventually reported not approved, and no new roster membership was committed. A synthetic control passed and the actual Add Device modal reproduced the blocked consent sheet with SwiftUI's single-sheet warning. Five absolute expiry mappings were fixed and 14 tests passed. The consent presenter-lease fix is saved **unfinished** at Projection `c922960`; its green gate and Release build were interrupted. See Projection `apple-clients/ManualQA/spec105-stopping-point.md`. Last observed phone state was waiting for approval. No further pairing, reset, sign-in or installation was attempted after stopping.
- The final Mac pack for Deep `6d5307b1`/Kernel a499 was assembled under `~/Library/Caches/AstralNodePack/pack-6d5307b1` but was not live-qualified before stopping. Earlier Deep9a57/Kernelc015 pack install/start/stop and authenticated bundled-tool dispatch passed. Native provider-to-node custody and mesh APNs registration remain separate gaps.

### Resume safely

1. Read current repository instructions and the latest wiki checkpoint, then inspect actual Git and app state. Kernel remains deliberately local, so a remote-only checkout cannot resolve that component without the local bundle/artifacts. Original non-task dirt in the primary Deep checkout was preserved.
2. Finish the saved narrow consent tests and review host/entry/generation cancellation semantics. Build and verify the stable-signed canonical Mac candidate only after the tests pass; take a fresh stopped data backup before installation. Coordinate one fresh unexpired pairing attempt with the owner and let the owner handle approval/authentication. Do not reuse the expired waiting transaction or reset keys/data as a workaround.
3. Qualify the separate AX readiness proposal before integrating it. Run combined Mac/iOS gates against the actual final binaries, then hardware voice/offline/pairing checks. Preserve the designated requirement and Keychain groups to avoid another signature-transition password cycle.
4. Diagnose the remaining work-runtime full-run failure without weakening authority or timing gates. Run all four backend groups at one new clean source/image, then both strict coverage bases. Complete remaining live/security/mesh-push criteria before any readiness claim.

All post-resume work remains stopped until a new owner instruction. The website downloads branch is saved for handoff; its changes are not deployed.

### Cleanup completed — 2026-11-14

At the owner's explicit request, all local Docker containers, images, managed volumes and build caches were deleted, including the data in those volumes. Final Docker inventory was zero containers, zero images, zero volumes and zero build-cache bytes; Docker Desktop was then stopped. Recorded image IDs above are historical evidence, not available local runtimes: rebuild services/images before resuming. Host Git sources, native app data, model weights, backups, private diagnostics, the local Kernel bundle and qualified host artifacts were preserved. No implementation or testing resumed during handoff/cleanup.

## Fifth checkpoint, 2026-11-14

This section supersedes the running state above. The owner resumed the work, was away for most of it, and allowed pushing AstralKernel once ready. Nothing is merged to a default branch, deployed, released or submitted.

Every path under `/tmp` named earlier in this file is gone. Evidence for this checkpoint lives outside Git at `/Users/sam/Desktop/Work/AstralDeep-wt/evidence-105/`. A structured copy of the numbers is in [evidence/resume-2026-11-14/summary.json](evidence/resume-2026-11-14/summary.json).

### Saved branches

| Repository | Branch and head | State |
|---|---|---|
| Deep | `feat/105-integration`, frozen backend candidate `bd197b828cd5d9bda4e613eaee6c34e39a75e8d7` | Pushed. Later commits on the branch are documentation only. |
| Deep | `feat/105-node-turn-scopes` at `4de30d69` | Pushed. A proposal, not integrated. Needs owner sign-off. |
| Projection | `feat/105-mesh-final` at `377ee45d1f7a64071762bfdbd9a7ba28a57daf2e` | Pushed. Consent repair, accessibility work, hosted voice guard, node pack driver and pin. |
| Kernel | `k/integration` at `a49905544244c79dd0f5b847ac4c1618e4a055e5` | Pushed to origin for the first time. Artifacts are not published. |
| Kernel | `k/composed-gate` at `97aba18` | Local only. Deliberately red. See 3.11 below. |
| Vessel, Plane, LETS, Primitives | Unchanged from the fourth stop | No new work. |

### Backend qualification, complete on one source

Deep `bd197b82` differs from the last tested source `6d5307b1` by three test fixes and one contract fixture. No product code changed.

One test image, `sha256:aa6fa0b4f5a2f35d0260b381021f27458f15bd5be14efd45c1e63c114d39060c`, ran all four groups. The tree was clean and at the frozen SHA before and after.

| Group | Collected | Skipped | Failures | Errors |
|---|---|---|---|---|
| `tests_2` | 11,708 | 13 | 0 | 0 |
| `tests_1` | 12,025 | 3 | 0 | 0 |
| `modules` | 9,072 | 17 | 0 | 0 |
| `persistent_agents` | 1,008 | 0 | 0 | 0 |

Voice worker on images built from the same SHA: 382 passed, two isolation skips, 4,219 of 4,669 lines (90.36%).

Strict changed-line coverage with a 90% floor, merged from exactly those four groups plus the voice worker:

- Resume delta against `ced61ce0`: 339 of 347 lines, 97.69%, pass.
- Full feature against `a3eb724a`: 4,583 of 4,685 lines, 97.82%, pass.

The comparison added `--empty-diff not-applicable`, as CI does. Both diffs had measurable lines, so the flag had no effect.

This is local qualification. It is not protected CI.

### The three intermittent failures were test defects

Each was reproduced by an injected stall or hold before any fix. None is a product or security defect.

1. `test_work_runtime_postgres_088.py`, zero model calls. The fixture set a 5 second claim lease, below the production floor. A worker stall let the lease lapse, the runner failed the operation before any model call, and the wait accepted a completed lifecycle without checking disposition. The fixture now keeps the production lease and the test asserts the disposition.
2. `test_evidence_surface.py`, a denial that did not raise. Helpers bounded each request by 5 seconds of wall clock. After a stall the test released its late child while the request was still running. The privacy proof itself closes in a `finally` and refuses afterwards. The bounds are replaced by explicit waits.
3. `test_work_controls_postgres_088.py`, a 503 for a concurrent exact retry. Bounded transactions cap each lock wait at 100 ms by design (feature 088 verification record). The test now accepts that documented refusal for the loser, still requires exactly one applied control, and checks a single audit row. A new event-driven test covers the held-lock case.

The natural cause of the stalls was not proven. CPU load inside the container did not reproduce them. Starvation of the whole Docker VM while the Mac ran several builds is the leading guess.

One product observation from item 1. Lease renewal starts only after authority refresh, admit and bind. A stall of about 20 seconds in that window would fail a real operation the same way. That is fail-closed. Whether it is acceptable is an owner decision.

### Harness

The gate runner was rebuilt from the Codex session logs and lives at `evidence-105/backend/harness/`. Three things differ from the old one.

- The old runner's guards never fired. macOS `/bin/bash` 3.2 ignores a failing bare `[[ ]]` under `set -e`. Earlier runs therefore never enforced their SHA and clean-tree checks. Every guard now exits explicitly.
- The source is baked into the test image as a full clone owned by the test user, with canonical remotes. Each run ends with a `git status` check on that tree.
- The PostgreSQL fixture is created and destroyed per run from CI's pinned image.

### Apple client

Pairing consent repair, three commits on Projection (`71b7d18`, `e66aad6`, `0787ade`):

- The consent sheet never appeared even with no other sheet open. An idle host did not observe the incoming request. This was the main defect.
- Closing a parent sheet on macOS left a hidden request alive until the 120 second timeout.
- A retired request could leave the system authentication prompt on screen.
- Four more sheets could hide consent. Each now hosts it.

The exact prompt bytes, digest binding and signature binding are unchanged. The only approve call in the product is the Approve button.

Native gates on Projection `8931bd8`, with no retries. The two later commits change only `pins.json` and the provenance ledger.

| Gate | Passed | Failed | Skipped |
|---|---|---|---|
| macOS AstralAppTests, two consecutive full runs | 1,618 each | 0 | 2 |
| iOS simulator, iPhone 17 Pro, iOS 27.0 and 26.5 | 989 each | 0 | 2 |
| AstralCore package | 621 | 0 | 1 |
| Kernel Swift wrapper | 10 | 0 | 0 |
| Windows client Python | 2,567 | 0 | 20 |
| Projection Python at `377ee45` | 4,528 | 0 | 8 |

The accessibility proposal is integrated together with two fixes it exposed. The workbench Stop button turned accent purple in a focused window and is now error red. The surface text reader was reading a transparent bitmap and now composites onto an opaque background. The 21 earlier accessibility failures were environmental. They passed on a second run of the same source and their trigger was not found.

A signed Release Mac candidate is staged and not installed:
`AstralProjection-wt/localtest-stage-20261114T2008Z-final/AstralDeep-localtest.app`. Its designated requirement, team, bundle identifier and entitlements are identical to the installed app. It was built from `8931bd8`, which has the same Swift sources as `377ee45`.

No physical iPhone build exists. The repository has no script for one.

Keep Xcode derived data and the source path out of `~/Desktop` for unsigned test runs. A privacy prompt for the Desktop folder otherwise freezes the test host.

### Node pack

Pack `3.11.18-bd197b82-a499055` is assembled at `~/Library/Caches/AstralNodePack/pack-bd197b82`. Archive 146,303,689 bytes, SHA-256 `155fe50401a9bd1c8ebd632f2c40a14423b81e283a30a7b63192673c2b431400`.

- Swift host tests: 48 executed, 0 failures.
- Real download, install, start, health and stop: passed.
- Committed driver `apple-clients/NodePack/qualify/tool_turn.py`: 14 of 14 checks. Missing and forged credentials get 401, the owner gets 200, 100 tools are offered, `generate_dynamic_chart` runs and persists.

Limits. The driver uses a fake model and a loopback issuer in development posture. The pack's macOS Kernel wheel and mailbox come from the `kernel-final-a499055` cache, which is not part of the 14-artifact qualified manifest.

### Findings that need the owner

1. **Node-run turns cannot use tools, and the obvious fix is unsafe.** Deriving the scope ceiling from mesh grants would let in-process tools dispatch with no Kernel ticket, and would open every enabled tool that shares a scope. The proposal branch instead mints a ticket for every call, carries an exact per-tool allowlist and refuses meta tools. 1,917 tests pass with it and changed lines are fully covered. It touches the central dispatch path, so it waits for sign-off.
2. **Node whole turns do not answer at all on the current source.** A probe with a text-only model ended in `node.no_answer`. The cause was not traced. The existing criterion 1.1 test uses a stub runner, so it does not show this.
3. **Sandboxed agents are refused by the normal gate stack** during a real model turn, and a node removed mid-turn keeps minting for itself.
4. **Node turns need a Keycloak offline grant and a live token exchange on every request.** That conflicts with criterion 1.2 for node-run turns.
5. **Provider key custody.** The Kernel can seal a key to a node. Nothing on the node can open it for the Python loop, and the app seals to every device without telling the owner.
6. **Mesh push cannot work as built.** The app enrolls a different device id than the gateway looks up, mesh mode skips registration, and the sender would need the publisher's APNs key on every personal node.
7. **Composed-gate conformance found two disagreements** between the contract and the Kernel host. Both fail closed. The host reports `allowance.share_spent` or `allowance.solo_cap_reached` where the contract says `allowance.exhausted`. A device removed while consent is pending is reported as a consent denial where the fixture expects `gate.device_not_on_roster`. The choice is between amending the contract and changing the Kernel. A Kernel product change means rebuilding and re-adopting all artifacts.
8. **Hosted pipelines never exercise the Kernel.** Deep's image build allows missing Kernel artifacts and the real-Kernel tests skip without the wheel. Projection CI cannot build the app from a clean checkout. The Kernel has no CI.
9. **Hosted voice remnants.** The app still advertises `voiceTransport = "livekit"`, the LiveKit package is still linked, and the Watch target posts to `api/voice` by design.

The full read-only audit is at `evidence-105/audit/exit-criteria-audit.md`.

### Still required before any readiness claim

- A fresh pairing attempt with the owner on the staged Mac build. Take a stopped data backup first. Do not reuse the expired transaction or reset keys.
- Physical checks: iPhone voice and offline operation, interruption timing, push delivery, hand-off between devices.
- Mac voice recheck for the duplicate acknowledgement fix, and the idle CPU check on the canonical Mac.
- The owner decisions listed above.
- Protected CI on the pushed branches.

### Live pairing, 2026-11-14

The owner paired the physical iPhone 13 Pro to the canonical Mac mesh. This is the first live-verified pairing for spec 105.

- **Mac.** Stopped data backup at `AstralProjection-wt/localtest-backup-20261114T204037Z-pre-final-pairing`, verified file by file. Then the staged build from Projection `8931bd8` was installed at `/Users/sam/Applications/AstralDeep Localtest.app`. Its designated requirement matched, and the existing mesh opened with all chats and devices and no Keychain prompt.
- **iPhone.** The app's data container was copied to `AstralProjection-wt/physical-backup-20261114T204416Z-pre-final-pairing` (20 MB). The app was removed from the phone at that moment, so the staged device build from Projection `b206e44` was installed fresh. The phone therefore joined as a new device with none of its earlier chats.
- **Flow.** The Mac showed a fresh invitation. The owner scanned it with the phone, matched comparison codes, approved on the Mac and completed the system authentication.
- **Commit, read-only from the Mac store.** The roster grew from 10,082 to 11,846 bytes and gained the peer `3lcED1m3…`. A sync session with that peer ran at 20:45:52 UTC. The phone's own `auth.transfer` event was accepted into the Mac log.

Roster membership requires a presence signature, and the product's only approve path is the consent sheet's Approve button. So the consent repair is live-verified on the Mac for the device-add path.

Two observations.

- The Mac wrote a `key.credential` event when the phone joined. Provider credentials were resealed to the new device with no prompt, which matches owner finding 5.
- The phone shows as "Needs sign-in to verify" on the Mac until it attests through Keycloak, as expected.

Still unverified: chat sync between the two devices, and every other physical check listed above.

Chat sync was then checked both ways with the owner. Read-only from the Mac store:

- **Mac to iPhone.** A new Mac chat (created 20:47:25 UTC) and its model turn were followed by a sync session with the phone at 20:47:29. The owner confirmed the chat on the phone.
- **iPhone to Mac.** The phone's chat create, user message and complete model turn (phone feed, sequence 2 to 21, 20:48:31 to 20:48:57) are in the Mac log.
- **Speed.** The owner saw the reply arrive slowly on the Mac. The phone's own model call took 10.3 seconds to its first response, against 0.7 seconds for the same kind of turn on the Mac. The last recorded session followed about 55 seconds after the turn ended. The store keeps only the latest session per peer, so the exact delivery time is not known.
- **Credentials.** The phone also wrote a `key.credential` event, so it is using the provider credential that was sealed to it on joining.

This live-verifies chat sync between paired physical devices on the same network with internet available. It does not verify sync with the internet off (2.1) or airplane-mode queueing (2.2).

## Codex continuation, 2026-11-14

The owner requested completion of spec 105 and active manual tests on the connected physical iPhone. New work stays local: no product push, merge, deployment or release has been performed. Phone mirroring and Xcode UI automation were briefly authorized, then revoked; subsequent physical checks are manual. No automated phone UI test or install ran.

### Physical results on the existing builds

- Airplane Mode with Wi-Fi off: history remained readable; a new chat/message survived force-quit and reopening while offline. After reconnection, the exact synthetic message appeared in the canonical Mac UI and its phone feed events were accepted into the Mac store. This qualifies persistence/delivery in 2.2, not an offline answer or LAN-only sync.
- Ordinary chat recovery failed: reopening removed the transient working/queued indicators but left a signed turn start without a turn end. The model did not automatically resume. Native startup resumed Runs only, and the native mapping discarded model notices.
- The iPhone voice model download succeeded. Offline recognition and a generic spoken acknowledgement worked. Stopping/restarting voice once terminated the app; the specific Jetsam report says per-process-limit at 196,659 pages of 16,384 bytes (about 3.22 GB). A later attempt restarted successfully and the queued question completed after reconnection. No completed offline model answer is qualified.
- The Mac arithmetic answer was complete in the UI, but speech stopped before its final number. Isolated real-weight synthesis preserved streamed/full PCM lengths; one seed mispronounced the final word. The physical cutoff cause remains unresolved. New playback/control timing instrumentation is diagnostic and must not be described as a measured acoustic interruption result.

### Local source and verification

| Repository | Local branch/checkpoint | Evidence and remaining work |
|---|---|---|
| Deep | codex/105-finish, through 456d7a30 | Postcommit answer publication, originating-session correlation, exact granted tool/hash ceiling, per-call tickets, final revocation recheck and delegated-tool attenuation integrated. Fifteen focused suites passed 419 tests, zero failures/errors/skips, using SHA-bound overlays on the bd197b82 qualified test image. This is diagnostic integration evidence, not a new frozen complete backend gate. |
| Projection | codex/105-chat-progress: 0ce9166 and 4f01659 | Checksum reads release each temporary buffer. Exact weight-file reproduction reduced RSS from about 1.26 GB to 10.8 MB with identical SHA. Progress/notices restore truthful durable state and preserve explicit incomplete warnings. Scoped Mac 104/iOS simulator 101 tests passed; VoiceModelStore 18 passed on each. Source is not installed on either physical client yet. Recovery scheduling and playback instrumentation continue. |
| Kernel | codex/105-chat-recovery | Resume-only expected-turn API, prompt eligible on-device fallback, deterministic wheel packaging, current verifier authority, denied-consent precedence and composed-host normalization implemented and tested. New verified-principal API and internal signed node-tool routing continue. Artifacts built from 66e10ee and b47d0bf are immutable diagnostic candidates, not final adoption. |

The 66e10ee Apple payload was verified against its 11-file source-bound manifest and copied only into Projection's ignored Artifacts directory for compilation. The prior a499 payload is preserved outside Git. Deep/NodePack production composition pins have not been advanced to an interim build. Requalify and adopt one exact final Kernel source/artifact set before full native/backend qualification.

The full Python reference suite passed 1,344 tests after installing its declared pinned requirements into an isolated evidence-directory environment. The primary .venv lacked biscuit-python; its earlier three collection errors were an environment failure, not a passing suite. Composed fixture changes preserve the pure exhausted code but retain the actual holder journal reason on exactly three rows.

### Owner decision and remaining closure

The owner chose Kernel-run node-requested turns with provider credentials kept inside the Kernel; no Python credential lease/export. Python keeps the agent host, permission/policy/PHI gates, auditing and executor dispatch. Personal mesh authority must come from a live verified admission plus the Kernel's attested principal, not profile/environment strings or per-request Keycloak exchange. Scheduled, persistent and institution-edge behavior retains its existing contracts.

Still required: finish and qualify safe native recovery, Kernel-hosted node turns and credential recipient controls; verify patched physical voice/offline behavior and interruption; complete composed Python-host conformance, generated-surface/model quality measurements, hand-off/crash checks, remote MCP and adaptive attack runs, publisher-operated push wake, final NodePack adoption, clean-source full gates and protected CI. The current installed clients and live state are not the new source qualification.

Durable local evidence: sibling evidence-105/codex-20261114-live, including native-progress, voice-memory, voice-tail, node-integrated-final, node-tool-admission and kernel-rebuild. Raw device data stays outside Git. The curated vault checkpoint was updated and pushed separately at 11b2e80, including wake custody, removal/cancellation repair and the typed local proposal smoke. Product changes remain local.

### Wake and local model continuation

Deep `997ec4db`/`a6cb7da8` integrate the publisher-only encrypted registry/gateway and the verified-route node HTTPS client. The implementation-lane 281-test run has zero skips; exact immutable overlays and invocation are under `publisher-mesh-push/node-wiring-green2`. The publisher is disabled by default and requires explicit HTTPS configuration/APNs publisher deployment; no physical delivery was performed. New audit records use canonical issuer+subject owner IDs; legacy raw-sub listing does not display them.

Kernel `f2a4548`/`5322e18` repair verified own-removal delivery and retain normal removed-feed append refusal for cancellation, with no fake interrupted result. Kernel `8993799` and Projection `7034d9f` add host-local versioned typed tool proposals and a narrow canonical card/text schema. Six deterministic Swift tests pass; engine, UI grammar, normal surface validation/dispatch and strict Clippy pass. A real normal-Kernel smoke at clean `8993799` has one allowed effect, one valid surface and one exposed generated adaptive response with zero outside effects. The first broader replacement diagnostic was interrupted after nine surface trials: all generated valid surfaces, but only three turns completed; a native transcript repair is being verified before a complete rerun. Earlier 0/50 negative measurement remains evidence for its exact older source. New artifacts/pins, native cancellation adoption, final same-source backend/native checks and protected/hardware verification remain unfinished.

Root independently reviewed wake authority/target/current-feed/transaction checks and native queued Stop retirement/join semantics; final compiled API gates remain required. Evidence is retained under `codex-20261114-live/on-device-tools`, `publisher-mesh-push`, `kernel-rebuild` and agent native evidence. No new product push, publication, merge, deployment or release occurred.

### Owner amendment: automated voice qualification

On 2026-11-14 the owner explicitly requested that live voice testing no longer qualify spec 105 and that code tests suffice for anything voice-related. Spec section 12.5, plan Phase 4, the voice rows in qualification.md and the task queue now apply that rule. All live microphone/speaker, audible-answer, acoustic latency and physical-phone/watch voice checks are optional diagnostics. Required software gates retain interruption control budgets with injected clocks, played-audio history, restart/buffering, verified installed assets/offline paths, paired-watch authorization/session fences, risky-action denial and zero hosted voice contact. Earlier live findings remain historical diagnostics and create no extra voice completion gate.

### Final fixture and packaging continuation, 2026-11-14

The owner voice amendment remains authoritative: automated tests qualify all voice requirements. The exact Deep ea73c7/Projection58/Kernel cf826 local artifact set and Mac pack were built without product publication. Kernel's final 17-payload manifest is c29fd5e5f61f6edeb8bb68dc26d7318a96011accad900152b134c1a5db98dbb5. The Mac pack archive is 146,058,414 bytes, SHA-256 3efadf0c3021b5560d309374fc8d8f47395a0635bc17feee77d667153db1151b; its actual Swift descriptor install/start/mesh/mailbox/health/stop and 14 packed owner WebSocket/tool checks pass. This owner turn is distinct from the signed peer-request tool route.

The integrated consumer now includes final native fixture corrections at Projection9215609 plus the separate NodePack Deep-source metadata commit (b83bc2cf). Production App code is unchanged from271972e; native tests run on the immutable921 snapshot with the verified cf826 ABI. The exact original mounted composer SystemAX assertions remain, with missingWindow reproduced on the original baseline in this environment. The Mac UI runner is killed before test connection even after valid existing development signing; no OS trust/security setting was changed. Full Mac unit and iOS unit/UI gates remain under execution. Root independently reran all78 NodePack Python tests after the metadata followup, with no failures or skips.

The ea73c7 backend group2 collected11,827 cases and retained six failing obsolete assertions: two still expected Apple's removed LiveKit transport, one attempted an unauthorized read after device removal, and three expected a completed turn after Stop now correctly fails it. Fixture repairs preserve the exact refusal codes, zero-effect and audit checks. The remaining groups were stopped before a clean refreeze; their incomplete attempt is not a pass. Root reproduced all14 root-Ruff errors at unchanged bd197 diagnostic scripts; production backend/scripts/sdk lint passes. Formatting-only repairs are verified separately. Final canonical four-group, voice and both strict coverage bases must use the next frozen source/image, with the original failure retained.

The repaired real on-device measurement is bounded evidence at Kernel8993799, not a final-cf826 rerun:50/50 valid surfaces,49 completed turns,32 corpus trials,57 generated adaptive attempts,54 exposed defending responses,zero unauthorized effects and exactly one allowed control. The five measured Swift adapter/host files match the frozen consumer. Cloud measurement remains incomplete at1/2 valid surfaces after nine actual requests; it does not establish adaptive cloud safety. Personal Python composed dispatch also remains partial because its protected background outside-effect path requires an interactive confirmation; no acceptance waiver besides voice is implied.

Local evidence: `evidence-105/backend/final-ea73c7a3/`, `codex-20261114-live/closeout-ea73c7a3/`, `codex-20261114-live/on-device-tools/measured-context-8993799-safe-summary.json`, `codex-20261114-live/nodepack-final-metadata/`, and the cache `AstralNodePack-final-cf826/pack-ea73c7a3/`. These are diagnostic local files and contain no protected-provider run identities. No push, merge, deployment or release is authorized by this continuation.

The corrected three-file backend overlay passes all81 tests with no skips; the removed peer is explicitly denied both chat-request and exact-child reads, cannot falsely answer the parent and dispatches no effect. Root-wide Ruff now passes after formatting-only diagnostics cleanup. Deep41ec2d1e incorporates these fixtures. The first final full Mac unit run at Projection921 executed1,736 tests:1,734 passed,one skipped,one outside-host pointer fixture failed six assertions. The original public SystemAX composer case passed in that full run. Projection4ff6985 makes the outside-host fixture inject pointerOverHost=false; all13 controlled class tests pass, including the separate pointer-over-host suppression test. A separate final Mac rerun is active; the iOS921 snapshot stays unchanged. No production App code changed. Final metadata integration includes this Mac-only correction, and all subsequent canonical backend evidence belongs outside the frozen candidate tree.
