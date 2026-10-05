# Feature Specification: Evidence-preserving agent context

**Feature Branch**: `codex/093-evidence-preserving-context`

**Created**: 2026-10-05

**Status**: Draft; specification reviewed for completeness, ready for clarification and planning. No implementation, adoption decision, benchmark result, deployment, or release is implied.

**Input**: User description: "Create a spec for paper #2, SoL-Pi, that improves the usability and functionality of the AstralDeep ecosystem."

Give users and their agents a shorter working context without losing access to the evidence behind their answers. The initial feature provides exact recall of authorized observations, protects history when compaction fails, and measures complete cost and answer quality before any default changes.

**Engineering mode**: Scale mode for authorization, auditability, privacy, retention, concurrency, recovery, and accounting; bounded initial product scope.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Recover an exact detail from a shortened observation (Priority: P1)

As a user working through a large dataset manifest or research result, I want my agent and me to recover the original authorized text behind a shortened result so that an omitted detail does not become a guess or force a repeat download.

**Why this priority**: Exact evidence is useful independently of automatic summarization and directly supports the promising observation-packing mechanism from the paper.

**Independent Test**: Capture a synthetic long observation containing distinct beginning, middle, and ending facts. Replace its working view with a labeled preview, then recover each fact and reconstruct the captured text from bounded pages without repeating the source operation.

**Acceptance Scenarios**:

1. **Given** an authorized retained observation whose working view is shortened, **When** the user or an authorized delegated agent requests a valid page, **Then** the response contains the exact permitted source text for that page, its observation identity, location, and integrity identity, with an explicit partial-view indication.
2. **Given** an agent needs a fact absent from the preview, **When** it recalls the page containing that fact, **Then** it can cite the fact to the captured observation and does not represent the preview as the entire result.
3. **Given** another owner supplies a valid-looking observation reference, or access has been revoked, **When** recall is requested, **Then** access is denied without disclosing content, ownership, size, existence, or a usable alternative reference.
4. **Given** a retained source has expired, been deleted, or failed its integrity check, **When** recall is requested, **Then** the response reports an unavailable source and an appropriate authorized recovery option without inventing text or silently fetching a replacement snapshot.

### User Story 2 - Continue safely when compaction fails (Priority: P1)

As a user in a long conversation, I want failed or incomplete summarization to leave my working history intact so that provider errors, cancellation, or a misleading summary do not erase instructions, evidence, or unfinished work.

**Why this priority**: The existing compaction failure behavior can remove earlier turns after replacing them with a generic notice. Correcting that behavior is valuable even before observation packing is enabled.

**Independent Test**: Start with a conversation near its context limit and inject failed, empty, oversized, stale, cancelled, and invalid summary results. Compare the history and pending actions before and after each attempt; successful compaction must preserve the protected state and reduce the working context.

**Acceptance Scenarios**:

1. **Given** a compaction attempt encounters a provider error, timeout, cancellation, empty result, invalid evidence reference, or no size reduction, **When** the attempt finishes, **Then** the original history remains unchanged and the user receives an honest recoverable status.
2. **Given** valid compaction of eligible older turns, **When** the replacement is accepted, **Then** governing instructions and the current user request remain verbatim, authoritative host state remains unchanged, and complete tool-call/result groups and captured failures remain intact or carry an explicit unchanged outcome with retained exact evidence.
3. **Given** the conversation changes while a summary is being prepared, **When** the older candidate returns, **Then** it cannot overwrite newer turns, cleared approvals, revocations, or another accepted compaction.
4. **Given** the authorized model's context limit cannot be met safely, **When** no valid compaction is available, **Then** the system pauses with a context-limit explanation and authorized continuation options rather than silently dropping protected material or reporting success.

### User Story 3 - Understand what was shortened and what the work cost (Priority: P2)

As a user, I want to distinguish a preview, a generated summary, and the captured source, and see the cost of the whole operation so that shorter prompts do not hide lost evidence or extra model spending.

**Why this priority**: Honest presentation and complete accounting make the optimization assessable and expose regressions before wider use.

