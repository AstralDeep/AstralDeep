# Implementation Plan: Evidence-preserving agent context

**Branch**: `codex/093-evidence-preserving-context` | **Date**: 2026-10-08 | **Spec**: [spec.md](spec.md)

## Summary

Deliver the full feature through the existing PR: authorized exact observation recall, failure-preserving owner-bound compaction, complete attempt accounting, shared primitive source presentation and paired evaluation. Both mechanisms remain independently default off. Missing live/formative/provider evidence remains a qualification gap, never a passed result.

## Technical Context

**Language/Version**: Python 3.11; existing Projection-rendered web and native consumers.
**Primary Dependencies**: existing FastAPI/Pydantic, astralprims, pinned Plane/Projection, Keycloak/delegation, audit, PHI screening and outbound HTTP. No new dependency.
**Storage**: application-scoped bounded ephemeral archive and source-dependent generated views; existing Plane-backed audit metadata for stable attempt accounting. No schema change. Qualify the coordinated Projection evidence-modal contract in its owning repository before adopting its exact revision and manifest digest.
**Testing**: pytest/cov, root Ruff, existing complete backend CI groups and >=90% changed-line coverage; deterministic fixed-response evaluation.
**Target Platform**: Linux backend; web, Windows, Android, macOS, iOS, watchOS.
**Project Type**: server-driven service.
**Performance Goals**: <=16 KiB/page, <=8 MiB/observation, <=64 MiB/conversation; bounded global bytes/record count. No live evidence eviction or hosted wall-clock bounds.
**Constraints**: <=24-hour absolute lifetime and all earlier deadlines; exact owner/source/conversation/audience host grant, fail closed absent/ambiguous policy; no cross-owner/system provider fallback; no authority changes or raw sensitive diagnostics. Privacy screening may impose a stricter supported capture size.
**Scale/Scope**: textual evidence, safe compaction, full accounting and 24 fixed nonclinical workflows split 12 development/12 held out. Model research gains/default promotion remain separate acceptance decisions.

## Constitution Check

I/III/IV/VI: Python 3.11, tracked root Ruff, short source headers, behavior/denial/failure/concurrency tests and >=90% changed-line coverage.
II/VIII/XII/XIV: installed primitive vocabulary only; no parallel renderer or new primitive/frame/action. Projection owns the versioned evidence inspection contract, strict correlated browser/native modal handling, fixtures, all client drift guards and provenance. Deep adopts its exact qualified revision and manifest digest. Use literal KeyValue source values in transient modal viewers; watch receives content-free metadata and the explicit phone/desktop handoff. Live acceptance remains required on every owner-retained target.
VII: bind authority from server dispatch context; ordinary permissions/delegation/security/confirmation and fresh source gates, PHI/egress validation and hash-chained audit. Evidence remains untrusted.
IX: ephemeral archive; stable bounded usage metadata uses existing public Plane audit append identities, not borrowed SQL or a schema workaround.
X/XI: current local configured IAM/PostgreSQL/worker topology is the candidate live target, with representative conversations and exact candidate identity. No topology/auth/schema change. PR remains draft until production-ready and applicable local/hosted/live gates pass. No platform exception, bootstrap, signing, deployment, release or default promotion is assumed. Hosted CI keeps current deterministic whole-suite <=30-minute jobs.
X/XIII: diagnostic evaluation preserves unknown pricing/usage and held-out failures; missing formative/real-provider results forbid promotion. No evidence-policy or protected publisher changes.

Post-design: no architecture exception; qualification gates remain enforceable and missing platform access may block closure.

Owner steering on 2026-10-08 explicitly omits Windows and then Android live testing, and requires recording both as untested. Portable Qt checks on macOS and Android automated gates remain separate evidence. Continue live acceptance on web, macOS, iOS and watchOS; neither omission creates release qualification or satisfies all-client default-promotion evidence. The owner will complete native sign-in.

## Project Structure

```text
specs/093-evidence-preserving-context/
  spec.md plan.md research.md data-model.md quickstart.md tasks.md
  contracts/context.md verification.md
backend/orchestrator/
  evidence_archive.py evidence_context.py safe_compaction.py context_usage.py
  context_presentation.py context_views.py orchestrator.py local_agents.py taint.py
  projection_surfaces/evidence.py
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

**Structure Decision**: independent archive/compaction/view modules, one host authorization/accounting adapter and ordinary first-party evidence agent. Canonical assistant commits retain only strict code-owned metadata. Fresh authenticated evidence-modal reads expose source, preview and generated text through current source dependencies, unchanged IAM/work authority and final operation/navigation/source checks. Projection owns literal rendering and client request retirement; no raw evidence enters durable workspace/chat snapshots or cached chat overlays.

The evidence control adapter is host-only because the archive and typed original-human lease belong to the application orchestrator. Startup excludes its directory from subprocess inventory and capacity under every flag posture. When in-process registration is disabled or its current matching recall adapter is missing, packing preserves the complete original result; inspection commands render unavailable through the ordinary guarded path. This does not alter other bundled agents' transport fallback.

## Execution

Foundation -> independent US1 archive and US2 compaction -> US3 accounting/presentation -> US4 evaluation -> ordinary dispatch integration, complete checks, independent review, live qualification and merge. Every source group's exact outcome/arguments remains verbatim; only older plain dialogue is eligible for compaction. Pure working-view acceptance never mutates authoritative host records. Snapshot freshness/fences are checked immediately before installing the local replacement.

## Complexity Tracking

No constitution violation or published component release is justified. A coordinated Projection implementation PR and exact qualified pin adoption are necessary to retain strict request freshness without weakening completed-generation conversation fences. Promotion requires all spec acceptance evidence; a diagnostic fixed-response result does not establish real-provider savings or formative usability.
