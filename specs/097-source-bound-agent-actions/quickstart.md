# Validation Guide: Source-Bound Agent Actions

Run from the repository root with Python 3.11, the pinned components, and an isolated local backend/PostgreSQL environment. No Apple or Android device is needed for backend enforcement; the user requested web-only client tests.

1. Inventory every registered canonical tool and alias, verifying the declared source/effect class. An omitted or contradictory effect declaration must prevent execution.
2. Run `cd backend; python -m pytest tests/test_source_bound.py tests/test_source_bound_dispatch.py -q` after implementation. Cover permitted read, unrequested write, coincident text, derived/retained/delegated/delayed material, exact approval, replay, expiry, and failure cases.
3. Through the live local web client, read a fixture containing an instruction to send or mutate data. The read should succeed; the effect must be denied or presented as one exact owner review. Approve once, then alter or replay the call and verify refusal.
4. Disable provenance or audit in an isolated test and confirm an effect fails closed with a reconstructable diagnostic. Verify the ordinary permission, PHI, egress, delegation, and LETS gates still run.
5. Run grouped backend web tests, Ruff, and changed-line coverage. Do not claim native client verification: Apple and Android tests are intentionally omitted at the owner's direction.

The current optional taint and policy flags do not constitute this feature's enforcement.
