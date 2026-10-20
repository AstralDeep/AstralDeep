# Research Decisions: Real Agent Security Qualification

Retrieved 2026-10-20. This plan uses current repository evidence and primary research; external results do not establish AstralDeep's security.

## Real dispatch seam

**Decision**: Use signed synthetic human turns and registered admission from `backend/tests/helpers/registered_human.py` and `backend/verification/drivers/in_process.py`. A local fixture agent exposes a read surface and an observable effect sink. Scripted tool proposals enter `handle_chat_message`, `execute_single_tool`, the normal authorization gates, governed physical invocation, and audit. Do not reuse verification's development-only `_get_delegation_token` override.

**Rationale**: The existing `security_benchmark/drivers/inprocess.py` imports the orchestrator from the wrong module and raises unconditionally at its real-turn method. The signed fixture and loopback transport already exercise a much larger real boundary, including Plane admission, without a device or provider.

**Alternatives**: Calling `_authorize_and_prepare` alone skips physical effects and retries. Mocking `execute_single_tool` or `_execute_with_retry` makes an authorization unit test, not real-dispatch qualification. Running live Keycloak and a provider in required CI is nondeterministic and exceeds the offline scope.

## Corpus provenance and mapping

**Decision**: Pin AgentDojo `0.1.35` as an evaluation-only optional dependency and bind its wheel/source digest plus a versioned mapping manifest to each external-corpus run. Unsupported user tasks, tools, source surfaces, or effect oracles become named exclusions. A requested external corpus with no supported cases fails. Sample cases keep a distinct `sample` identity.

**Rationale**: All three existing adapters can fall back to samples; AgentDojo's loader returns no real cases even when import succeeds. AgentDojo's maintainers warn that its API can change. The package is MIT-licensed and Python 3.11-compatible. The published wheel has SHA-256 `364bea4219716b716bf639f504d195943f7f6a5535d312ca41d7098704a2affd`; this is a distribution identity, not a claim that its cases map to AstralDeep.

**Alternatives**: Silently substituting seven schematic cases would make the denominator false. Treating every AgentDojo case as supported without a corresponding tool and effect oracle would create false security evidence.

## Outcome validity

**Decision**: A case contract freezes the user task, attacker surface, objective, permitted outcome, and physical effect oracle before execution. Report proposal, gate refusal, actual effect, benign completion, and missing evidence separately. No trace or uncertain effect is `indeterminate` and cannot count as defended.

**Rationale**: The current adjudicator treats `executed=True` as success for several objectives and labels any attempted no-effect call blocked. A fixture effect ledger and audit-chain receipt provide independent observation. Debenedetti et al., *AgentDojo*, NeurIPS Datasets and Benchmarks 2024, [paper](https://proceedings.nips.cc/paper_files/paper/2024/hash/97091a5177d8dc64b1da8bf3e1f6fb54-Abstract-Datasets_and_Benchmarks_Track.html), motivate separate user-task and attacker outcomes. Wang et al., *Safety, or Just Capability?*, arXiv:2607.28685v1, 2026 preprint, [paper](https://arxiv.org/abs/2607.28685), demonstrate metric sensitivity across benchmark populations. Applying these lessons to AstralDeep is an inference.

**Alternatives**: Response text or an attempted tool call alone cannot prove whether an effect occurred.

## Adaptive and legitimate-task evidence

**Decision**: The deterministic offline gate uses frozen cases with paired benign controls. A defense-aware held-out study remains a separate opt-in run with declared model, attack budget, repeated-run distribution, and utility. Neither a static pass nor a scripted proposer supports a model-resistance claim.

**Rationale**: Ma et al., *AutoDojo: Adaptive Attacks Expose Superficial Defenses and User-Underspecification Limits in LLM Agents*, arXiv:2606.15057, 2026 preprint, [paper](https://arxiv.org/abs/2606.15057), report that adaptive attacks recover success against defenses that look strong on static prompts. Their measurements concern their test suites and models, not AstralDeep. Narisetty et al., *Adaptive Evaluation of Out-of-Band Defenses Against Prompt Injection in LLM Agents*, arXiv:2606.26479v1, 2026 preprint, [paper](https://arxiv.org/abs/2606.26479), give a narrower reproduction. A separate study prevents a nondeterministic third-party model from weakening the required CI gate.

**Alternatives**: A single ASR number hides benign-task loss, unsupported cases, and adaptation risk.
