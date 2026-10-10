# Core repository PR rereview and posted feedback

**Date:** 2026-10-05 (UTC). **Scope:** all five core repositories, with exact-head source review and existing-discussion checks. The initial inventory contained 17 open contributor PRs: Deep 0, Plane 1, Primitives 8, Projection 1, LETS 7. Every head matched the previous report; several hosted qualification results had completed since that snapshot.

Posted 11 current-head reviews: nine requested changes and two approvals. Six unchanged PRs already had substantive exact-head reviews and received no duplicate comment. Under the owner's standing merge authorization, the two approved PRs were merged normally with their reviewed heads bound to the merge request. The final contributor inventory is 15: Deep 0, Plane 1, Primitives 7, Projection 1, LETS 6. All 15 have requested changes; mergeability recalculation after the two base advances is not a substitute for that source assessment.

## Merged candidates

| PR | Reviewed head | Merge receipt | Evidence |
|---|---|---|---|
| [Primitives #27](https://github.com/AstralDeep/AstralPrimitives/pull/27) | `8b2ac5d0a0f390d9f45244e96981882cb3cb20b0` | `b2fdebe1619a3e3f0b5e783d436df54ff37cac42`, 2026-10-05T04:24:08Z | Five hosted checks passed; five new discriminator regressions passed independently and again under root verification. Manifest/runtime/lock agree at 0.6.0; compatibility notes exist; retained exact-head changed coverage is 2/2 executable lines. Three whitespace additions are non-blocking nits. |
| [LETS #90](https://github.com/AstralDeep/LETS/pull/90) | `526053e471122310ad30f273b785e0aade0ece94` | `6ecf3b0e66cf293dc2017c5ead0c7b77be5c76d1`, 2026-10-05T04:24:14Z | Eleven hosted checks passed; fresh client/API/mTLS suite 174 passed. Root verified generation/enforcement of legacy schema exclusion, valid and malformed metrics regressions, and source-policy corrections; generator check and three metrics tests passed. New approval replaces the old requested changes on 5b14a8d. |

These are source merges. No manual package publication, release verification, component adoption, AstralDeep pin movement, server deployment, or live client acceptance was performed in this rereview. Repository publication workflows may run normally; their outcome was not asserted here.

## Posted reviews

| PR / review | State | Current finding |
|---|---|---|
| [Primitives #25](https://github.com/AstralDeep/AstralPrimitives/pull/25#pullrequestreview-5409998257) | Requested changes | All five checks and 173 locked primitive/README tests pass; the complete matrix is sound. Remove eleven newly added narrating comments under Constitution VI. No runtime regression or version bump requirement. Formatter warnings reproduce on main and are not a new CI blocker. |
| [Primitives #26](https://github.com/AstralDeep/AstralPrimitives/pull/26#pullrequestreview-5409998344) | Requested changes | Removed serializer breaks flattened attributes, class alias, empty-CSS/null omission; normalized mapping keys silently lose data; duplicate registration overwrites Text. New tests fail after 26 passes. Rebase while preserving current main's serializer and registry, choose the behavior version against the newly advanced main, and add denial/compatibility tests. |
| [Primitives #27](https://github.com/AstralDeep/AstralPrimitives/pull/27#pullrequestreview-5409998467) | Approved, merged | Concrete-class validation now receives the supplied discriminator; matching and nested wire output remain intact. |
| [Primitives #28](https://github.com/AstralDeep/AstralPrimitives/pull/28#pullrequestreview-5409998616) | Requested changes | `setdefault` lets explicit `allow_nan=True` emit NaN/Infinity; `to_dict()` still returns non-finite values. Enforce and test a shared recursive finite-number policy. |
| [Primitives #29](https://github.com/AstralDeep/AstralPrimitives/pull/29#pullrequestreview-5409998753) | Requested changes | Missing base64 import, mixed-set sorting errors, silent normalized-key collisions, and unnormalized models/attributes. Existing passing goldens do not cover these failures. |
| [Primitives #30](https://github.com/AstralDeep/AstralPrimitives/pull/30#pullrequestreview-5409998883) | Requested changes | Registered option is never consumed; garbage collection and relabeling the existing suite add none of the requested registry-derived matrix assertions. This is an incomplete tooling deliverable, not a demonstrated production round-trip failure. |
| [Plane #29](https://github.com/AstralDeep/AstralPlane/pull/29#pullrequestreview-5409999048) | Requested changes | Production key rejection works, but the new test imports a nonexistent export, shadows its helper, expects a valid string key to fail, and removes unrelated compatibility verification. Fix the real regression tests and PR description. |
| [Projection #44](https://github.com/AstralDeep/AstralProjection/pull/44#pullrequestreview-5409999183) | Requested changes | New Markdown token selection repeatedly scans the remaining suffix. 128/256/512 ordinary code spans produce 57,920/230,528/919,808 searched characters. Link destination bounds do not bound this quadratic public-sanitizer work. |
| [LETS #90](https://github.com/AstralDeep/LETS/pull/90#pullrequestreview-5409999315) | Approved, merged | Earlier legacy metrics exclusion and source-policy findings are fixed; renewed review answers the contributor's correction reply. |
| [LETS #92](https://github.com/AstralDeep/LETS/pull/92#pullrequestreview-5409999450) | Requested changes | Adapter fabricates authority from an empty authorizer response, hashes local text instead of verifying a signed receipt, and permits replay across new adapter instances. Use real authorization and durable executor claim boundaries. |
| [LETS #96](https://github.com/AstralDeep/LETS/pull/96#pullrequestreview-5409999590) | Requested changes | Invented InfoDocument fields reject a valid committed-contract response; `getattr` on mapping replies denies valid integration operations. Fresh narrow integration reproduction: one failed/two passed; completed CI has the same failure across six Python lanes. |

All nine new requested-changes reviews and both approvals bind the full exact reviewed head. The public review bodies contain source locations and actionable fixes; none treats merely waiting for CI as a source defect.

## Existing discussions retained

Primitives #15/#21 and LETS #75/#80/#87/#88 have unchanged heads and existing substantive requested changes. Fresh inspection/reproductions confirm the nested finite-number bypasses, removed CSS import, incomplete response validation, fabricated/unsigned authority and replay, deadlocking deadline fixtures, and uninterruptible borrowed-client header work. No contributor correction or new head required another duplicate review.

## Narrow verification

- Primitives #25: locked Python 3.11.15/Pydantic 2.13.4, `python -m pytest tests/test_primitives.py tests/test_readme_reference.py -q -o addopts='' -p no:cacheprovider`: **173 passed**. Changed-file Ruff lint and whitespace checks pass. Formatter failure also exists on the same main file.
- Primitives #27 root verification: Python 3.11.15, `python -m pytest tests/test_primitives.py -q -o addopts='' -k scarlet_regression`: **5 passed, 75 deselected**. Root inspected the discriminator change, version/lock, compatibility note and retained exact-head coverage record. An additional initial root selection passed two tests; it is not counted as five.
- LETS #90 independent exact-source verification: Python 3.14.0, `python -m pytest tests/unit/test_client.py tests/integration/test_api.py tests/integration/test_client_mtls.py -q -o addopts=''`: **174 passed**. Generator and targeted Ruff lint/format pass. Root reran `scripts/generate_response_contract.py --check` (**up to date**) and the metrics selection (**3 passed, 136 deselected**) and inspected protected variant exclusion handling directly.
- Root independently executed #26's public wire/key/registry reproduction against its bound source and #28's non-finite public APIs; root inspected #29's normalization, #30's unused option/assertion absence, #44's sanitizer loop, Plane's real helper/new test, and LETS #92/#96's authority/contract seams. Agent reports supplemented this direct verification.
- Negative checks intentionally fail for blocked candidates. No weakened assertion, CI waiver represented as a pass, full combined-main qualification, or fresh coverage regeneration is claimed.
- User working trees remained untouched. Temporary detached source trees and raw receipts stay outside product and vault commits.

## Specification created in the same follow-up

[Feature 093](../093-evidence-preserving-context/spec.md) defines owner-scoped exact observation recall and failure-preserving compaction with complete cost accounting, absolute retention, protected-state preservation, independent default-off controls and held-out evaluation. Its [requirements checklist](../093-evidence-preserving-context/checklists/requirements.md) passes 16 requirements-quality criteria after independent review. This is a specification, not implemented or measured context adoption.
