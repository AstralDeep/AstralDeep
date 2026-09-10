# Data model: integrated durable work

Status: design for feature 088; proposed additions below are not implemented contracts.
Baselines: Deep `2de5d867`, Plane `b20c8f3e` / schema `079.001`, rewrite `daeea32`.
The standalone rewrite is a read-only source. No rewrite database, account, token,
schedule, approval, audit chain or encryption key is imported.

## 1. Storage ownership and reuse

Use the existing application-scoped `PlaneRuntime` and `RepositoryCatalog` from
`backend/orchestrator/plane_composition.py`. Deep uses `AssignmentStore.transaction`
and `WorkAdmissionCoordinator` rather than SQL/driver calls. Plane owns SQL,
transactions, migrations, storage records and compare-and-set mechanics. Existing
stdlib, Pydantic, cryptography and Plane's `psycopg2-binary` suffice; the donor
SQLAlchemy engine, psycopg3 driver, schema registry and ORM models are not dependencies.

| Existing authority | Existing types / fields / methods | 088 use |
| --- | --- | --- |
| Work admission | `OperationRequest`, `OperationOwner`, `AdmissionClass`, `ExecutionFence`; `submit`, `claim_operation`, `assert_current_execution`, `renew_execution_lease`, `terminalize`, `cancel` | Capacity and physical execution fences for every episode. Preserve interactive, voice, MCP, scheduled, maintenance and system capacity classes. |
| Persistent work | `AssignmentDefinition`, `AssignmentRecord`, `AssignmentFence`, `AssignmentOperationBinding`; `create_assignment`, `apply_control`, `bind_operation`, `finish_episode`, `recover_expired_for_administration` | The logical durable task and its reconstructable controller. Extend this implementation for one-shot work; do not create a second donor operation lifecycle. |
| Actions and budgets | `AssignmentActionIntent`, `AssignmentResourceAmount`, `AssignmentActionReservation`, `AssignmentDispatchPermit`, `AssignmentActionOutcome`; `put_action`, `reserve_action`, `start_action`, `record_action_outcome`, `release_unstarted_action` | One reservation/effect ledger for tools, models and publication. Existing spent/daily/outstanding buckets remain authoritative. |
| Dependencies and observations | `AssignmentTask`, `AssignmentTaskClaim`, `AssignmentTaskResult`, `AssignmentSourceBatch`, `AssignmentSourceEvent`; `put_task_plan`, `claim_task`, `complete_task`, `record_source_batch` | Bounded child work, source identity/cursor CAS and retained evidence provenance. |
| Review | `AssignmentActionDecision`, `AssignmentActionReconciliation`; `link_interactive_proposal`, `decide_action`, `claim_for_approved_action`, `reconcile_action` | Existing exact owner review and uncertain-effect handling. |
| Scheduling | `ScheduledJob`, `ScheduledOccurrence`, `JobRunRecord`; `materialize_and_claim_due_for_administration`, `attach_operation_to_claim`, `start_claim_attempt`, `finish_claim_attempt` | Preserve cron/interval/one-shot, timezone and occurrence identity. Add donor episode/run-limit semantics here. |
| Authored agents | `UserAgentRecord`, `AgentRevisionRecord`; `agents.create_agent`, `create_revision`, `compare_and_set_agent`, `transition_revision` | Existing ownership/revision lifecycle, extended with declarative metadata; existing host/runtime trust is unchanged. |
| Identity, secrets, preferences | `identity`, `credentials`, `offline_grants`, `encrypted_llm_config`, `preferences.personalization`, `personalization_graph` | Existing Keycloak subjects and service facades remain authoritative. |

Sources: `components/AstralPlane/src/astralplane/repositories/{assignment_models,assignments,work_admission,scheduler,agents,preferences,secrets,offline_grants}.py`;
`backend/persistent_agents/{store,service,execution,runner}.py`.

## 2. Logical task versus physical admission

For newly adopted durable operation APIs, `TaskRead.id` is the existing assignment UUID4.
A claim's work-admission UUID is a separate attempt/capacity identity; it is never
reused as the logical task ID. A schedule occurrence references the logical task and
its existing physical admission binding. Existing chats/messages, work-admission
rows, persistent assignments and old native IDs are not rewritten into this shape.
Ordinary existing chat stays on its current authenticated dispatch during the first
UI milestone. Its existing IDs and idempotency remain authoritative. A read adapter
can show those conversations beside enhanced durable work without claiming that
old chat gained assignment checkpoints, pause/resume or durable recovery. Only the
qualified enhanced operation path creates one-shot assignments; routing ordinary
chat through it is a later explicit cutover with equivalent legacy behavior tests.

