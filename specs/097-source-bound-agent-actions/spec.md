# Feature Specification: Source-Bound Agent Actions

**Feature Branch**: `codex/097-source-bound-agent-actions`

**Created**: 2026-10-20

**Status**: Draft

**Input**: User request to investigate prior security holds and specify changes that make the AstralDeep ecosystem more secure.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Untrusted Content Cannot Authorize an Action (Priority: P1)

A user asks an agent to read a web page, clinical document, message, tool result, or retained memory. The source may supply facts for the answer, but instructions inside it cannot expand the user's request into a write, external transmission, privileged operation, or delegation.

**Why this priority**: A permitted read can otherwise become a confused-deputy action when source text is treated as an instruction or when its origin is lost across transformations.

**Independent Test**: Give the agent a permitted source containing both useful task data and an instruction to perform an unrequested effect. The requested read succeeds; the effect is denied and visible to the user and auditor.

**Acceptance Scenarios**:

1. **Given** a user-authorized read of an attacker-controlled source, **When** it asks the agent to send records to an address, change a permission, or run a command, **Then** the system presents the source as data and performs none of those effects.
2. **Given** an agent summarizes, translates, quotes, retrieves, or delegates source material through several steps, **When** it proposes an effect based on that material, **Then** the action retains the source's lower trust and is subject to the same denial or explicit authorization as the original material.
3. **Given** the source is unavailable, its origin is unknown, or lineage has been lost after reconnect or restart, **When** an effect is proposed, **Then** the system cannot treat the missing provenance as user authority.

---

### User Story 2 - The Owner Can Deliberately Approve a Specific Use (Priority: P2)

A user can authorize a legitimate action using information from a source, such as saving a cited finding to a private workspace or sending a selected passage to a named recipient. The approval names the action, destination, data, owner, and scope, and does not authorize a later different action.

**Why this priority**: Blanket refusal would make useful research and clinical workflows impractical; a bounded approval preserves utility without letting the model approve itself.

**Independent Test**: Approve one source-derived action, then alter its recipient, content, owner, or timing. Only the exact approved action proceeds.

**Acceptance Scenarios**:

1. **Given** an authorized user reviews a source-derived write or egress proposal, **When** the user explicitly approves its displayed destination and material, **Then** the system performs that one action under the existing permission, privacy, and audit gates.
2. **Given** a confirmation for one action, **When** the agent changes the action or replays the confirmation, **Then** the altered or repeated action is denied and requires a fresh decision.
3. **Given** a user directly specifies a value in the current request, **When** the same value also appears in a lower-trust source, **Then** the system distinguishes the user's actual instruction from source text without treating coincidental string equality as proof of authorization.

---

### User Story 3 - Operators Can See Coverage and Failures (Priority: P3)

An operator can inspect which registered actions read external data, change state, send data, or exercise privilege. Missing classifications and enforcement failures are visible before an agent becomes available for those actions.

**Why this priority**: A new tool or transport path must not silently fall outside the safety envelope because of a name, alias, or disabled optional flag.

**Independent Test**: Register a new effect tool and route it through interactive, delegated, and unattended entry points. The same classification and denial evidence appear everywhere; an unclassified effect cannot run.

**Acceptance Scenarios**:

1. **Given** a tool is newly registered or renamed, **When** its effect classification is absent or contradictory, **Then** effectful execution is unavailable and the operator receives a precise diagnostic.
2. **Given** a provenance or policy service fails during an effect proposal, **When** dispatch reaches the enforcement point, **Then** the effect fails closed while a read-only operation that needs no unavailable control can continue.
3. **Given** a denied or approved action, **When** an authorized auditor examines its record, **Then** the record identifies the actor, source class, decision, action class, bound approval if any, and outcome without exposing source contents or credentials.

### Edge Cases

- An agent returns useful text in either a structured result or presentation component; both forms retain the same source classification.
- User-supplied and third-party text overlap exactly, or a source paraphrases the user's request.
- A derived value is split, encoded, reformatted, copied into memory, or produced by a different agent or client before an effect.
- A delayed, retried, scheduled, or delegated action executes after user authority, source access, or an approval expires.
- A tool is read-only in one mode and effectful in another, or an effect hides behind a neutral name.
- A proposed write targets private storage while external egress or clinical disclosure gates still apply to its downstream use.
- An action may have started when its outcome becomes uncertain; recovery must not silently repeat it under a new approval.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Every agent-visible external, uploaded, retrieved, generated, remote-agent, and tool-produced value MUST carry an integrity origin that survives supported transformations, handoffs, retention, and restart until it is no longer usable.
- **FR-002**: Source content MUST NOT become an instruction, user consent, permission, policy rule, or approval merely by appearing in a prompt, summary, memory, search result, or tool response.
- **FR-003**: Every registered action MUST have an explicit, reviewable classification for source, read, local mutation, external disclosure, privileged control, and applicable owner/data sensitivity; effectful actions with missing or conflicting classifications MUST be unavailable.
- **FR-004**: Before every effect, the system MUST bind the actual actor, current user intent, owner, target, data lineage, effect class, policy, and any delegated authority to the proposed action. Entry point, tool alias, or agent placement MUST NOT weaken this check.
- **FR-005**: A lower-trust source MUST NOT cause an effect outside the user's independently established intent. Any permitted use of source-derived data in an effect MUST require a bounded owner decision that displays the actual destination and material and passes existing privacy, permission, delegation, and confirmation gates.
- **FR-006**: An approval MUST be single-use and bound to the exact action, target, data identity, owner, and authorization state; edits, expiry, revocation, replay, or ambiguous outcome MUST require a fresh decision or a safe reconciliation path.
- **FR-007**: Unknown lineage, unavailable enforcement, malformed policy, or missing classification MUST deny an effect with an actionable diagnostic and an audit outcome. A model or remote agent MUST NOT override the denial.
- **FR-008**: The system MUST preserve legitimate read and answer workflows and provide a clear path for users to approve a specific useful source-derived effect without granting broad continuing authority.
- **FR-009**: The system MUST record privacy-preserving, tamper-evident evidence for allow, deny, confirmation, revocation, and uncertain-outcome decisions through the existing audit chain.
- **FR-010**: Verification MUST cover direct, multi-hop, cross-agent, retained-memory, delegated, scheduled, restart, alias, malformed-policy, and failure cases through the real dispatch boundary, including every supported invocation channel.