**Independent Test**: Exercise the same preview, recall, compaction success, and compaction failure on every supported client. Check source access, displayed state, and totals against independently recorded synthetic provider usage, including charged failures and retries.

**Acceptance Scenarios**:

1. **Given** an answer uses a shortened observation or generated summary, **When** the user inspects its evidence, **Then** the presentation labels its origin and omissions and offers the same authorized source-recall capability on every supported client.
2. **Given** summarization consumes model usage, **When** totals are shown, **Then** those charges appear in the owning conversation's complete totals even if the summary is rejected, cancelled, or never displayed.
3. **Given** a provider omits usage or pricing information, **When** totals are reported, **Then** the affected amount is explicitly unknown or conservatively estimated and cannot be counted as zero for an efficiency claim.
4. **Given** the user's authorized provider is unavailable or prohibited for this content, **When** compaction is considered, **Then** no other owner's or system provider receives the content as an automatic fallback.

### User Story 4 - Decide whether the feature earns promotion (Priority: P2)

As an operator, I want independent quality, safety, usability, and complete-cost evidence for each mechanism so that a benchmark efficiency claim does not turn into a default that makes real work less reliable.

**Why this priority**: The complete SoL-Pi stack reports lower point-estimate scores on the cited evaluations; AstralDeep needs its own acceptance evidence.

**Independent Test**: Freeze the candidate and evaluation definitions, run paired baseline and candidate workflows over a separate held-out set, and verify that a failed capability, boundary, integrity, or complete-cost criterion prevents promotion.

**Acceptance Scenarios**:

1. **Given** observation packing and safe compaction can be selected independently, **When** each is evaluated, **Then** its report separates that mechanism's quality and complete cost from the combined feature.
2. **Given** any held-out task, authorization, failure-preservation, or evidence-integrity regression, **When** promotion is requested, **Then** the candidate remains disabled by default and the failed result is retained without changing the acceptance rules.
3. **Given** the feature is disabled, **When** ordinary conversations and tools run, **Then** their baseline behavior remains unchanged and no observation archive or auxiliary model call is created by the new feature.

### Edge Cases