`AssignmentRecord` already contains `instruction_revision`, `control_epoch`,
`state_version`, `lifecycle`, `phase`, `next_wake_at`, `wake_reason`,
`wake_generation`, `checkpoint`, `tasks`, `usage`, timestamps and safe error code.
Its `AssignmentFence` also carries claim generation/token. Preserve these separate
meanings; a public revision never substitutes for a worker fence.

Proposed additive assignment execution profiles:

- `persistent` is the default for every existing row and API. Its present source,
  allowed-tools, offline-grant, daily/lifetime-budget and retention rules remain.
- `one_shot` is the profile for an explicitly submitted chat/research task. It
  permits a source-less model turn and an empty external tool set, but requires
  nonempty model/task intent and an exact current authority reference. An
  unattended or scheduled one-shot additionally requires its existing
  offline authority. The profile cannot be selected by a serialized authority claim.

The current repository unconditionally requires a source, tools and a valid
`offline_grant_id` (`assignments.py:196`, `:461`). A profile-aware constructor and
validator are therefore real new work. Do not synthesize a source/tool/grant to
make ordinary Send pass the persistent validator. Add `create_operation` as a
profile-specific public constructor sharing the current assignment initializer,
action ledger and fencing. Legacy `create_assignment` continues to create only
the persistent profile. Profile-specific capacity accounting prevents ordinary
task history from exhausting the existing 25 active / 256 retained assignments.
One-shot history uses explicit bounded pagination/retention; it must not silently
delete unresolved actions or consume the persistent-assignment allowance.

Store `execution_profile` as an indexed discriminator on `persistent_assignment`
with default `persistent`. Add a versioned `operation` member to its existing
private `data` JSON, only for one-shot rows:

| Field | Meaning / invariant |
| --- | --- |
| `version`, `kind`, `title`, `accepted_at`, `deadline_at` | Version 1; kind `chat` or `research`; immutable accepted intent and absolute deadline. Existing broader Deep tool kinds remain available. |
| `origin`, `authority_reference` | Bounded server-produced origin and references to existing owner/session/credential/parent lineage; no bearer, role claim, secret or decrypted provider key. |
| `agent_reference` | Exact existing agent ID, revision and definition digest. No new agent identity or bundled trust promotion. |
| `guidance` | Optional selected skill/note references, expansion version and keyed combined binding. No selected note value or expanded model prompt. Absence means no new explicit selection, not removal of legacy personalization. |
| `source_plan`, `source_retention` | Ordered reviewed sources, extraction profile and `operation`/`none`; bounded URLs and reader identities. Source text is not hidden in this member. |
| `limits` | Per-operation model/tool/token ceilings and absolute deadline; reserved, spent and uncertain consumption never reset on claim, retry or control. |
| `schedule_reference` | Optional existing job/occurrence/run identity and parent grant allowance binding; no second recurrence clock. |
| `control` | Optional versioned event wait key/watermark and retained resume disposition; owner pause is still assignment lifecycle/control epoch. |
| `retry` | Attempt index, configured bounded backoff, next due time and classified reason; unknown/unsafe effects remain held. |
| `result_reference`, `terminal_outcome` | Reference to canonical result/approved artifact; explicit completed/failed/cancelled disposition when the controller is terminal. Never infer logical success from a released capacity slot. |

Keep the complete private assignment JSON within the current 256 KiB bound and
checkpoint within 64 KiB. Large reviewed artifact/source bytes use existing Plane
blob/workspace storage with owner, length and digest; metadata remains bounded.

Existing assignment creation receipts use a UUID4 `submission_id`. New external
operation keys are namespaced bounded strings, so add the Plane-owned receipt
relation `assignment_operation_receipt(owner_id, origin_namespace, caller_key,
command_digest, credential_id, assignment_id, created_at)`, with unique
`(owner_id, origin_namespace, caller_key)` and owner-consistent foreign references.
Store the original key; a hash-derived UUID is not the uniqueness authority.
Credential identity binds framework replay without changing the caller key's
namespace. Create this receipt, assignment and any allowance debit atomically.
Receipt retention must cover the documented retry window and retained task; deleting
a task must not silently make a live retry key admit a new effect. Existing UUID4
assignment submission receipts retain their original behavior.

