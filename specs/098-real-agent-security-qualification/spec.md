# Feature Specification: Real Agent Security Qualification

**Feature Branch**: `codex/098-real-agent-security-qualification`

**Created**: 2026-10-20

**Status**: Draft

**Input**: User request to investigate prior security holds and specify changes that make the AstralDeep ecosystem more secure.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Measure Effects Through the Actual Agent Boundary (Priority: P1)

A security engineer runs an adversarial case in which an agent reads attacker-controlled material and then proposes an action. The result states whether the agent attempted the attacker objective, whether authorization rejected it, and whether an observable effect occurred through the same path used by real users.

**Why this priority**: A simulation of defenses cannot establish that AstralDeep's current dispatch, permission, delegation, privacy, confirmation, and audit controls actually stopped an effect.

**Independent Test**: Run a supported attack and benign control against the product boundary in an isolated environment. Match the reported decisions and effects to the recorded dispatch and audit trace.

**Acceptance Scenarios**:

1. **Given** a supported injection case with a permitted read and forbidden write, **When** the agent handles the source, **Then** the report distinguishes no attack attempt, blocked attempt, and completed effect using observed evidence.
2. **Given** a case whose requested capability or benchmark data cannot be loaded, **When** qualification starts, **Then** it fails or records a named exclusion before scoring; it never silently substitutes sample cases or simulated enforcement.
3. **Given** an effect's outcome is unavailable or ambiguous, **When** results are adjudicated, **Then** the case is labeled unresolved and cannot count as a defended success.

---

### User Story 2 - Defenses Survive New and Adaptive Attacks (Priority: P2)

A researcher evaluates frozen attacks and then tests defense-aware variants against the same declared user tasks. The report shows attack success, legitimate task completion, false denials, and the actual case population so a lower attack rate is not mistaken for an unusable agent.

**Why this priority**: Static cases can overstate robustness when an attacker adapts to a published defense or changes a tool result across multiple turns.

**Independent Test**: Freeze a task and acceptance oracle, vary only the lower-trust material using an attacker-visible defense description, and compare attempted effects, completed effects, and benign task completion.

**Acceptance Scenarios**:

1. **Given** a frozen supported corpus and a declared defense posture, **When** defense-aware attacks are evaluated, **Then** the report separately records static and adaptive results with exact versions, attempts, and exclusions.
2. **Given** an attack that crosses a tool hop, memory recall, delegation, or delayed action, **When** an effect is proposed, **Then** the final result is tied to the originating case and the actual action outcome.
3. **Given** a security change that blocks legitimate tasks, **When** paired benign cases run, **Then** the utility loss and false denials appear beside attack success rather than being hidden by the security metric.

---

### User Story 3 - Release Owners Receive Honest, Bounded Evidence (Priority: P3)

A release owner sees what passed on the exact candidate, which results are simulated diagnostics, which were measured against actual dispatch, and which claims are still unqualified. Required offline checks stay deterministic and within the hosted job budget; live or adaptive model studies are separately identified.

**Why this priority**: Security claims need a traceable candidate, reproducible cases, and an explicit boundary between diagnostic output and release evidence.

**Independent Test**: Deliberately weaken one authorization gate. The required check fails on an observed unauthorized effect and identifies the responsible case without network dependence.

**Acceptance Scenarios**:

1. **Given** a candidate that weakens a real dispatch gate, **When** its required offline qualification runs, **Then** an effect oracle fails and the candidate cannot inherit an older passing result.
2. **Given** a simulated run or a sample corpus, **When** a report is generated, **Then** it is labeled diagnostic and cannot be described as real-dispatch benchmark evidence.
3. **Given** an interrupted run, incomplete teardown, missing source version, or missing audit receipt, **When** the owner reviews qualification, **Then** the report explicitly marks it incomplete and withholds a passing disposition.

### Edge Cases