- Empty observations, Unicode boundaries, structured results, repeated identical results, and multiple outputs from one tool invocation retain distinct identities and honest empty/partial states.
- Oversized observations or exhausted per-conversation capacity do not evict still-needed evidence silently; shortening is refused and an explicit context or retention limit is reported.
- Invalid, negative, overlapping, or beyond-end page requests return a bounded deterministic error or explicit end-of-source state; input values cannot escape the referenced observation.
- Tool output containing instructions, executable-looking actions, credentials, personal data, or clinical data retains its existing untrusted status and mandatory handling rules throughout preview, summary, recall, and rendering.
- A claim of success cannot omit a captured error, denial, incomplete transfer, unresolved approval, or conflicting observation merely because the successful excerpt is shorter.
- Deletion, policy revocation, expiry, restart, and recovery cannot resurrect prohibited content or turn a stale reference into authority.
- A crash or duplicate completion cannot install half a replacement, duplicate accounting, or detach a retained observation from its owner and conversation.
- Cancellation may race with a provider charging for work; accounting records a known charge once, while the history remains unchanged if replacement was not committed.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST support exact recall of a captured, authorized textual observation independently of automatic compaction. "Exact" means the same permitted text after the mandatory redaction and normalization applied at capture; it does not mean unrestricted raw files, binary artifacts, credentials, or hidden provider data.
- **FR-002**: Each retained observation MUST identify its owner, conversation, originating operation, intended audience, source snapshot, capture order, integrity identity, and retention state. Repeated content MUST NOT merge different owners, audiences, or operations.
- **FR-003**: A shortened observation MUST retain its stable source reference, the operation's actual success/error/denial state, a labeled preview, omission indication, and a bounded recall instruction. A reference or summary MUST NOT itself grant authorization.
- **FR-004**: Recall MUST return deterministic source pages no larger than the approved page limit, with explicit locations, ordering, total extent where authorized, and continuation/end state. Reassembling all valid pages MUST reproduce the permitted captured text without omissions or duplication.
- **FR-005**: Every recall and compaction operation MUST enforce the current owner's identity, delegated authority, tool permissions, privacy, outbound-content policy, confirmation requirements, and revocation state through the existing authorized operation path. Denied references MUST NOT reveal source existence or content.
- **FR-006**: Preview, generated summary, and recalled text MUST remain untrusted evidence. Embedded instructions, links, role claims, and action-shaped data MUST NOT acquire command, approval, tool, or policy authority by being retained or summarized.
- **FR-007**: Recall MUST verify the bound source's integrity before returning content. Invalid references, mismatched identities, integrity failures, or unavailable source states MUST fail closed with an explicit authorized recovery outcome.
- **FR-008**: Capture MUST be permitted by the applicable source, audience, and conversation retention policy; missing or ambiguous permission to retain MUST refuse capture. Expiry MUST be absolute and no later than 24 hours after capture or any earlier source/conversation/policy deadline. Recall, compaction, restart, and recovery MUST NOT renew expiry. Deletion and revocation MUST make content unavailable immediately and trigger policy-governed cleanup; the feature MUST NOT create an unmanaged permanent copy or silently extend retention.
- **FR-009**: Shortening MUST occur only after the permitted original observation is available for recall within its approved retention window. A failed capture, unsupported result type, or exhausted limit MUST leave the existing authorized view intact and surface the limit; no successful archival claim is allowed.
- **FR-010**: Compaction MUST propose a bounded replacement while leaving the active history unchanged. A replacement MUST be installed only as one accepted state transition after checking its source identities, protected material, current conversation state, and actual context reduction.
- **FR-011**: Provider error, timeout, cancellation, empty or malformed summary, rejected evidence, stale history, retention failure, or ineffective reduction MUST preserve the previous history and pending-action state. The system MUST NOT substitute a generic notice and discard the history.
- **FR-012**: Governing instructions and the current user turn MUST remain verbatim in the active context. Authoritative host records for pending approvals, open work items, accepted operation parameters, source/manifest identities, revocations, and operation outcomes MUST remain unchanged. Every captured error, denial, and incomplete operation in the compacted range MUST retain its actual outcome and complete tool-call/result relationship, either intact or with an explicit outcome and exact available source reference. Validation MUST compare these protected records and references with the pre-attempt snapshot; an absent, changed, or unverifiable protected item or an unresolved evidence reference MUST reject the candidate. Pending work and approvals may remain pending when their state is preserved. Generated summaries MUST remain labeled evidence and MUST NOT become higher-authority instructions.
- **FR-013**: Unsupported or omitted content MUST be explicitly represented as unavailable or partial. A summary MUST NOT claim source completeness, verified truth, or successful completion solely because an excerpt matches its source.
- **FR-014**: Auxiliary model work MUST use an owner-authorized provider and content route with the same privacy, outbound-content, budget, and audit constraints as the originating conversation. Missing authorization MUST prevent the call; automatic cross-owner or system-provider fallback is prohibited.
- **FR-015**: Every attempt, acceptance, rejection, recall, denial, expiry, and cleanup MUST be auditable with the acting principal, human authorizer where delegated, operation, bounded source identities, policy outcome, and integrity relationship. Diagnostic and accounting records MUST exclude raw sensitive evidence and secrets.
- **FR-016**: Conversation accounting MUST include direct and auxiliary model calls, reported input/output and cache usage, retries, charged failed/cancelled work, recall traffic, and dated pricing where available. Missing usage or prices MUST be labeled, and duplicate delivery MUST NOT double-count a charge.
- **FR-017**: Users MUST be able to distinguish full source, partial preview, generated summary, missing source, and blocked recall on every supported client. Source inspection MUST use the same server-authorized capability and existing approved presentation vocabulary, including a labeled supported fallback where needed.
- **FR-018**: If protected history cannot fit within the authorized context budget, the system MUST produce an explicit recoverable limit and retain the history. It MUST NOT silently lower privacy, switch providers, drop protected material, or retry without a configured bound.
- **FR-019**: Observation packing and the new compaction behavior MUST be independently controllable and disabled by default. With both off, existing configured conversation behavior remains unchanged and this feature creates no new capture or auxiliary call. Packing alone permits capture, preview, and recall without new automatic compaction; compaction alone performs safe replacement without new observation capture or packing; both on permit both mechanisms. An enabled new mechanism MUST govern its affected work instead of applying a legacy lossy transform to the same material. Disabling packing MUST stop new capture and shortening while existing references remain recallable subject to current authorization, revocation, deletion, and absolute expiry; disabling new compaction MUST stop its auxiliary calls and replacements. No combination may change authentication, tool authority, approval state, or authoritative source records.
- **FR-020**: Evaluation MUST compare each mechanism and the combination against a paired baseline using fixed source snapshots, provider routes, scoring rules, complete-cost definitions, and separate development/held-out workflows. Held-out failures MUST be recorded and cannot justify tuning or redefining success on that same holdout.
- **FR-021**: Promotion MUST require passing deterministic capability, owner-isolation, revocation, privacy, confirmation, evidence-integrity, failure-preservation, retention, concurrency, and accounting checks, followed by the product's ordinary qualification and affected-client verification. Research savings alone MUST NOT authorize a production default.