`TaskRead.revision` is the current `state_version` snapshot (a strict integer),
while writes also bind the observed instruction revision/control epoch on the
server. Events use `AssignmentActivityRecord.sequence`; heartbeat-only updates
may advance state_version without a new user event. Consumers must not decrement
either watermark. Read projection reports the vector used; it does not mint a
new independent revision counter.

| Public task disposition | Existing authority plus explicit 088 metadata |
| --- | --- |
| queued / active | Active assignment awaiting admission / current claimed assignment and execution fence. |
| awaiting_approval / awaiting_authority | `waiting_approval` / `waiting_authorization`. |
| budget_blocked / reconciliation_required | `budget_exhausted` / `reconciliation`. |
| paused | Assignment lifecycle `paused`, with retained control semantics. |
| awaiting_event | Active controller with a registered event wait and no due wake; distinct from cadence. |
| retry_eligible | Nonterminal failed phase with an explicitly admitted retry deadline. |
| completed / failed / cancelled | Terminal controller plus explicit one-shot outcome; `stopped` maps to cancellation. |

Unknown profile/checkpoint versions stay intact, readable and non-dispatchable.
Old assignment APIs filter/handle the new profile explicitly; an older worker must
not claim a source-less one-shot using its persistent episode algorithm.

## 3. Actions, result bytes and transient input

Reuse the current action identity, request/permission/precondition digests,
maximum amount, attempts, dispatch token and result ledger. Add optional versioned
payload disposition to `AssignmentActionIntent` and outcome serialization:

- `durable` retains current behavior for existing tasks and exact consequential
  proposals.
- `transient_input` stores a bounded reconstruction envelope, keyed payload binding,
  source/guidance revision references and retention profile. The actual model
  messages exist only during the call and pass normal dispatch validation. Current
  `ActionExecutor.action` persists full `request.messages`; it must use this new
  disposition when explicit notes or non-retained source text are present.
- A successful read whose bytes were deliberately discarded keeps its factual
  receipt, usage and `result_available=false` plus a typed reacquisition reason.
  This is not a failed read and cannot be used as available evidence. A subsequent
  read gets a new budgeted action/attempt identity; its content may have changed.

No note plaintext, expanded private guidance, discarded source text or private
input content hash enters activity, audit, diagnostic labels or historical model
requests. Source fingerprints required for monitoring are permitted only under
the declared public-source retention policy. Outcome settlement may retain a late
fact/charge from an authentic issued permit; it cannot advance the task, create a
new proposal or publish after the current authority/fence was invalidated.

Store exact reviewed result bytes with existing workspace/artifact publication.
Bind publication to owner, task, action, request digest, content digest, destination,
instruction revision/control epoch, expiry and one-time decision. No parallel
donor `artifacts` or `approvals` table is needed.

## 4. Schedule/observation additions

The existing `scheduled_jobs`/occurrence/run repositories remain the recurrence
authority. Add one optional, versioned job policy record through `SchedulerRepository`:
`max_runs`, `admitted_runs`, `per_episode_limits`, `max_outstanding_episodes=1`,
`monitor_changes`, `definition_revision`, `terminal_stop`, and last logical task
reference. Absence preserves existing job semantics; it does not impose new
run limits on old jobs. A durable occurrence-to-assignment binding is unique by
occurrence and owner. Admission of a new episode, run/grant spend and binding commit
together; an outstanding episode consumes nothing more.

Monitor cursor/fingerprints, context digest, complete-source set, normalized final
URLs, prior result binding and observation sequence live in the existing assignment
source/checkpoint contract or its typed scheduler observation extension for
successive one-shot episodes. The last complete observation survives episode
deletion only according to the declared policy; missing prior result bytes yield
insufficient evidence, never invented semantic equality. Initial extractive
passages and unchanged-source detection remain distinct from changed-source model
assessment.

Pause suspends future triggers only for the donor-style job policy. Stop atomically
sets terminal stop, invalidates future claims and stops all outstanding episode
families without erasing history/charges. This does not change the established
meaning of pausing an individual persistent assignment. Delete requires no
outstanding/unresolved episode and preserves completed artifacts.

## 5. Existing guidance and new explicit-note records