- The model declines the malicious instruction before proposing an effect; this is not scored as a dispatch-layer block.
- A benchmark scores a tool-call attempt while AstralDeep stops the effect later; the report states both points separately.
- The adapter maps a case to an unavailable tool, permission, or data shape; the case becomes a visible exclusion with a reason.
- A corpus is present but differs in content, license, or version from the declared run.
- An action is retried, times out after a possible effect, or resumes after a node restart.
- An attack is present in a nested tool result, attachment, memory, generated summary, remote-agent response, or cross-client flow.
- A purportedly isolated test leaves records, credentials, or attack text in another owner's data.
- An adaptive attack generator sees prior outcomes; only predeclared attempts and budgets are compared.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Qualification MUST exercise the product's actual authenticated agent dispatch and effect boundary for every run labeled real, including ordinary permission, delegation, privacy, confirmation, provenance, and audit decisions applicable to that case.
- **FR-002**: Simulated defenses and sample corpora MAY remain as diagnostics, but their results MUST have distinct labels and MUST NOT satisfy a real-dispatch or external-benchmark claim. A scripted proposer MAY drive a deterministic real-dispatch gate when the signed request, authorization, physical effect path, and audit are real; that result MUST be labeled scripted-proposer and MUST NOT support a model-resistance claim.
- **FR-003**: Selecting a named external corpus MUST use the exact declared version and content, or fail visibly. Unsupported cases MUST be enumerated with reasons and excluded from the supported-case denominator; an empty supported set MUST fail qualification.
- **FR-004**: Each case MUST have a declared legitimate task, attacker-controlled surface, attacker objective, expected permitted behavior, adjudication point, and observable effect oracle before execution.
- **FR-005**: Results MUST distinguish attack not attempted, attempted and denied, effect completed, legitimate task failed, indeterminate, and unsupported; an indeterminate or missing trace MUST NOT count as a defended success.
- **FR-006**: Every report MUST include the exact product candidate, component and policy posture, corpus identity, test data identity, model identity when used, attack budget, case counts, exclusions, per-case observations, and uncertainty or nondeterminism.
- **FR-007**: The evaluation MUST measure both security and legitimate task completion, including false denials, on paired task sets. It MUST NOT present a reduction in attack success alone as a net improvement.
- **FR-008**: Qualification MUST include a frozen baseline and a defense-aware, held-out attack set that covers at least direct, multi-hop, retained-context, delegated, and delayed-action attempts. The latter MAY be a separately approved research run when model variability prevents a deterministic hosted gate.
- **FR-009**: A bounded, offline, deterministic real-dispatch subset MUST fail on observed unauthorized effects and remain within the existing hosted job limit. Live third-party services and model sampling MUST NOT be required by that check.
- **FR-010**: All cases MUST use isolated synthetic owners and data, preserve evidence needed for review, and verify teardown without affecting real user data or leaving adversarial text in ordinary memory.
- **FR-011**: Security reports MUST be tied to the exact candidate and expire as qualification evidence when code, policy, corpus, adjudication, or relevant component pins change.
- **FR-012**: Research reports MUST state the supported population and limitations; they MUST NOT claim general prompt-injection immunity from a finite corpus or one model.

### Key Entities *(include if feature involves data)*

- **Case contract**: Frozen legitimate task, attacker surface and objective, permitted outcome, effect oracle, and exclusion rule.
- **Run identity**: Exact candidate, policy posture, corpus, model, seed or sampling policy, and attack budget.
- **Observed trace**: Authenticated dispatch, proposed call, gate decisions, audit references, and effect outcome.
- **Adjudication**: One of the named outcome states with evidence and a declared observation point.
- **Qualification report**: Per-case results, denominators, exclusions, legitimate utility, attack success, uncertainty, and completion status.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: At least one versioned external corpus and one AstralDeep-specific set run through actual authenticated dispatch for all mapped cases. Every mapped case has a complete trace or an explicit indeterminate outcome; no requested real run silently uses samples.
- **SC-002**: The initial supported acceptance set contains at least 12 distinct attack cases across three lower-trust surfaces and 12 paired benign controls. It includes at least one multi-hop, one delegated, one retained-context, and one delayed-action case, with all exclusions reported.
- **SC-003**: An intentionally weakened authorization or privacy gate produces an observed unauthorized effect in the acceptance environment and fails the required offline check; the unchanged candidate passes that check within 30 minutes on a hosted runner.
- **SC-004**: For every reported result, attempted versus completed effects and legitimate task completion can be reconstructed from the case contract and observed trace; zero missing traces are counted as defended successes.
- **SC-005**: Replaying the deterministic subset against the same frozen candidate and case set yields identical adjudications. Variable-model studies report repeated-run distributions and exact attempt counts rather than claiming deterministic equality.
- **SC-006**: All synthetic test principals and stored attack material are accounted for after teardown, with zero contamination of ordinary owner data; cleanup failure withholds a passing disposition.
- **SC-007**: A defense-aware study reports both attack success and benign utility for at least five attack families and identifies unsupported surfaces and unresolved cases. It makes no universal-security claim.

