# Implementation research

Date: 2026-10-08. Baseline main `5c35bb38227055dadf25a02d0ac17e70d4bfbc8e`, reconciled into the original branch. Three independent read-only agents inspected recall, compaction and presentation/accounting; the implementer verifies critical seams.

## Retention

Decision: bounded application archive with exact host grants for owner, conversation, source agent/tool, audience and absolute deadline. Missing/ambiguous/changed/unavailable policy refuses capture. Read permission and provider-sharing acknowledgments are not retention grants. Restart starts an empty archive; old sources are unavailable, never reconstructed.
Rationale: pinned Plane has no neutral observation archive/grant facade. Credential, knowledge, personalization, assignment and attachment contracts cannot be repurposed. The spec explicitly permits unavailable sources after restart. Existing assignment `none|operation` retention must refuse new capture. PHIGate's redactor supports <=64 KiB; larger source capture is initially refused honestly rather than bypassing screening.
Alternatives: durable archives require a separately qualified Plane migration; unrelated repositories cannot supply retention authority.

## Compaction

Decision: independent default-off safe compaction over immutable input/protected snapshots. Preserve system/developer instructions, current user, all complete tool groups/failures and authoritative host state. Summarize only eligible older plain dialogue; do not truncate summary input. Reject errors, empty/malformed/oversized/non-reducing/stale/cancelled candidates. Generated prose is assistant evidence, never system authority. Pause if protected material cannot fit.
Rationale: current `compaction.py` removes history after failed summary and inserts a system notice. `orchestrator.py` passes a null socket, selects system credentials and independently tombstones observations; enabled mechanisms must supersede those transforms and pre-history slicing.
Alternatives: arbitrary tool-group reduction expands the preservation oracle; retaining groups is conservative. No durable host replacement transaction is needed because compaction changes only the request-local working view and cannot overwrite host records.

## Authorization and presentation

Decision: ordinary evidence agent, bound server dispatch context, fresh original-source gates before recall, and ordinary audited/governed effect. No meta-tool shortcut. Mark preview/recall as untrusted and retain datamarking. Literal KeyValue values are supported without source interpretation on web/Qt/Compose/Swift/watch; generic watch action uses an explicit supported phone/desktop handoff. Raw pages are transient, never persisted as new chat/workspace evidence copies.
Rationale: generic Text is Markdown on some clients; KeyValue values are escaped/literal on all six. Watch limits general buttons/code, so do not spoof allowed watch markers. Ordinary tool UI is normally persisted; evidence needs a transient delivery path.
Alternatives: new vocabulary would require component-first coordinated client releases; raw ordinary durable pages could bypass absolute retention.

## Accounting and evaluation

Decision: physical attempt IDs with owner/conversation ledgers and stable Plane-backed audit events. Record unknown usage/pricing, cache counts, retries, failures, cancellation/late results and recall bytes. Totals deduplicate attempts rather than deliveries. Instrument all conversation model seams.
Rationale: `_accumulate_usage` treats missing counts as zero and misses auxiliary/retry work. Plane already deduplicates audit event identities; Deep currently allocates new UUIDs, so expose stable optional identity through that facade.
Alternatives: new SQL/usage schema is unnecessary for bounded metadata; existing increment-only token totals cannot establish complete cost.
Fixed paired 24-workflow development/holdout evaluation remains deterministic. Real providers/formative users/client qualification are separate evidence, and unknown or failed inputs block promotion.
