# Apple native coverage domain checkpoint — 2026-09-12

This local checkpoint follows `native-checkpoint-20260912.md`. Projection is
pinned to `6eab76d706aeea5e64577d392a4622c6229605ad`; its Apple and Android
shipping sources are unchanged from qualified `5074bdd`. This is diagnostic
evidence tooling, not a release approval or completion of T060–T062.

The iOS collector now independently reconstructs compiler source geometry from
the exact prepared arm64 binaries using the pinned LLVM toolchain. It discards
all compiler-tool execution counts. Original xccov arrays remain the only
execution observations; no missing rows or counters are invented. Nine absent
final physical Swift braces are beyond independently reconstructed compiler
domains. Missing executable prefixes and conflicting source masks still fail.

The protected normalizer reconstructs each Core, app-unit, UI and mandatory
authenticated-staging domain from retained source, complete Products,
generated xctestrun and raw result bundles. Source bytes are checked against the
candidate's immutable Projection gitlink. Incorrect platform, architecture,
binary member, source digest, copied lane identity or reduced domain is refused.
Mac and Watch retain their strict existing contracts. A local iOS report cannot
fill either platform's evidence slot.

## Observed checks

| Frozen check | Result |
| --- | --- |
| Projection full Python cohort | 1,740 passed, no skips. |
| Projection changed Python | Increment 587/589 (99.66%); whole branch 1,436/1,475 (97.36%). |
| Shared compiler-domain module | 124 passed; 302/302 executable lines covered. |
| Collector | 53 passed; 222/223 executable lines covered. |
| Deep native tooling increment | 514/521 changed executable lines covered (98.66%). |
| Fresh iOS Core / app unit / UI | 213 / 198 / 22 passed, no failures or skips. |
| iOS changed Swift, canonical Projection main comparison | Combined 2,440/2,692 (90.64%); app 1,967/2,212 (88.92%); Core 473/480 (98.54%). |

Deep's first integration run retained one expected exact-byte parity failure
against the old Projection pin; the component update closes that mismatch.
The first exact combined-candidate cohort passed 1,330 tests with three existing
skips and four standard exclusions; one stale expected component-pin assertion
failed. Both expected pins were then updated to the qualified revisions. The
final integrated Deep candidate and checks are recorded outside the tree.
All 117 Swift source files and the complete prepared Products stayed unchanged
through the fresh native run. Core observes 25 sources; app unit and UI each
observe 43 sources. All three collectors, exporters, strict lane merge and
source/domain completeness checks passed. The task-owned simulator is shut
down and its fixture preferences were removed; user devices were untouched.

Ruff, workflow shell parsing, staged secret checks and full provenance replay
passed. Projection retains 183 transformations, including 16 removals, and 336
unchanged original imports. An initial fixed-count provenance assertion and
unused test import failure are retained with their corrections.

## Limits and retained artifacts

Darwin's observed LLVM runtime emits no usable profile binary IDs. Native source
geometry does not prove raw-profile-to-binary UUID association; a successful
`--check-binary-ids` invocation cannot establish that missing relationship.
Mandatory authenticated staging and final Mac/Watch qualification remain open.
The app-only percentage is explicitly distinct from the combined iOS percentage;
no threshold or coverage scope was changed to produce the result.

Fresh result/Products archives, source closures, exact binary identities and
raw observations are retained under Projection's
`build/088/native-domain-ios-5074bdd/`. Prior reports and failures remain intact.
Existing Android and unsigned Apple shipping artifacts remain source-identical;
they have not become signed or store-ready. The AAB remains
`build/088/handoff/astraldeep-088-controller-UNSIGNED.aab` in the main Deep
checkout, SHA-256
`9c80f8ca52b96b2a10e1fa964aff8513c3813717a2076498a504754bf2f5c2dd`.

No product push, PR promotion, signing, store submission, live runtime update,
schema change, new runtime dependency or Windows redesign occurred. Full 088
execution, guidance, monitoring and framework tasks remain incomplete.