## Assumptions

- **Mode**: Scale mode for security qualification because incomplete evidence could authorize unsafe releases. The first required cohort remains bounded under the constitution's 30-minute CI limit.
- This is a successor to [feature 047](../047-security-benchmark-harness/spec.md), which already specifies an in-process benchmark and honest attack adjudication. It does not replace that intent; it closes the gap between those claims and current executable behavior.
- External corpora are used only under their research/evaluation terms. Sensitive clinical cases use synthetic data and do not invoke live third-party systems in required checks.
- The exact case mix and eligible external corpus are finalized during planning after adapter feasibility and license checks. No specific benchmark-wide attack success target is assumed before a baseline is measured.
- The feature does not modify product authorization rules. A failing study creates a repair task and a new exact-candidate qualification obligation, not an automatic policy waiver.

## Research Basis and Decision Boundaries

- **Edoardo Debenedetti et al., _AgentDojo: A Dynamic Environment to Evaluate Prompt Injection Attacks and Defenses for LLM Agents_, NeurIPS 2024 Datasets and Benchmarks Track, peer reviewed**: It provides 97 tasks and 629 security cases with stateful tool use and separate attack and legitimate-task outcomes. Those results establish a useful evaluation method, not AstralDeep's attack success rate. [Proceedings](https://proceedings.nips.cc/paper_files/paper/2024/hash/97091a5177d8dc64b1da8bf3e1f6fb54-Abstract-Datasets_and_Benchmarks_Track.html).
- **Praneeth Narisetty, Shiva Nagendra Babu Kore, Uday Kumar Reddy Kattamanchi, and Jayaram Kumarapu, _Adaptive Evaluation of Out-of-Band Defenses Against Prompt Injection in LLM Agents_, arXiv:2606.26479v1, 2026 preprint**: It argues for defense-aware tests and reports one limited Progent reproduction with a weak model and one attack template. Its result does not prove general robustness. Requiring a held-out adaptive study for AstralDeep is an engineering inference. [Paper](https://arxiv.org/abs/2606.26479).
- **Edoardo Debenedetti et al., _Defeating Prompt Injections by Design_, arXiv:2503.18813v2, 2025 preprint**: CaMeL couples security claims to a constrained agent design and reports task utility as well as security. This supports reporting paired utility; it does not validate AstralDeep's envelope. [Paper](https://arxiv.org/abs/2503.18813).
- **Repository evidence at `main` `991a79e6bcf3`**: `backend/security_benchmark/__main__.py` defaults to synthetic mode. The current `inprocess.py` real-turn method raises instead of executing a case. `adapters/agentdojo.py` returns no real cases after importing the package and falls back to seven sample cases; the README labels in-process as real and describes external corpora. Searches of current CI workflows found no invocation of this benchmark CLI. These observations show a qualification gap; they do not imply any specific attack has succeeded against a deployed system.

## Alternatives Rejected

- Treating the current scripted ablation table as measured product attack resistance would confuse simulated enforcement with an observed effect.
- Adding a larger static prompt corpus alone would miss authorization and effect outcomes. Cases must run through the actual boundary and include adaptive variants.
- Requiring live providers in protected CI would make the gate nondeterministic and conflict with the repository's hosted job policy. Keep the required subset offline and report live studies separately.