### Key Entities *(include if feature involves data)*

- **Authorized observation**: A permitted captured textual result tied to its owner, conversation, audience, source operation, integrity identity, and retention lifecycle.
- **Working view**: A full observation, labeled partial preview, or identified generated summary supplied for the current conversation; it carries source references and actual operation state without adding authority.
- **Recall page**: A bounded, ordered, integrity-checked slice of an authorized observation with a location and continuation or end state.
- **Compaction candidate**: A proposed replacement bound to a specific conversation state and source set, with protected material, validation outcome, and acceptance or rejection state.
- **Protected material**: Verbatim governing instructions and current user turn; authoritative pending-action/work/parameter/source/permission records; and the actual outcome, complete relationship, and exact evidence for each captured error, denial, or incomplete operation. Its pre-attempt snapshot supplies the deterministic preservation oracle.
- **Usage record**: An owner-bound entry for a direct or auxiliary operation, its reported or unknown usage, pricing date, charged outcome, and deduplication identity.
- **Evaluation candidate**: A frozen mechanism selection and product revision evaluated against predefined development and held-out workflows, with quality, boundary, usability, and complete-cost results.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Every retained synthetic observation in the deterministic acceptance set can be reconstructed exactly from authorized pages; beginning, middle, and ending facts are recoverable without repeating the originating operation.
- **SC-002**: All cross-owner, revoked-access, prohibited-provider, sensitive-content, forged-reference, and confirmation-bypass cases are denied with zero unauthorized disclosure or action.
- **SC-003**: Every injected compaction failure, cancellation, stale completion, and rejected replacement preserves the previous history and pending approvals; every accepted replacement preserves protected material and reduces the working-context size.
- **SC-004**: All supported clients provide the same authorized source-inspection capability and distinguish preview, summary, unavailable source, and denial states in live acceptance; no client silently omits an affected capability.
- **SC-005**: All synthetic known charges, retries, and auxiliary operations reconcile exactly once with conversation totals. Every missing-usage or missing-price case is visibly labeled and excluded from a claimed verified saving.
- **SC-006**: A frozen candidate passes every deterministic task oracle and has no lost accepted task or critical-evidence regression against the paired held-out baseline. Results remain task-specific evidence, not a clinical or population-wide claim.
- **SC-007**: To recommend promotion on the selected long-output workload, complete cost per accepted task is at least 10% lower than its paired baseline, including all auxiliary work and charged failures, while SC-002, SC-003, SC-005, and SC-006 remain satisfied. A candidate that misses this target remains a documented experiment.
- **SC-008**: In a formative synthetic-source exercise, at least four of five representative users can distinguish a preview from its captured source and recover a specified omitted fact without assistance. Corrections, repeated retrievals, and clarification turns are reported alongside completion, without claiming population-wide usability improvement.
- **SC-009**: Deterministic retention, deletion, restart, concurrent-update, and capacity-limit scenarios all honor the approved limits without silent evidence eviction, resurrected content, partial replacements, or duplicate charges.

## Assumptions

