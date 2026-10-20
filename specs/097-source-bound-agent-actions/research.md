# Research Decisions: Source-Bound Agent Actions

Retrieved 2026-10-20. The cited research motivates a design; none proves AstralDeep secure.

## Provenance and control flow

**Decision**: Treat the signed human request as the authority source and agent-visible documents, tool results, memory, and remote replies as data. Conservatively carry an origin set through the turn and subsequent tool proposals. Unknown lineage after a transformation or restart cannot authorize an effect.

**Rationale**: Debenedetti et al., *Defeating Prompt Injections by Design*, arXiv:2503.18813v2, 2025 preprint, [paper](https://arxiv.org/abs/2503.18813), constrain agent control flow with capabilities; Costa et al., *Securing AI Agents with Information-Flow Control*, arXiv:2505.23643v2, 2025 preprint, [paper](https://arxiv.org/abs/2505.23643), use external integrity/confidentiality labels. Their results apply to their systems, not this implementation. AstralDeep's current `taint.py` uses an optional, in-memory exact-string tracker and a byte-equality user-text promotion; it does not provide the needed origin guarantee.

**Alternatives**: Prompt reminders and filters do not confer authority. Turning on the old taint flag would leave structured results, unknown transformations, and error paths incompletely covered.

## Action classification

**Decision**: Give every registered canonical action an explicit effect declaration and inspect aliases against that declaration. A missing or conflicting effect declaration blocks execution where an effect may occur. Enforcement happens in the common authorization path and again at the physical-call boundary.

**Rationale**: The current name-pattern `is_sink` can miss a neutral tool name or mode-dependent effect. The existing scope and agent-card inventory supplies candidates for review, but scope strings alone are not a trustworthy effect class.

**Alternatives**: Treating every unknown tool as read-only would fail open. Treating every unknown tool as approved write could allow a hidden disclosure.

## Exact approval and audit

**Decision**: Reuse the server-held HITL call and existing `authorize_action` card without a new UI protocol. Bind the signed owner, session, canonical tool, exact target/material/arguments, current authority, expiry, and single physical attempt. Require an audit receipt before effect execution and withhold reuse after an uncertain outcome.

**Rationale**: `hitl_confirmation.py` already checks exact arguments and prevents reuse after physical start, but its trigger is optional and its audit helper currently logs and continues on failure. Source-bound actions need an independent mandatory trigger and fail-closed audit. Review previews already refuse sensitive or oversized arguments; that remains safer than partial disclosure.

**Alternatives**: A broad standing approval or regenerated model summary cannot bind the actual destination and data. A new primitive or client action would require coordinated native work without improving the host enforcement core.

## Adaptive acceptance

**Decision**: Test direct, derived, retained, delegated, and delayed action attempts against the real dispatcher, paired with legitimate reads and exact approved effects.

**Rationale**: Debenedetti et al., *AgentDojo*, NeurIPS Datasets and Benchmarks 2024, [paper](https://proceedings.nips.cc/paper_files/paper/2024/hash/97091a5177d8dc64b1da8bf3e1f6fb54-Abstract-Datasets_and_Benchmarks_Track.html), separate attack and user-task outcomes. Ma et al., *AutoDojo*, arXiv:2606.15057, 2026 preprint, [paper](https://arxiv.org/abs/2606.15057), find that adaptive, action-open tasks are harder than static prompts. This supports a held-out adaptive study but not a universal-security claim.

**Alternatives**: A unit test of the classifier alone would miss dispatch, confirmation, and physical-effect failures.
