# Feature-093 Context Adoption Evidence

This directory contains the evidence for the context adoption decision related to feature-093, as required by issue #273.

## Structure

- `development_fixtures/`: Fixtures used for development and initial evaluation.
- `held_out_fixtures/`: Fixtures held out from development to validate the solution.
- `evaluation_script.py`: Script to run the evaluation and collect results.
- `adoption_decision.md`: The recorded adoption decision.

## Usage

Run the evaluation script to generate the evidence:

```bash
python evidence/feature-093/evaluation_script.py
```

## Acceptance Criteria

The evaluation is bound to the feature-093 plan and follows the acceptance rules defined in `specs/093-evidence-preserving-context/spec.md`.

## Notes

- This evidence task does not defer any implementation child's required security, coverage, deterministic CI, staging or client checks.
- The evaluation retains failed/unknown results, source identities, and accounting.
