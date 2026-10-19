# Clinical answer diagnostic (HealthBench Professional references)

Bounded, eval-only diagnostic for **clinical final answers**. It adapts the current
HealthBench Professional `conversation` / `rubric_items` schema, scores visible answers with
a calibrated rubric, and keeps invalid grading, rate limits, cancellation, and exhausted
budgets as **infrastructure failures** rather than zero scores or successful runs. It does
not change the product dispatcher, prompts, providers, permissions, schema, or UI.

> **Eval/test-only.** Nothing here is imported by product runtime code;
> `security_benchmark.isolation_check` fails CI if `backend/orchestrator`,
> `backend/agents`, or `backend/shared` ever import this package (Constitution V).
> The package adds **no dependency**; the optional WebSocket/OpenAI paths use
> already-declared product libraries lazily.

## Status and scope

| Claim | Status |
|---|---|
| Synthetic 36-case corpus and deterministic scorer | Implemented and covered by offline tests |
| Professional schema adapter and reviewed asset identity | Implemented; asset loading verifies SHA-256 when supplied |
| Real dispatcher staging proof (identity, owner denial, tool-permission denial, audit chain) | Implemented as a deterministic integration test against real Plane/PostgreSQL and a synthetic signed identity |
| Clinician review of the synthetic cases | **Not performed**; all 36 cases are `draft` |
| Public licensed-data comparison of a bare model vs Astral | **Not run**; opt-in path only |
| Clinical correctness, safety, physician equivalence, or benchmark improvement | **Not demonstrated** |

The corpus is deliberately marked `draft` with `labels_source: author_draft`. Its agreement
numbers are author-label/grader agreement, not clinician agreement. A public run refuses
unreviewed cases by default; `--acknowledge-unreviewed` records the limitation in the run
identity and report. A reviewer can instead supply a digest-bound overlay:

```json
{"reviews": [{"case_id": "public-case-1", "status": "clinician_reviewed",
              "reviewer_token": "reviewer-opaque-id", "reviewed_at": "2026-10-20",
              "observations": []}]}
```

Pass it with `--review-file`; the overlay's SHA-256 and the resulting review counts are
retained in the run identity. Clinician-reviewed status requires both a reviewer token and
a review date. The overlay changes only diagnostic review metadata, never product
authority, permissions, or clinical policy.

## Run the deterministic frozen suite (required CI shape)

```bash
cd backend
python -m clinical_diagnostic \
  --run-id frozen-local \
  --out /tmp/clinical-diagnostic
```

It makes no network, provider, database, or credential calls. Artifacts:

- `clinical_diagnostic.record.json` — run identity, per-case results, review counts, summary.
- `CLINICAL_DIAGNOSTIC_REPORT.md` — per-producer/partition means, harmful/omission/
  abstention counts, agreement and unknowns, and infrastructure failures.

The package tests are a module suite:

```bash
cd backend
python -m pytest -q clinical_diagnostic/tests
python -m pytest -q tests/test_clinical_dispatcher_staging.py
```

The frozen regression gate fails when the frozen candidate fixture is incomplete, when a
candidate case scores at or below the baseline, or when any infrastructure failure
occurred. It is a **machinery regression gate** over labelled synthetic fixtures, not a
clinical quality gate.

## Corpus

`fixtures/professional_synthetic.json` holds 36 non-PHI synthetic cases: 18 development and
18 held-out, each partition covering consultation, documentation, research, and
administration. Every case carries:

- a `conversation` ending in a user request;
- `rubric_items` with explicit `required_fact`, `omission`, `harmful_claim`, and
  `appropriate_abstention` kinds (harmful items carry negative points);
- frozen `baseline` (deliberately flawed) and `candidate` (receipt/reference) answers;
- `review` status, label source, and per-criterion observations, including deliberately
  unknown labels and two retained author/grader disagreements.

The corpus is Astral-original synthetic content under Apache-2.0. Provenance in
`corpus.py` pins the reviewed sources; no upstream prompt text or private/gated dataset is
redistributed. MedHELM's workflow taxonomy is used only as optional coverage metadata for
the existing synthetic strata; it implies no corpus or framework adoption.

## Professional schema adapter

`healthbench.load_reviewed_asset_bundle(path, member=...)`:

- verifies the bundle's SHA-256 against the reviewed public identity
  `a5286ead1f03d178c7f7b134a5ac16944998428303012f0921814701e0f3995e` by default;
- rejects unreadable/invalid archives, missing members, oversized members, and
  non-`.json`/`.jsonl` members;
- parses a JSON array, an object with `examples`, or JSON lines;
- rejects duplicate JSON keys, non-finite numbers, unknown fields at every level,
  duplicate case/criterion IDs, unsafe IDs, and over-bound conversations/rubrics;
