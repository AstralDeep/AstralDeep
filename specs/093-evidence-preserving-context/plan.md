# Implementation Plan: Evidence-preserving agent context

**Branch**: `codex/093-evidence-preserving-context` | **Date**: 2026-10-08 | **Spec**: [spec.md](spec.md)

## Summary

Deliver the full feature through the existing PR: authorized exact observation recall, failure-preserving owner-bound compaction, complete attempt accounting, shared primitive source presentation and paired evaluation. Both mechanisms remain independently default off. Missing live/formative/provider evidence remains a qualification gap, never a passed result.

## Technical Context

**Language/Version**: Python 3.11; existing Projection-rendered web and native consumers.
**Primary Dependencies**: existing FastAPI/Pydantic, astralprims, pinned Plane/Projection, Keycloak/delegation, audit, PHI screening and outbound HTTP. No new dependency.
**Storage**: application-scoped bounded ephemeral archive; existing Plane-backed audit metadata for stable attempt accounting. No schema/component pin change.
**Testing**: pytest/cov, root Ruff, existing complete backend CI groups and >=90% changed-line coverage; deterministic fixed-response evaluation.
**Target Platform**: Linux backend; web, Windows, Android, macOS, iOS, watchOS.
**Project Type**: server-driven service.
**Performance Goals**: <=16 KiB/page, <=8 MiB/observation, <=64 MiB/conversation; bounded global bytes/record count. No live evidence eviction or hosted wall-clock bounds.
**Constraints**: <=24-hour absolute lifetime and all earlier deadlines; exact owner/source/conversation/audience host grant, fail closed absent/ambiguous policy; no cross-owner/system provider fallback; no authority changes or raw sensitive diagnostics. Privacy screening may impose a stricter supported capture size.
**Scale/Scope**: textual evidence, safe compaction, full accounting and 24 fixed nonclinical workflows split 12 development/12 held out. Model research gains/default promotion remain separate acceptance decisions.

## Constitution Check

I/III/IV/VI: Python 3.11, tracked root Ruff, short source headers, behavior/denial/failure/concurrency tests and >=90% changed-line coverage.
II/VIII/XII/XIV: installed primitive vocabulary only; no parallel renderer, protocol, component source or pin. Use KeyValue literal source values on all six clients and ordinary governed tool actions; watch receives the documented supported action handoff. Live acceptance on every target is required before complete qualification.
VII: bind authority from server dispatch context; ordinary permissions/delegation/security/confirmation and fresh source gates, PHI/egress validation and hash-chained audit. Evidence remains untrusted.
IX: ephemeral archive; stable bounded usage metadata uses existing public Plane audit append identities, not borrowed SQL or a schema workaround.
X/XI: current local configured IAM/PostgreSQL/worker topology is the candidate live target, with representative conversations and exact candidate identity. No topology/auth/schema change. PR remains draft until production-ready and applicable local/hosted/live gates pass. No platform exception, bootstrap, signing, deployment, release or default promotion is assumed. Hosted CI keeps current deterministic whole-suite <=30-minute jobs.
X/XIII: diagnostic evaluation preserves unknown pricing/usage and held-out failures; missing formative/real-provider results forbid promotion. No evidence-policy or protected publisher changes.

Post-design: no architecture exception; qualification gates remain enforceable and missing platform access may block closure.

## Project Structure

```text
specs/093-evidence-preserving-context/
  spec.md plan.md research.md data-model.md quickstart.md tasks.md
  contracts/context.md verification.md
backend/orchestrator/
  evidence_archive.py evidence_context.py safe_compaction.py context_usage.py
  context_presentation.py orchestrator.py local_agents.py taint.py
backend/agents/evidence/
  evidence_agent.py mcp_server.py
backend/audit/
  schemas.py repository.py
backend/context_evaluation/
  evaluate.py workflows.json
backend/tests/
  test_evidence_archive.py test_safe_compaction.py test_evidence_context.py
  test_context_usage.py test_context_presentation.py test_context_evaluation.py
```

**Structure Decision**: independent archive/compaction modules, one host authorization/accounting adapter and ordinary first-party evidence agent. Shared source presentation uses server-owned primitives; evidence pages are delivered transiently and excluded from durable workspace/chat snapshots. Durable labels/reference metadata carry no evidence bytes.

## Execution

Foundation -> independent US1 archive and US2 compaction -> US3 accounting/presentation -> US4 evaluation -> ordinary dispatch integration, complete checks, independent review, live qualification and merge. Every source group's exact outcome/arguments remains verbatim; only older plain dialogue is eligible for compaction. Pure working-view acceptance never mutates authoritative host records. Snapshot freshness/fences are checked immediately before installing the local replacement.

## Complexity Tracking

No constitution violation or component release is justified. Promotion requires all spec acceptance evidence; a diagnostic fixed-response result does not establish real-provider savings or formative usability.
