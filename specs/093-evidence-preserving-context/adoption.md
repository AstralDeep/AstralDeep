# Context adoption decision record

**Date:** 2026-10-15. **Decision: WITHHELD.** The evidence-preserving context changes (owner/conversation-scoped exact observation recall and failure-preserving compaction) remain a documented experiment: every deterministic paired oracle passes, the required external evidence classes do not exist yet, so adoption is not granted. No default, flag, test, or gate was changed by this task.

This record is the deliverable of tracker child [#273](https://github.com/AstralDeep/AstralDeep/issues/273) ("Produce candidate-bound context adoption evidence") under the best-harness wishlist tracker [#238](https://github.com/AstralDeep/AstralDeep/issues/238), decomposition reference `wishlist-20261005/context_evaluation`. It is limited to producing candidate-bound adoption evidence. It defers none of the implementation children's required security, coverage, deterministic CI, staging, or affected-client checks (see [Nothing deferred](#nothing-deferred)).

## Candidate binding

All evaluation artifacts are bound to one exact candidate and to the accepted feature plan. Every binding below is independently reconstructible (procedure in [Reproduction](#reproduction)).

| Binding | Value | Verification |
| --- | --- | --- |
| Evaluated candidate | `7e0db649247fae2849f201e79e72d54b88d8039b` | `git rev-parse` of the tree the harness executed |
| Implementation digest | `13a66ac5fb9f4319d31bed556936efd6e3b911820884582b4816c8d2a4a9cc40` | SHA-256 identity over the nine context-implementation files at that commit (list in `backend/context_evaluation/evaluate.py`) |
| Accepted feature plan | `600725e5e3f37c3e41fac9d521c174e84a73c612728f8a55cf1b017069fdf80f` | SHA-256 of `specs/093-evidence-preserving-context/plan.md` at the candidate commit |
| Workflow fixtures | `776b3f069d9a4446…` (full value in the evidence record) | SHA-256 identity of `backend/context_evaluation/workflows.json` |
| Scoring rules | `exact-frozen-task-outcome-and-source-v1`, digest `1198653f860aa2e8…` | SHA-256 identity of the frozen rule set in `backend/context_evaluation/evaluate.py` |
| Evidence record | `specs/093-evidence-preserving-context/adoption-evidence.json` | File SHA-256 `b183f667cb2256b005ed8fb589e815134f7d80a19d83fb59de9a3615f40d63b5`; internal receipt `24d3d50bee4130277e19a40f458995424b5841dd1ae57bea3e8dbc7221596e37` |

The evaluated candidate is the current qualified main state that contains the feature delivered by merged PR #232 (merge commit `c3e3477e80d12e5a8474614e7d7c0997746e1061`, 2026-10-08) on top of the feature's declared baseline `e9a47d8b826dc506dd509b6110a5199ad1cddc42`. Between that merge and the candidate, the archive, compaction, accounting, presentation, authority, and budget modules are byte-identical; `orchestrator.py` carries later unrelated changes, which is exactly why the record binds the evaluated tree by digest rather than by the merge commit alone. The evidence PR adds only the adoption module, its tests, and this record, so the bound implementation digest remains verifiable at the PR head.

Mechanism provenance: the evaluated mechanisms are the AstralDeep engineering inference from SoL-Pi (Haozhe Liu et al., arXiv:2609.20519v1, 2026 preprint, retrieved 2026-10-04) recorded in [`specs/research/2026-10-04-sol-pi-review.md`](../research/2026-10-04-sol-pi-review.md); the paper's reported scores are not reproduced or claimed here.

## Fixtures

24 frozen synthetic non-clinical workflows (`backend/context_evaluation/workflows.json`), partitioned in advance into 12 development and 12 held-out tasks with distinct documents, tasks, and operations, a near-duplicate partition guard (below 5% 4-gram overlap), and frozen per-task oracles covering success, error, denial, incomplete, empty, oversized, revoked, expired, corrupt, and cancelled source states. The split and oracles are enforced by `backend/tests/test_context_evaluation.py`; mutation of any fixture is rejected at run time.

## Frozen results

The candidate-bound diagnostic ran all 96 paired workflow/control combinations (4 controls × 24 workflows). Every run reports source identity, task identity, boundary checks, exact page reconstruction, failure preservation, and attempt accounting; unknown costs stay unknown.

| Control | Passed | Total |
| --- | ---: | ---: |
| baseline | 24 | 24 |
| packing | 24 | 24 |
| compaction | 24 | 24 |
| combined | 24 | 24 |
| **All** | **96** | **96** |

- Development split 48/48; held-out split 48/48. No failed workflows, no boundary bypasses, no exact-reconstruction failures, no failure-preservation regressions.
- Foreign-owner reads, revoked, expired, integrity-failed, and oversized sources were each refused with the honest unavailable reason; cancelled auxiliary attempts preserve history and are recorded as attempts, not successes.
- Accounting honesty: all 96 runs carry `complete_cost_known = false` and `reported_provider_cost = null`. The diagnostic's provider route is the declared deterministic synthetic route (`deterministic-synthetic-provider-v1`); no real price or usage is claimed anywhere.

## Comparison under explicit acceptance rules

The comparison ran under the frozen acceptance rule set (`SCORING_RULES` plus the decision rules embedded in the evidence record): paired-oracle passing, owner-isolation/revocation/privacy/confirmation/integrity/failure-preservation/retention/concurrency/accounting checks, a 10% minimum complete-cost saving per accepted task on the held-out split including all auxiliary and charged-failed work, a five-participant formative usability floor of 4/5 unaided successes, and product/all-client qualification. Verdict of the frozen `promotion()` evaluator:

- Deterministic capability, boundary, and failure-preservation criteria: **satisfied**.
- Real-provider complete-cost measurements (`complete_cost_target`): **missing** — no dated provider prices or usage exist, and missing usage cannot count as zero cost or as a saving.
- Five-user formative evidence: **missing**.
- Product and six-client live qualification evidence: **missing**.
- Overall: `eligible = false`, `failed = []`, `missing = ["real_provider_measurements", "five_user_formative_evidence", "product_and_client_qualification"]`.

## Decision and rationale

**WITHHELD.** The deterministic results are necessary but not sufficient: feature-093's own success criteria require measured complete-cost superiority (SC-007) and formative usability (SC-008) that only real, governed measurements can supply, and FR-021 requires passing promotion checks followed by the product's ordinary qualification and affected-client verification. None of those inputs exists today; recording them as missing — not as passing, zero, or deferred — is the honest state. Per SC-007, the candidate remains a documented experiment.

Adoption would be reconsidered only when the three missing evidence classes are produced through their own governed tasks with receipts bound to a new exact candidate, and a fresh record supersedes this one. The defaults stay off until then.

## Defaults and gates unchanged

- Verified at the candidate: `FF_OBSERVATION_PACKING`, `FF_SAFE_COMPACTION`, and `FF_MESSAGE_COMPACTION` all default to disabled in `backend/shared/feature_flags.py`; the record's `controls_default_off` field re-verifies the declared defaults through the flag registry with the environment scrubbed.
- `defaults_changed = false` and `waived_gates = []` are structural fields of the evidence record; the verifier rejects any record that changes them.
- No test, threshold, workflow, or gate was modified or waived by this task. The frozen diagnostic (`backend/context_evaluation/evaluate.py`) is untouched; its suite passes unchanged (219 tests).

## Nothing deferred

This evidence task does not defer any implementation child's required checks. Still required before any default promotion, each owned by its own task or ordinary qualification path: changed-line coverage and the complete backend/module CI groups; production image build and fail-closed boot smoke; secret scan; real-authentication staging with representative migrated data; live evidence-modal acceptance on web, macOS, iOS, and watchOS (Windows and Android live verification remain explicitly untested per recorded owner direction in [verification.md](verification.md)); and the review gates above. The `ui-v2-contracts` CI profile was reproduced locally on this snapshot (339 passed, 1 skipped, fresh hash-locked Python 3.11 environment) and required no repair.

## Reproduction

From the repository root at the evaluated candidate (or any tree whose bound digests match):

```text
cd backend
python3 -m context_evaluation.adoption \
  --candidate 7e0db649247fae2849f201e79e72d54b88d8039b \
  --plan ../specs/093-evidence-preserving-context/plan.md \
  --output ../specs/093-evidence-preserving-context/adoption-evidence.json
python3 -m pytest tests/test_context_adoption.py tests/test_context_evaluation.py -q
```

The record is deterministic: rerunning with the same inputs reproduces byte-identical JSON, so the committed artifact can be regenerated and compared. `context_evaluation.adoption.verify(record, plan_path=...)` re-checks the receipt, all scope digests against the live tree, the plan digest, the embedded run summary, the decision consistency, and the no-waiver posture.

## Tooling identities

- New: `backend/context_evaluation/adoption.py` (record producer/verifier; stdlib-only, no new dependency) — 16 focused tests in `backend/tests/test_context_adoption.py`, 100% statement coverage of the new module.
- Unchanged: `backend/context_evaluation/evaluate.py`, `backend/context_evaluation/workflows.json`, and all nine bound implementation files.