Authored agent revisions already exist in `agents`; add optional declarative
definition/policy metadata and keep existing executable/remote agent paths.
Clone creates an ordinary new owned draft; activation uses existing validation
and trust semantics. Historical definitions must not be fabricated.

Owner skills currently live in owner-hashed Markdown under
`backend/orchestrator/user_skills.py`. Introduce a Plane-owned current-head/revision
catalog behind a single Deep skill facade: stable owner/skill identity, revision,
format version, definition digest, applicability, alias, enabled/deleted state,
timestamps and immutable definition snapshots. Existing skill packs and induced
recipes are separate, preserved sources. Materialize only existing Deep owner
skills on controlled first use/cutover, retaining their exact legacy format and
valid behavior; do not pretend unavailable prior revisions existed. Preserve the
original files as recovery evidence. Route every old/new mutable user-skill entry
point through the facade before making the catalog authoritative; no concurrent
file/SQL authorities. Conflicting file edits or alias collisions produce a bounded
conflict report, not silent normalization or lost instructions.

The existing `preferences.personalization.MemoryRecord` contains plaintext value,
salience, supersession, validity, recall and project fields. Its automatic/graph
memory is not replaced. Add an `ExplicitNoteRecord` to that same repository facade
for the incompatible encrypted-current-value/Forget semantics: owner, UUID4,
revision, format, category, ciphertext, enabled, created/updated/expires timestamps,
deleted timestamp/reason. The new `owner_guidance_note` table has no value-history
table. Cryptography stays in Deep's existing secrets boundary; Plane receives
opaque ciphertext and validated metadata. Encryption binds owner/ID/revision and
metadata. Correct/enable/disable re-encrypt the new bound value; Forget/expiry erase
ciphertext and unnecessary metadata while retaining the minimum tombstone.

Selection references are at most 20 skills / 8 explicit notes, carrying current
revision and opaque binding; combined expansion is bounded by the selected
provider input allowance. Same-transaction head checks plus a shared owner lock
linearize edit/Forget against dispatch. A note/skill mutation invalidates pending
references/proposals through an indexed reference relation
`assignment_guidance_reference(assignment_id, owner_id, kind, resource_id, revision)`.
This index carries no private text and excludes terminal rows from active scans.

Framework credentials and bounded unattended allowance extend existing credential
and offline-grant facades. Issuer reference, closed scopes, hash-only token record,
expiry/revocation, maximum and consumed admissions are authoritative fields; a
credential is never itself unattended consent. No rewrite session table is adopted.

## 6. Migration and required public contract additions

`088.001` is a proposed Plane revision, not an allocated or qualified schema ID.
Resolve component ownership and inventory local/remote migration IDs before reserving
it. The frozen baseline's expected predecessor is `079.001`; if the authorized
Plane baseline advances, reconcile its guarded registry and populated-data upgrade
path before selecting the actual predecessor. Record the resulting exact migration
digest. Deep's pin changes only after that exact Plane candidate passes. Changes
are additive:

1. Assignment profile discriminator/index, version-aware read/write/claim support,
   one-shot definition and transient input metadata; namespaced operation admission
   receipts, bounded wake receipts and the private guidance reference index.
   Existing row JSON and digests are preserved.
2. Optional scheduler policy, occurrence/assignment relationship and observation
   metadata, plus finite grant-admission usage where existing grants lack it.
3. User-skill heads/immutable revisions and explicit encrypted notes behind existing
   composition facades; optional agent metadata and scoped framework credentials.

New/extended public methods must be covered by Plane contract fixtures: profile-aware
`create_operation`, `claim_due_for_administration`, `set_event_wait`, `accept_wake`,
one-shot result/outcome projection and retention; transient-payload-aware action
methods; `SchedulerRepository.admit_assignment_episode` and `stop_assignment_job`;
skill revision/head CAS; explicit-note CAS/purge and indexed guidance invalidation;
credential issue/revoke/allowance CAS. These names are proposed contracts, not
claims that baseline Plane implements them. Reuse private lifecycle helpers rather
than implementing a second state machine in any of these adapters.

Verify empty and populated `079.001` upgrades, repeated upgrade, concurrent CAS,
owner isolation, unchanged legacy row digests and retained unresolved effects.
No destructive down migration. Recovery restores the matched application/Plane
pin/database backup or uses a documented forward repair. A `079.001` binary must
not be asserted compatible with newly created profiles/ciphertexts. No test or
migration accesses the standalone rewrite database/services.
