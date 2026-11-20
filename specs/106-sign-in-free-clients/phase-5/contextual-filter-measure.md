# Contextual filter measure, a proposal for approval

This is a proposal. Nothing here has been run. Every number is marked "(proposed)" and you can change any of them. Approving it settles TO.7.

## Why a measure

The Kernel floor reads fixed rules. It cannot find a bare name with no clinical context. Presidio finds those today, but the Kernel cannot run Presidio. The on-device model is meant to cover that gap. The research says the model filter is not yet qualified. This measure decides when it is.

The model can only add findings, and each one is `uncertain`. It can never lower a verdict. An uncertain match asks you, and counts as PHI when nobody can be asked.

## The four arms

Each arm gets the same corpus.

- **Baseline.** Deep `contains_phi_for_sharing` with Presidio at the versions pinned in the backend requirements. Its verdict is yes or no, so it has no `uncertain`.
- **Floor.** The Kernel detector alone.
- **Floor with model.** The floor, plus the Apple Foundation Models filter under the `astral.phi-filter/v1` request, schema, chunk size and timeout.
- **Optional.** The floor plus the OpenAI privacy filter running on the node (Deep #318). It runs only if the model is available on the Mac at the time.

For the baseline, `phi` counts as caught.

## The corpus

Synthetic only. No real patient text enters it. Every item is labeled when it is generated, so no person judges a label.

A fresh seed builds it. It is not the detector's own corpus, because the floor's rules were tuned against that one. The seed and generator are committed. The corpus is built once and never tuned against.

PHI-bearing items cover each of the HIPAA identifier types:

- names, with a label, with a clinical phrase, and bare
- street and smaller geographic parts
- dates
- phone and fax numbers
- email addresses
- record, account, plan and license numbers
- device and vehicle identifiers
- URLs and IP addresses
- biometric and photo references
- any other unique code

The same details also appear in other forms:

- encoded forms, such as percent escapes and HTML references
- multilingual text
- long inputs that fill several chunks
- structured tool output, where integers and floats are not screened as values

Benign items are:

- the clinical controls fixture
- clinical prose with no identifier
- multilingual prose
- long benign text

Size, per identifier type: at least 60 PHI items (proposed). Benign items: at least 600 in total (proposed). Each item is run 3 times on the model arms (proposed), and the lowest result counts.

## What is measured

Per arm:

1. **Item recall.** The share of PHI-bearing items caught. `phi` and `uncertain` both count as caught.
2. **Recall by identifier type.** The same, split by type.
3. **Ask rate.** The share of benign items that return `uncertain`. Reported for the floor alone and for floor with model, so you can see what the model adds.
4. **False PHI rate.** The share of benign items that return `phi`.
5. **Model failure rate.** The share of model calls that fail, time out or fail the answer check. Each is `phi` with `detector_failed`. It is reported only. No threshold applies.
6. **Lowering count.** The number of items where adding the model gave a lower verdict than the floor alone. This must be zero.

## Proposed thresholds

The floor with model passes when all of these hold:

- Recall at or above Presidio's for every identifier type, one by one (proposed).
- Name recall at least 0.9 of Presidio's name recall (proposed). Names are the case where the floor is weakest and Presidio is strongest, so the bar is set a little below parity.
- Benign ask rate at most 10% (proposed).
- Lowering count of 0 (proposed).

A pass on one run is not enough. All 3 runs must pass (proposed).

## What the thresholds do not cover

The model's answer carries no confidence. The v1 schema holds only a kind and a text. A rule such as "above a measured confidence" therefore needs a stand-in. One option is agreement across the 3 runs (proposed). Another is a schema change in a later version. Your call.

## What approval changes

If the thresholds are met, a later version may promote model findings above a measured confidence from `uncertain` to `clear` (OD4). That would be a new format version with its own vectors and a new owner approval.

If they are missed, today's rule stays. Every model finding is `uncertain`, and the owner is asked.

Either way the floor and its verdicts do not change.

## How it runs

Offline on your Mac, as a diagnostic. It is not part of required CI. It needs no network and reads no clock for a verdict.

The report records the Mac model, the OS version, the Foundation Models version, the Presidio and spaCy model versions, the corpus seed and the commit. There is no wall-clock gate. The model timeout is the contract's own and runs on the Kernel clock.

## Decisions for you

1. Approve the four arms, or drop the optional one.
2. Approve the corpus size (proposed above), or change it.
3. Approve the thresholds (proposed above), or change them.
4. Pick the confidence stand-in: agreement across runs, or a schema change later.
