# Windows UI v2 draft handoff

Windows work is on `codex/091-windows-ui-v2` in Deep and Projection. The owner requested immediate branch publication and PRs. **Visual fidelity to the kos-wiki screenshots is not accepted, and the client is not complete or merge-ready.** Functional test results do not establish screenshot parity.

## Current source

Projection `e0419ddaad927a64c575eac21bfcc26f54378a3a` integrates published shared Projection `162486570a101cbc00f2ae48dbc33ce73c5d3308`. Deep integrates shared `711d428210e20150e665b81931feb0512156b89e` and pins the combined component. The canonical UI-protocol digest is `25b964ce64553db80cf2789991eb3565bb86c66cc9792849aa496101ef35585d`. Four-source composition and immutable Projection ledger/export checks pass. LETS, Plane and Primitives remain at their existing pins. Windows-authored changes do not modify 090 artifacts or Apple/Android sources.

The Windows scoped viewport hydration consumer is **not implemented**. Its incomplete, untested prototype was removed from active source and preserved only in ignored `build/windows-091/viewport-consumer-recovery/`. The shared contract is available; no competing protocol or post-snapshot guard relaxation was introduced.

## Evidence and gaps

See [verification](verification.md) for exact source-bound results and failures. Prior complete Windows source `3a76954` passed 1,586 tests, 10 skips and 98.39% changed coverage, including all 700 supervision trials. The later lifecycle cohort passed 470 tests; corrected socket/generation fixtures passed 157. The later broad run recorded 1,699 passes and two fixture failures; the superseding rerun was stopped incomplete. These are not a final combined-source suite pass.

Prior Deep d105 / Projection 8a Linux qualification passed 152 affected tests, boot/dependency/composition checks and changed coverage. The 8a executable builds and passes six read-only package checks; its GUI/connected acceptance remains open. Final combined-source CI and screenshot comparison are pending. Draft PRs target 090; Projection core CI filters to main and will not run automatically for that base.

Remaining blockers: faithful screenshot-based geometry/typography/control layout, the Windows viewport consumer, current-source full CI/coverage, authenticated attachments and Advanced selection, connected frozen acceptance, real microphone/worker playback, physical DPI and the complete logical-size matrix. Desktop input recovery failed; current captures are read-only evidence. No credential or authentication bypass was used.

## Local runtime and cleanup

The local development backend remains on image `sha256:906ba6a782f69aeb6993490c01599234e43d8a1cc828de737f653a21ce53be14` (d105), with unchanged configuration/data mounts. It does not run the new shared resize source. Worker, LiveKit and application PostgreSQL remain available. The unsigned 8a executable SHA256 is `32302ec1b8058d8a4cad85805dbbc7676b7656b7bf57b338b43700d8e2020a9d`.

Temporary test Docker resources are being removed under the owner's cleanup request. Historical logs and qualification artifacts remain under ignored `build/windows-091/`; incomplete driver drafts are not ready to execute. No merge, release or production deployment is authorized.