### Key Entities *(include if feature involves data)*

- **Source origin**: Identity, owner, integrity level, sensitivity, and lifetime of material observed by an agent.
- **Derived value**: Material whose origin set includes every contributing source and transformation.
- **Action declaration**: Registered description of an action's possible reads, effects, destinations, and privilege.
- **Action decision**: Bound actor, intent, target, value lineage, effect, governing policy, approval, and outcome.
- **Bound approval**: One owner's time-limited decision for one concrete action and material identity.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In the deterministic acceptance set, 100% of source-originated unrequested effects are denied across direct, multi-hop, retained, delegated, scheduled, and reconnect paths; zero unauthorized data leaves the system.
- **SC-002**: 100% of registered effectful actions and aliases have an explicit classification, and every missing or conflicting classification prevents that action from becoming executable.
- **SC-003**: All approved source-derived actions in the acceptance set execute at most once with the exact approved actor, target, material, and scope; all altered, stale, revoked, or replayed approvals are denied.
- **SC-004**: Every induced provenance, policy, or audit failure on an effect path produces a visible failure and a reconstructable outcome; none is recorded as successful authorization.
- **SC-005**: All existing permitted read-only scenarios in the acceptance set remain usable, and at least four of five representative users can identify the proposed destination and material before authorizing a source-derived effect in a formative exercise.
- **SC-006**: Every denial and approval in the acceptance set can be traced through the authorized audit view without displaying raw patient data, source text, or credentials in the decision record.

## Assumptions

- **Mode**: Scale mode, because this changes the security-relevant dispatch boundary across multiple agents and clients. No authorization or audit shortcut is acceptable.
- Existing Keycloak identity, RFC 8693 delegation, owner isolation, PHI/egress checks, confirmation flows, LETS enforcement, and hash-chained audit remain authoritative. This feature makes provenance and effect coverage complete around those controls.
- Direct user intent and source material are separate authorities even when they contain identical bytes. A model may propose a classification or action, but the server decides.
- This spec does not authorize deployment, release, migration, or an automatic switch of any production flag. Operational posture and any component contract changes require planning and qualification under the constitution.
- Current `main` already has optional taint, policy, confirmation, and benchmark mechanisms. This spec defines the missing end-to-end contract rather than claiming they are absent or that an exploit has been demonstrated.

## Research Basis and Decision Boundaries

- **Edoardo Debenedetti et al., _Defeating Prompt Injections by Design_, arXiv:2503.18813v2, 2025 preprint**: CaMeL extracts control flow from the trusted request and uses capabilities to constrain tool data flows. It reports 77% of AgentDojo tasks solved with its stated security guarantees versus 84% for an undefended system. Applying source-bound decisions to AstralDeep's existing dispatcher is an engineering inference, not a result shown for this product. [Paper](https://arxiv.org/abs/2503.18813).
- **Manuel Costa et al., _Securing AI Agents with Information-Flow Control_, arXiv:2505.23643v2, 2025 preprint**: Fides models confidentiality and integrity labels and enforces policies outside the model. The paper motivates tracking both data origin and disclosure constraints; it does not establish correctness of AstralDeep's current string-fingerprint tracker. [Paper](https://arxiv.org/abs/2505.23643).
- **Edoardo Debenedetti et al., _AgentDojo: A Dynamic Environment to Evaluate Prompt Injection Attacks and Defenses for LLM Agents_, NeurIPS 2024 Datasets and Benchmarks Track, peer reviewed**: The benchmark contains 97 tasks and 629 security cases and shows that tool-returned content can redirect agents. Its cases motivate cross-tool acceptance tests; its published aggregate rates do not predict AstralDeep's rate. [Proceedings](https://proceedings.nips.cc/paper_files/paper/2024/hash/97091a5177d8dc64b1da8bf3e1f6fb54-Abstract-Datasets_and_Benchmarks_Track.html).
- **Repository evidence at `main` `991a79e6bcf3`**: `backend/orchestrator/taint.py` defaults tracking off, classifies sources and sinks with lists/names, and fingerprints observed strings in memory; `orchestrator.py` records successful tool `ui_components` in the taint tracker and lets policy evaluation errors allow. `.env.example` also shows `FF_TAINT_TRACKING=false`. These are code-path observations and motivate tests, not a claim of a confirmed exploit or deployed posture. Earlier [feature 033](../033-frontier-techniques-research/spec.md) specifies a taint graph; this feature defines current cross-boundary acceptance before promotion.

## Alternatives Rejected

- Prompt-only reminders, text filters, or a second model's approval are not sufficient authority for effects: source content can still influence the action proposal. They can provide diagnostics but cannot replace deterministic dispatch checks.
- Denying every use of untrusted facts in a write would block legitimate research and clinical work. The bounded owner decision in User Story 2 provides a narrower safe path.
- Turning on the existing optional taint flag without completing its origin, effect, failure, and approval semantics would claim protection that the inspected implementation does not establish.