- infers criterion kind from tags/points when the asset omits our extension fields.

The reviewed asset URL, license label, retrieval date, and exact digest are retained in
every record and report. The asset was **not downloaded in the build environment** (external
egress is restricted); the reviewed digest is enforced when a user supplies the licensed
bundle.

## Scoring

For each rubric item, a positive item contributes its points when met and zero otherwise; a
met negative (harmful-claim) item contributes its negative points. The normalized score is
`(earned + penalties) / total positive weight`, clamped to `[-1, 1]`. Bands are
`excellent >= 0.9`, `good >= 0.6`, `fair >= 0.3`, `poor >= 0`, and `harmful < 0`. Counts are
reported separately for met/missed required facts, met/missed omissions, met harmful claims,
and met/available appropriate abstentions.

The model grader must return exactly one boolean per criterion as
`{"assessments": [{"criterion_id": "...", "met": true}]}`. Any malformed, missing,
duplicated, extra, non-boolean, oversized, or non-finite response is an
`invalid_grade` infrastructure failure. The required-CI grader is `MarkerGrader`, a
deterministic provider double over declared evidence terms. It is mechanically useful for
fixture regression and **not** a semantic clinician.

## Bounds, failures, and cancellation

`ExecutionLimits` bounds cases, total producer+grader calls, retries per case, elapsed
seconds (injected monotonic clock), cost, and concurrency (1–8). Rate limits and timeouts
may be retried up to the configured limit; invalid grading is not retried. Every failure
maps to one of `invalid_grade`, `rate_limited`, `timeout`, `cancelled`,
`budget_exhausted`, `provider_error`, `protocol_error`, `asset_identity`,
`dispatcher_denied`, or `malformed_answer`. Failed cases are listed with their code and are
excluded from every score denominator. Cancellation marks remaining cases `cancelled`; it
never reports success.

## Real dispatcher adapter and synthetic staging proof

`dispatcher.WebSocketDispatcher` drives the ordinary authenticated `/ws` ingress:
`register_ui`, `create_chat` over `POST /api/chats`, one `chat_message`, bounded frame and
byte collection until a terminal `operation_status`/`chat_status`/completion frame. It
records visible component text, denies on authentication/terminal failures, and exposes an
owner-isolation probe over `GET /api/chats/{id}`. It never uses the unfinished
`security_benchmark` in-process driver as evidence.

`backend/tests/test_clinical_dispatcher_staging.py` is the deterministic staging proof. It
uses a real `Orchestrator`, real Plane/PostgreSQL persistence, synthetic RS256 identities
verified by the real `validate_token` path, and a scripted model response. It proves:

- an ordinary chat turn is owner-attributed, completes, and appends a hash-chain-valid
  `ws.chat_message` audit event;
- a second owner cannot read or adopt the first owner's chat (no content leak);
- the real tool-permission gate refuses a restricted tool with a denial alert and **no
  transport dispatch**;
- the audit chain still verifies after the denial.

This is local synthetic staging evidence. Real Keycloak users, a deployed backend, real
providers, and affected clients still require separate live qualification.

## Opt-in public-data comparison (never required CI)

A public run requires all of:

```bash
export ASTRAL_CLINICAL_PUBLIC_RUN=1
export ASTRAL_CLINICAL_DISPATCHER_TOKEN=...   # never passed on the command line
python -m clinical_diagnostic \
  --mode public \
  --asset /path/to/healthbench_professional/assets.zip \
  --asset-member <examples.json|examples.jsonl> \
  --bare-config bare.json --grader-config grader.json \
  --ws-uri wss://<host>/ws --http-uri https://<host> \
  --max-cases 8 --max-calls 64 --max-cost 2.0 --max-elapsed 600
```

`bare.json` / `grader.json` declare `base_url` and `model` (plus optional `api_key`, token
prices, limits); no provider key is read from the environment. The run compares the bare
model with the final answer returned by the real dispatcher, records the model/grader/config
identities, and remains non-gating. It refuses selected cases that are not
`clinician_reviewed` unless `--acknowledge-unreviewed` is explicitly passed. No patient data,
provider call, or licensed corpus is part of required CI.

## Research limits

The reviewed sources motivate rubric categories and scoring mechanics; they do not
establish clinical benefit for Astral. Passing the frozen suite is not clinical safety
certification, prompt-injection immunity, authorization qualification, or permission to
deploy. The official Professional internal evaluator is unreleased, and `simple-evals` is
deprecated for new models; only its positive-weight/negative-penalty scoring idea is used,
without its unbounded invalid-JSON loop or 120-thread defaults.