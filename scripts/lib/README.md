# Publication scanner contract

`publication-secrets.mjs` is the byte-for-byte implementation used by Pi-Agent's
`src/publication-secrets.mjs`, under `astral-publication-secrets/v1`.
`publication_fixture_contract.json` and the fixture checker pin its SHA-256.
Update both consumers together when changing this contract; do not maintain a
second detector or add fixture exclusions.

The scanner always rejects explicitly registered credentials and private-key
headers. Other token matches are compared as per-file occurrence counts against
the trusted baseline. New values and extra copies are rejected. Binary input
uses the same UTF-8 conversion as Pi's publication path.

The Deep checker obtains baselines from verified immutable Git objects, never
from candidate files or the candidate index. Its named fixture list applies
strict source scanning even to unchanged files; it grants no exception. The
required UI contract job runs the regression suite and checks the event's exact
base and head commits. The independent Gitleaks history scan remains required.
