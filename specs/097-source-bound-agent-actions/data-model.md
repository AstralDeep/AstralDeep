# Data Model: Source-Bound Agent Actions

No new product table is planned. Live turn state is bounded and volatile; retained content whose origin cannot be verified after restart has `unknown` lineage and cannot authorize an effect.

## SourceOrigin

- Opaque source ID, source class (`signed_human`, `external`, `upload`, `tool`, `remote_agent`, `generated`, `retained`, `unknown`), owner, conversation, trust level, sensitivity class, and lifetime.
- `signed_human` requires the existing human-request authority receipt. Byte equality with source text never upgrades origin.
- Derived material receives the union of contributing source IDs. Unknown contribution yields `unknown`.

## ActionDeclaration

- Canonical agent/tool identity, aliases, version, read/source behavior, local mutation, external disclosure, privileged control, owner/data sensitivity, and target binding rule.
- A mode-dependent tool declares the union of possible effects unless a deterministic validated mode narrows it.
- Unknown or contradictory declarations block execution of possible effects.

## ActionDecision

- Actor, owner, signed request reference, canonical action, target, arguments/material digest, source-origin set, effect class, delegated authority reference, policy posture, approval reference, audit ID, and physical outcome.
- State: `proposed` → `denied` or `review_required` → `approved` → `started` → `completed` or `uncertain`.
- Only a current, fully bound decision enters `started`. Missing lineage/policy/audit returns denial; an uncertain state cannot be retried with the same approval.

## BoundApproval

- Server-held, single-use request ID, owner, session, canonical tool, exact action/target/material/argument digest, authority state, expiry, and consumption status.
- Approval is valid only after a signed interactive owner decision; the model and source cannot mint or alter it.
- Restart, revocation, expiry, argument change, owner change, or uncertain physical outcome invalidates reuse.
