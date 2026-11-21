# Scripted prompt-optimization pilot

The offline workbench measures the archived planning prompt in
`synthetic_planning_prompt.txt` against synthetic train, selection and final
partitions. Use the explicit `synthetic_coordinator_planning` task to run this
fixture. The former `coordinator_planning` product task returns a retirement
error before opening the database or exporting a report. Existing reports keep
their original task and source identities.

The deterministic model double qualifies the evaluation machinery. Reports
record `actual_model_qualified: false` and export inert JSON for review.

The former raw live-config adapter has been removed. There is no provider URL,
credential, or paid-model mode. A future optional actual-model runner must first
integrate current owner, provider, PHI, egress, audit, per-attempt output and
cost-admission controls; a low-level SDK client is insufficient.

## Run

Use Python 3.11 with the repository's installed dependencies. Configure a
**dedicated synthetic qualification database** with
`ASTRAL_PROMPT_OPTIMIZATION_DATABASE_URL`. The application `DATABASE_URL` is not
used as a fallback. Plane's existing composition binding verifies the pinned
contract and initializes its normal guarded migrations. No Deep SQL or new
schema, component pin, production setting, or credential store is introduced.
Optionally set `ASTRAL_PROMPT_OPTIMIZATION_STATE_ROOT` to an absolute directory
without symlink ancestors for Plane's local blob roots; the default is the
ignored `_artifacts/plane/` directory beside this file.

From the repository root:

```sh
PYTHONPATH=backend python -m prompt_optimization --task synthetic_coordinator_planning --out /absolute/private/report
```

Final model-input exposure is single-use across processes sharing that database.
Before the first final call, one normal Plane transaction creates all final-input
qualification runs through `quality_audit.create_run`. Each input has a stable,
digest-derived run identity independent of its case ID, labels, candidate, or
model label. A fresh claim ID prevents replay of the same optimization run.
An overlapping partition rolls back every fresh claim. Failed, cancelled, or
crashed runs retain their claims; deleting an output directory cannot reset them.
Only IDs and digests enter qualification metadata. Each run's category identifies
it as a final-input comparison, and the metadata binds the fixture and evaluation
context. Terminal status describes evaluation completion, not test correctness.

After exposure, supply a newly designated, untouched synthetic final partition:

```sh
PYTHONPATH=backend python -m prompt_optimization \
  --task synthetic_coordinator_planning --holdout /absolute/new-synthetic-holdout.json --out /absolute/private/new-report
```

The UTF-8 JSON envelope has exactly `schema_version: 1` and `holdout`, a list of
2–32 case objects shaped like `PlannerCase.to_dict()`. The whole file is bounded
to 65,536 bytes. Train and selection-validation remain fixed. Renamed duplicate
inputs and overlaps with earlier final inputs are refused. Fixture digests still
bind the independent labels.

Every admitted attempt, including a retry, denial, failure, and cancellation,
consumes the relevant call allowance. Token admission includes already spent and
outstanding tokens, conservative UTF-8 input size, and bounded output allowance.
Incomplete usage retains its entire reservation and remains unqualified after a
successful retry. No model/cost qualification or production prompt application
occurs.
