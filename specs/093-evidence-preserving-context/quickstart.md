# Validation guide

Use Python 3.11, declared dependencies, exact initialized components and an isolated feature checkout. Both flags default off. Enable capture only with explicit exact-scope host retention grants.

1. Focused tests: `PYTHONPATH=backend:components/AstralPlane/src python -m pytest backend/tests/test_evidence_*.py backend/tests/test_context_*.py backend/tests/test_safe_compaction.py backend/audit/tests -q`. Configure an isolated real PostgreSQL test DSN; a skipped database fixture is not a passed authorization test.
2. Existing compaction/context/ordinary dispatch/audit suites, root `ruff check .`, changed-line coverage >=90%, complete backend/module CI groups with isolated real PostgreSQL.
3. All four new control combinations plus legacy controls: off creates no capture/new auxiliary work; packing and compaction stay independent.
4. Synthetic exact reconstruction including empty/Unicode/limits; owner/audience/scope/policy/integrity denials, deletion/revocation/expiry/restart; no repeated source operation or durable raw-page copy.
5. Inject all compaction failure/cancellation/stale/reduction/provider/context-limit cases; history and protected host state remain unchanged.
6. Reconcile known/unknown retry/cache/failure/cancelled/late/recall records once, with no evidence in diagnostics. Retain paired fixed-workflow failures and unknowns.
7. Build the exact candidate image and exercise real configured IAM/database/workers and representative conversations. Verify labels/source capability on web, Android, macOS, iOS and watchOS, including explicit supported handoff. User performs required sign-in. The owner explicitly omitted Windows live verification on 2026-10-08; record Windows OS/live as untested, separately from portable Qt tests. This does not supply Windows release or default-promotion evidence.
8. Record exact candidate/test/live/formative/provider evidence in verification.md. Missing merge gates keep PR draft; missing promotion evidence keeps defaults off.

## Operator configuration

The flags are `FF_OBSERVATION_PACKING=false` and `FF_SAFE_COMPACTION=false` by default. Their files contain operator policy, never provider keys; mount them read-only and keep owner identifiers and conversation grants outside Git.

`ASTRAL_OBSERVATION_POLICY` names a bounded JSON array. Each entry has exact `owner_id`, `conversation_id`, `audience_id` (`user:` followed by that owner), `source_agent`, `source_tool`, and an absolute RFC 3339 `expires_at`. Optional `source_deadline` and `conversation_deadline` can only shorten retention. There are no wildcard grants, user/model-supplied grants or renewal on recall. The archive clamps capture to at most 24 hours and the earliest deadline. The host additionally limits privacy-screened text to 64 KiB; larger authorized results retain their original view when capture is refused. Pages remain at most 16 KiB and previews at most 1 KiB.

`ASTRAL_CONTEXT_BUDGET_POLICY` names a bounded JSON array with one exact entry for each qualified owner/provider/endpoint/model route:

```json
[
  {
    "owner_id": "synthetic-owner",
    "provider": "custom",
    "base_url": "https://synthetic-model.invalid/v1",
    "model": "synthetic-model",
    "context_tokens": 16384,
    "max_output_tokens": 512,
    "output_parameter": "max_tokens"
  }
]
```

This example is not a qualified production route. Set the actual context window and completion parameter from operator qualification; `max_completion_tokens` is also supported. The conservative UTF-8 estimate includes messages, tool schemas and output allowance. Missing, duplicate, malformed or changed bounds stop enabled work with an honest context limit. Neither a global model-window guess nor system-provider fallback is permitted.

Source inspection uses ordinary authorized tools, or `/evidence recall <reference> <byte-offset>`, `/evidence delete <reference>` and `/evidence usage`. Inspection repeats no source operation. Live references stay subject to current source/delegation/privacy/consent checks after flags are disabled. Cleanup removes source text and source arguments; a lost authorization proof yields the same blocked response as an unknown reference. Explicit successful deletion renders source unavailable.

Captured previews, pages and their generated answers remain transient. They are literal values, excluded from durable chat, completion-summary and workspace publication. Tool-step and usage receipts retain bounded metadata only. Historical or unlinked ordinary model receipts keep accounting partial; unknown usage or prices never establish a saving.

## Paired diagnostic

Run `PYTHONPATH=backend:components/AstralPlane/src python -m context_evaluation.evaluate --candidate <clean-commit-sha> --output <outside-checkout-json>`. Its frozen 24 independent workflows and four controls use separate 12/12 development and held-out tasks. Optional provider, five-user formative and qualification inputs must bind that candidate, implementation/workflow/scoring digests and actual receipts. Metadata consistency cannot authenticate actual execution or replace ordinary qualification. Missing real provider costs, unaided participant observations or client evidence stays explicit and prevents promotion; the command never changes defaults.