- This request authorizes specification of the first bounded adoption candidate. Implementation, provider selection, deployment, release, and default promotion require their subsequent governed phases and evidence.
- The initial scope is textual tool observations, exact bounded recall, safe compaction, shared presentation, complete accounting, and comparative evaluation. Installing the Pi extension, general recursive execution, action fusion, learned context policies, and another-model evidence reducers are outside this feature.
- Existing identity, delegation, authorization, privacy, outbound-content, confirmation, audit, governed persistence, and shared presentation remain authoritative. A future implementation that needs a durable contract or shared presentation change must qualify the owning component before adoption.
- The proposed initial safety limits are 16 KiB of permitted text per recall page, 8 MiB per observation, and 64 MiB of retained observation text per conversation. Operational policy may lower these limits; increasing them needs a reviewed capacity and failure assessment. Required redaction takes precedence over exactness relative to an unrestricted source.
- Retention requires explicit permission under existing policy and inherits every stricter source and conversation deadline, with a maximum of 24 hours from capture even when a source has no shorter stated lifetime. This feature establishes no new retention entitlement, cross-conversation search, or cross-owner evidence sharing. An unavailable source remains unavailable after restart; absolute expiry cannot slide with access.
- Initial comparative evaluation uses 24 synthetic non-clinical-data workflows, partitioned in advance into 12 development and 12 held-out tasks without near-duplicate variants crossing the partition. Provider routes and repetitions are declared before evaluation; deterministic regression gates use fixed responses. The small sample supports a bounded adoption decision rather than a generalization claim.
- The 10% complete-cost target and five-user formative exercise are AstralDeep acceptance goals, not findings from SoL-Pi. Price/usage gaps prevent a verified complete-cost claim. Real-provider measurements are recorded diagnostics rather than live-network or shared-runner timing gates.

## Research Basis and Decision Boundaries

- **Haozhe Liu et al., _SoL-Pi: Recursively Scaling Auto-Research Loops for Efficient Agent Harness_, arXiv:2609.20519v1, 2026 preprint**, submitted September 17; retrieved October 5, 2026. Its four mechanisms include observation packing and online compaction. ObservationPack alone is a promising standalone result, while the complete stack has lower point-estimate scores on the reported evaluations. The paper does not establish clinical usability or AstralDeep security. Choosing exact recall and safe compaction first is an engineering inference. [Versioned paper](https://arxiv.org/html/2609.20519v1), [prior evidence review](../research/2026-10-04-sol-pi-review.md).
- **Xiaochuan Li et al., _ACM: Agentic Context Management for Long Horizon Tasks_, arXiv:2607.23809v1, 2026 preprint**, July 26; version status rechecked October 5, 2026. It supports retaining discarded context for later access; retained storage does not by itself prove correct recall or owner isolation. [Versioned paper](https://arxiv.org/html/2607.23809v1).
- **Alex L. Zhang, Tim Kraska, and Omar Khattab, _Recursive Language Models_, arXiv:2512.24601v3, revised May 11, 2026 preprint version consulted**; status rechecked October 5, 2026. Recursive programmatic context access is a future alternative; its wider execution and spend surface is unnecessary for the initial bounded feature. [Versioned paper](https://arxiv.org/html/2512.24601v3).
- **Rui Ye et al., _AgentFold: Long-Horizon Web Agents with Proactive Context Management_, arXiv:2510.24699, 2025 preprint version consulted**, retrieved October 4, 2026; venue acceptance was not verified. Learned multiscale folding remains a later option because training and lossy context changes expand this feature's evaluation scope. [Preprint](https://arxiv.org/abs/2510.24699).
- The product baseline is merged main `e9a47d8b826dc506dd509b6110a5199ad1cddc42`; its context paths match the inspected baseline in the prior evidence review. This specification addresses missing exact-recall references, destructive compaction failure behavior, and auxiliary owner/accounting seams identified there. It does not claim an observed production incident or measured AstralDeep improvement.
- The existing deterministic authorization and bounded evidence approach remains preferable for the first feature because the cited papers do not qualify the product's ownership, privacy, revocation, or approval requirements. Governing policy is [the AstralDeep constitution](../../.specify/memory/constitution.md), version 6.1.0.
