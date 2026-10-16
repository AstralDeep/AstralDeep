# Context adoption decision record

**Updated:** 2026-10-16. **Decision: WITHHELD.** The evidence-preserving context changes (owner/conversation-scoped exact observation recall and failure-preserving compaction) remain a documented experiment: every deterministic paired oracle passes, the required external evidence classes do not exist yet, so adoption is not granted. The verifier now rejects rehashed semantic forgeries; no default, flag, or gate was changed.

This record is the deliverable of tracker child [#273](https://github.com/AstralDeep/AstralDeep/issues/273) ("Produce candidate-bound context adoption evidence") under the best-harness wishlist tracker [#238](https://github.com/AstralDeep/AstralDeep/issues/238), decomposition reference `wishlist-20261005/context_evaluation`. It is limited to producing candidate-bound adoption evidence. It defers none of the implementation children's required security, coverage, deterministic CI, staging, or affected-client checks (see [Nothing deferred](#nothing-deferred)).

## Candidate binding

All evaluation artifacts are bound to one exact candidate and to the accepted feature plan. Every binding below is independently reconstructible (procedure in [Reproduction](#reproduction)).

| Binding | Value | Verification |
| --- | --- | --- |
| Evaluated source anchor | `dc1bdc96bedf53119a9083108dd51a0db886f33d` | Exact Git merge of the original PR head and main `9d297ce79efdcf1366adcf85d6352ab4a573713f`; the producer verifies the executed source bytes against this object |
| Implementation digest | `13a66ac5fb9f4319d31bed556936efd6e3b911820884582b4816c8d2a4a9cc40` | SHA-256 identity over the nine context-implementation files at that commit (list in `backend/context_evaluation/evaluate.py`) |
| Accepted feature plan | `600725e5e3f37c3e41fac9d521c174e84a73c612728f8a55cf1b017069fdf80f` | SHA-256 of `specs/093-evidence-preserving-context/plan.md` at the candidate commit |
| Workflow fixtures | `776b3f069d9a4446…` (full value in the evidence record) | SHA-256 identity of `backend/context_evaluation/workflows.json` |
| Scoring rules | `exact-frozen-task-outcome-and-source-v1`, digest `1198653f860aa2e8…` | SHA-256 identity of the frozen rule set in `backend/context_evaluation/evaluate.py` |
| Declared flag source | `dde6b5bc57e203a93c1b87f3d0bf1380db12bde4e9a16805579439812c86bda2` | SHA-256 of `backend/shared/feature_flags.py` at the source anchor, checked against the executing tree |
| Decision rules | `5472480902d585debc69aaff2240d152fcc4829fd114d38a25785943ebb9c124` | Identity of the verifier-owned rules, checked independently of the record's receipt |
| Evidence record | `specs/093-evidence-preserving-context/adoption-evidence.json` | Version `astral.context-adoption/v2`; file SHA-256 `ee067bf1c59d3713484fd8c000fa5672ba313a98b86dc871bbf23d7e40859079`; internal receipt `f96830492c7bd9271fc312d003d0ad43d93133d8cb2fb18277800170a93e1d80` |

The source anchor includes the feature delivered by merged PR #232 (merge commit `c3e3477e80d12e5a8474614e7d7c0997746e1061`, 2026-10-08). Its nine-file implementation digest is unchanged from the original diagnostic. Git binding covers those files, workflow fixtures, accepted plan, and declared flag source. Generated evidence, this document, and the adoption tooling are outside that source digest, so a subsequent evidence commit does not require its own future hash. This proves source consistency with the named object; it does not independently authenticate an execution, provider, participant, or client observation.

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

The comparison ran under the frozen acceptance rule set (`SCORING_RULES` plus verifier-owned decision rules): paired-oracle passing, owner-isolation/revocation/privacy/confirmation/integrity/failure-preservation/retention/concurrency/accounting checks, a 10% minimum complete-cost saving per accepted task on the held-out split including all auxiliary and charged-failed work, a five-participant formative usability floor of 4/5 unaided successes, and product/all-client qualification. The record retains bounded raw `provider_route`, `promoted_control`, `provider_measurements`, `formative`, and `qualification` inputs, and verification recomputes the canonical `promotion()` result from them and the complete frozen run inventory. Verdict:

- Deterministic capability, boundary, and failure-preservation criteria: **satisfied**.
- Real-provider complete-cost measurements (`complete_cost_target`): **missing** — no dated provider prices or usage exist, and missing usage cannot count as zero cost or as a saving.
- Five-user formative evidence: **missing**.
- Product and six-client live qualification evidence: **missing**.
- Overall: `eligible = false`, `failed = []`, `missing = ["real_provider_measurements", "five_user_formative_evidence", "product_and_client_qualification"]`.

## Decision and rationale

**WITHHELD.** The deterministic results are necessary but not sufficient: feature-093's own success criteria require measured complete-cost superiority (SC-007) and formative usability (SC-008) that only real, governed measurements can supply, and FR-021 requires passing promotion checks followed by the product's ordinary qualification and affected-client verification. None of those inputs exists today; recording them as missing — not as passing, zero, or deferred — is the honest state. Per SC-007, the candidate remains a documented experiment.

Adoption would be reconsidered only when the three missing evidence classes are produced through their own governed tasks with receipts bound to a new exact candidate, and a fresh record supersedes this one. The defaults stay off until then.

The supplied-receipt schema cannot prove external origin. Even if all structural comparisons pass, this local tool records `external_evidence_origin = "unverified"` and withholds adoption with `external_evidence_origin_unverified`; independent provenance and live qualification are still required. Rehashing a dictionary never provides that authority.

## Defaults and gates unchanged

- Verified at the candidate: `FF_OBSERVATION_PACKING`, `FF_SAFE_COMPACTION`, and `FF_MESSAGE_COMPACTION` all default to disabled in `backend/shared/feature_flags.py`; the record's `controls_default_off` field re-verifies the declared defaults through the flag registry with the environment scrubbed.
- `defaults_changed = false` and `waived_gates = []` are structural fields of the evidence record; the verifier rejects any record that changes them.
- No threshold, workflow, or gate was modified or waived. The frozen diagnostic (`backend/context_evaluation/evaluate.py`) is untouched; its suite passes unchanged (219 tests). Verifier tests now include rehashed rule, run, promotion, source, input, and authenticity forgeries.

## Nothing deferred

This evidence task does not defer any implementation child's required checks. Still required before any default promotion, each owned by its own task or ordinary qualification path: changed-line coverage and the complete backend/module CI groups; production image build and fail-closed boot smoke; secret scan; real-authentication staging with representative migrated data; live evidence-modal acceptance on web, macOS, iOS, and watchOS (Windows and Android live verification remain explicitly untested per recorded owner direction in [verification.md](verification.md)); and the review gates above. The current local owning check runs the adoption and evaluation suites together on Python 3.11; full current-head CI and the external evidence holds remain separate requirements.

## Reproduction

From the repository root at the evaluated candidate (or any tree whose bound digests match):

```text
cd backend
python3 -m context_evaluation.adoption \
  --candidate dc1bdc96bedf53119a9083108dd51a0db886f33d \
  --plan ../specs/093-evidence-preserving-context/plan.md \
  --output ../specs/093-evidence-preserving-context/adoption-evidence.json
python3 -m pytest tests/test_context_adoption.py tests/test_context_evaluation.py -q
```

The record is deterministic: rerunning with the same inputs reproduces byte-identical JSON, so the committed artifact can be regenerated and compared. `context_evaluation.adoption.verify(record, plan_path=..., expected_candidate="dc1bdc96bedf53119a9083108dd51a0db886f33d")` checks the caller's candidate expectation, actual Git/source bindings, fixed rule identity, complete run inventory/schema, retained inputs, recomputed promotion, declared defaults, decision, and receipt. It also replays all 96 deterministic scenarios and compares their exact outputs, then rechecks source and plan bindings. Async callers use `await verify_async(...)`; synchronous `verify(...)` owns its event loop and refuses an existing loop. Version-1 records are rejected and must be regenerated. All local receipts are diagnostic, not protected release authorization or external-origin attestations.

## Tooling identities

- `backend/context_evaluation/adoption.py` is the record producer/verifier; it uses the standard library and local Git, with no new Python dependency. Its owning tests are in `backend/tests/test_context_adoption.py`; coverage and exact test results belong to the current PR qualification.
- Unchanged: `backend/context_evaluation/evaluate.py`, `backend/context_evaluation/workflows.json`, and all nine bound implementation files.
