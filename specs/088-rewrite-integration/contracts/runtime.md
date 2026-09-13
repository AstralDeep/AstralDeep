# Integrated runtime contract

Status: proposed feature 088 contract; implementation begins only after plan/analyze.
Companion entity definitions are in `../data-model.md`. No new runtime dependency,
second database engine, standalone IAM or alternate dispatcher is introduced.

## 1. Composition and dependency direction

`authenticated transport -> Deep operation service -> AssignmentService/Runner +
WorkAdmissionCoordinator -> ordinary governed dispatcher -> existing agent/provider`

This is the enhanced durable-operation composition. Existing ordinary chat keeps
its current authenticated dispatch, admission and response path during the first
UI milestone. The new shell adapts that path immediately; it does not require an
assignment conversion or offline grant to send ordinary chat. Enhanced durable
research/tasks use one-shot assignments only after their profile is qualified.
The service/read adapters label capabilities and preserve each path's existing
identity, so they cannot present unsupported durable controls for old chat.

Plane's `RepositoryCatalog.assignments`, `work_admission`, `scheduler`, `agents`,
`credentials`, `offline_grants`, `preferences`, `encrypted_llm_config` and existing
artifact/workspace repositories own durable mechanics. `AssignmentStore` supplies
bounded async transaction calls through `AsyncPlaneRuntime`. The orchestrator's
one initialized `PlaneRuntime` supplies all repositories. Reuse the bounded
`AssignmentRunner._active` supervision/lease loop; do not start donor `Worker` or
`StepExecutor` alongside it. Registered one-shot episode handlers implement chat
and source research while existing persistent episode handlers keep their behavior.

Keep Python 3.11, existing Pydantic/cryptography and Plane's existing psycopg2 stack.
Donor pure validation/serialization/accounting algorithms may be adapted under
these interfaces. Donor `astral.storage`, SQLAlchemy, psycopg3, SessionService and
its app factory are not imported. HTTP read/model work uses the existing governed
tool path and `backend/shared/external_http.py`; adopted deadline/body/process
cleanup improvements must strengthen that path, not create a policy bypass.

## 2. API and authority

Expose a versioned Deep-owned operation router (proposed base
`/api/work/v1/operations`) to avoid replacing existing operation/status endpoints.
The SDK/MCP/A2A compatibility facades bind to this same service; response envelope
and base URL may change through generated versioned contracts. Old authenticated
REST/WebSocket/native routes remain. Resource IDs are integrated UUID4s; no donor
identifier/import alias is needed.

Commands: submit, cancel, pause, resume, wait, wake, decide, reconcile, delete.
Queries: list/detail, events/poll/SSE, physical attempts, result/artifact and
measurements. Agent/skill/note/grant/schedule/provider adapters call their existing
owner services and new declared Plane extensions. Ordinary submit runs once on
Send. A pure preview/explanation endpoint may support optional details; its receipt
is neither required for ordinary Send nor an approval token.

Transport constructs context only from the current institutional Keycloak principal
and existing session/delegation machinery. Immutable refs identify owner, actor,
credential/session, agent revision, original origin, parent, job and grant where
applicable. An API body cannot assert these as trusted authority. Cookie writes
retain current CSRF/origin checks. External frameworks use explicitly issued scopes
and ordinary attenuated delegation; framework read/control scope never includes
approve/reject, delete/reconcile or settings authority.

Submission is idempotent by `(owner, origin namespace, caller key)` and canonical
command digest. Framework keys are additionally bound to the exact issuing
credential. The new Plane operation constructor stores the original bounded key
and digest in `assignment_operation_receipt`; it does not replace the key with an
unchecked hash. Matching
retry returns the original logical task; mismatched reuse is conflict. The receipt
lookup precedes re-expansion of guidance so an already accepted retry cannot create
a new task from subsequently changed skill content. Current authentication is still
required to retrieve the receipt.

Every command binds the observed `state_version` and resolves its current
instruction revision/control epoch under lock. Wake also binds event ID, key and
source revision: same ID/content acknowledges duplicate; mismatched ID/content or
old source watermark refuses. Persist bounded receipts in Plane, not client memory.
Unknown state/control/checkpoint versions allow inspection/cancel where safe and
refuse continuation. Unauthorized resource and absent resource both return 404.

## 3. Transaction boundaries

Remote IAM/provider/network calls never hold a Plane transaction open. A remote
authority observation is a bounded short-lived snapshot, checked against current
local lineage after acquiring locks. Time is re-sampled after lock waits using
Plane database time. Missing/inconclusive remote or local verification refuses
new work. Authorization checks are not satisfied by an owner ID alone.

| Boundary | Required atomic state |
| --- | --- |
| Accept | Lock owner/credential authority in declared order; verify current authentication and the receipt's owner/origin/issuing-credential namespace, then resolve the original receipt and compare the canonical command digest. Return a matching previously accepted task without re-expanding guidance, revalidating its agent/source selections or consuming admission allowance again; mismatched reuse is conflict. Only a genuinely new admission checks current submit scope, grant, agent/guidance revisions, source policy and resource bounds, creates the task/optional occurrence binding, consumes finite allowance and appends required audit before commit. No model/tool call occurs in this transaction. |
| Claim | Claim the same AssignmentRepository profile; admit/claim existing work-admission capacity and bind `AssignmentOperationBinding`. No action can start with only one of the two current fences. Failed admission yields a durable bounded retry/hold without dispatch. |
| Prepare action | Reconstruct actual input in memory, recheck PHI/taint/egress/guidance/provider bound. Use existing `put_action` and `reserve_action`; reserve maximum input+output/model/tool/time amount. Audit and reservation must be durable before dispatch. |
| Issue permit | Refresh remote/current authority without locks; in one transaction verify local issuer/revocation/expiry, selected revisions, both fences, exact request/permission/precondition binding and available reservation; `start_action` creates the dispatch permit. This commit is the effect dispatch linearization point. |
| Observe/settle | Record an authentic issued permit's success/failure/uncertain charge through `record_action_outcome`. Recheck current authority and both fences before incorporating source/model output or advancing the checkpoint. A stale result may establish consumption only, never new executable state or publication. |
| Publish | Existing interactive proposal decision and publication path rechecks exact content/destination/expiry/current permissions and one-time receipt under the same fenced transaction as the publication marker. Blob staging is recoverable; only the committed existing publication pointer makes a result visible. |

All adapters use one common lock order: owner/credential/grant authority, relevant
job/occurrence when admitting or stopping a scheduled episode, logical assignment,
work-admission fence, action/resource heads in stable ID order, then audit/outbox.
The implementation must reconcile this ordering with existing Plane/Deep lock
acquisition before changing calls; contradictory existing orders require a shared
helper change and a PostgreSQL regression, not a second ad hoc transaction path.
No callback passed to Plane may perform external I/O or acquire another pool.

Current issuance fix: checking an issuer before acquiring an owner lock is
insufficient. `issue_framework_credential` must lock/reload the existing issuer
lineage, owner retirement/revocation state and current time immediately before
inserting the token hash/allowance record and audit. A token generated in memory
before that point is not returned on refusal/rollback. Only the successful response
may show the token once. Revocation and issuance serialize on the same lock domain.

## 4. Resource accounting and recovery

The current assignment budget buckets `spent`, `daily` and `outstanding` stay the
source of truth; child work shares the parent ledger. A one-shot limit also caps
its complete task lifetime, across physical work-admission claims. Scheduled
episode limits are not a fresh allowance while an episode is paused or in review;
only a newly admitted occurrence receives its separately charged episode allocation.

`AssignmentResourceAmount` already covers model/tool calls, tokens, elapsed time
and optional quoted currency. Adopt donor charge-basis annotations (observed,
estimated, uncertain, none) without a duplicate counter. A missing usage counter
does not become zero; an unknown price does not become free. Confirmed provider
overruns remain charged and block later work. Releases require proof no dispatch
permit/effect was used; timeout/restart does not refund uncertainty. If a provider
can prove definite nonconsumption after request transport, record a typed settlement
under that provider contract rather than misusing `release_unstarted_action`.

Existing provider breadth/user-versus-system credential resolution is preserved.
The donor local Ollama profile is an optional reviewed provider adapter, with exact
input framing, model/template identity and output ceiling. Unsupported accounting
profiles cannot advertise strict reserved-token guarantees for a bounded task;
their operation returns a truthful unsupported-bound disposition. Existing working
provider workflows continue under their existing declared policy, without silent
model substitution or weaker bounds. Common provider support can expand after
specific framing/output/accounting profiles are qualified.

One-shot transient retries use the declared finite backoff (donor profile
5/15/45 seconds, maximum three) within the original deadline and remaining budget.
Legacy persistent retries keep their selected policy. Unknown side-effect outcome
requires reconciliation or an existing proven idempotent/read-only replay boundary.
One-shot retry cannot call the old persistent recurrence algorithm or create a new
logical task. Crash recovery fences prior assignments/actions before any reclaim.

### Ephemeral source recovery regression

The implemented `POST /api/work/v1/operations` public-reader request accepts the
optional closed `source_retention` field (`operation` or `none`). Omission retains
the previous `operation` behavior and exact previous canonical receipt digest.
An explicitly supplied field participates in command identity: a retry must use
its original body. `none` selects the same governed ephemeral reader/model path,
without changing IAM, budgets or execution authority. Its result endpoint returns
metadata with unavailable content; it cannot reconstruct a discarded result for
later viewing or saving. Invalid types/values or extra payload fields refuse
before refresh or admission. This transport option does not itself implement a
new client composition control, automatic provider retry or retention migration.

When `source_retention=none`, source title/URL/extraction metadata and receipt may
survive; full text exists only in the current attempt. Do not persist an `available`
flag that a new attempt interprets as retained content. A checkpoint carries:
source identity, observation generation, completeness, receipt/result reference,
and `text_available=false` with a bounded reacquisition plan.

After transient model failure, authority hold, restart or lease loss, the next
attempt re-fetches every source needed by its model input through the ordinary
reader and creates new charged read actions. It discards stale semantic baselines
if bytes/final URLs/completeness changed. If remaining budget cannot reacquire,
report budget-blocked; if a fresh fetch fails, report source-unavailable. Previously
discarded successful evidence alone cannot produce source-unavailable. Do not reuse
the old successful `ActionExecutor` result key: a new observation/action identity
is required while the old receipt/charge remains.

## 5. Fair scheduling and monitoring

Existing recurrence types, timezone calculation, occurrence uniqueness and run
history stay in `SchedulerRepository`. Donor-style jobs attach optional episode
policy. In one transaction, `admit_assignment_episode` verifies the current job,
source/configuration revision, no outstanding episode family, both finite
allowances and current offline authority, then creates the task/binding and advances
admitted-run state. A held episode does not consume another occurrence allowance.

Claim eligible work before applying the bounded dispatch limit. The current
assignment query already filters phases to waiting/failed and uses
`FOR UPDATE SKIP LOCKED`; retain that property. Add bounded owner rotation and
stable `(due_at, id)` continuation for jobs deferred by authority/episode holds.
Advance the scan position after every examined candidate, including refusal; never
change the job's semantic due time merely to hide starvation. The query and
continuation must wrap safely and be bounded in work/memory. Ten oldest held jobs
must not permanently exclude a later eligible job belonging to another owner.

For a finite donor-style job, Pause changes future triggers only. Stop invalidates
job/occurrence claims, stops active logical episode families and consumes/invalidates
their unspent approval authority; issued effects retain uncertain charges. The final
admitted run can still be stopped while outstanding. Delete refuses unresolved
episodes and never deletes completed artifacts as an incidental action.

Use existing source-batch CAS to bind initial/changed observations. Initial output
selects complete attributed passages, with no history claim. Complete identical
normalized extractions/final URLs may skip generation. Changed or incomplete sources
cannot inherit that conclusion. Comparative generation receives a bounded exact
prior result binding and current sources, and passes the same grounding/content
checks as ordinary research. Guidance is instructions, not cited evidence.

## 6. Guidance and revision checks

All old/new user-skill writes converge on one owner-scoped facade before revisioned
catalog cutover. Existing automatic skill applicability and personalization remain;
the explicit-selection mode adds exact head bindings without disabling old tools.
An ordinary Send may capture/apply current default skills deterministically without
a separate review dialog. If the user explicitly selected a particular revision,
that revision must still match; do not silently replace it during submission.

Resolve at most the selected bounded notes/skills, normalize and expand with one
versioned function, check total input size, then store only references and a keyed
binding on the task. Resolve them again at prepare, permit, heartbeat and result/
publication commit. Correct/disable/delete/Forget/expiry atomically changes the
current head and invalidates unfinished bound tasks/proposals. It never authorizes
an old expanded prompt from a checkpoint. Already dispatched usage stays charged.

Explicit note correction/expiry removes previous ciphertext and metadata according
to `ExplicitNoteRecord`; it does not archive a plaintext value in general memory,
source receipts or action messages. Existing automatic memory has its existing
retention rules and clear separate product semantics. Bounded purge scans skip
locked owners and rotate, report aggregate failures, and never prevent unrelated
work from being considered.

## 7. Results, controls and read contracts

Canonical result contains bounded answer, source provenance/completeness, disposition,
optional validated reading guide and optional saved-artifact reference. Result
generation itself does not approve saving/publication. Proposal review shows the
exact bytes/destination/scope and needs the current owner; framework adapters return
an explicit human-input-required state. Approval uncertainty survives navigation;
authoritative read reconciles it before another decision is offered.

Public operation projection joins the assignment/task/action/activity/work-admission
records in one bounded snapshot, excluding private checkpoint/credential content.
Do not manufacture missing historical attempts or delivery timestamps for old work.
SSE/poll revalidate the original current credential at every delivery interval;
revocation, expiry or inconclusive identity ends delivery with a bounded error.
Pagination is stable, owner-scoped and exposes truncation/resync. Controls use the
current authoritative snapshot even if an earlier UI response arrives late.

Measurement projection distinguishes logical task count from physical claims,
observed resource interval sums from elapsed unions, null/unknown from zero and
server delivery from verified client visibility. Reuse existing telemetry/audit
repositories; do not import donor process counters as historic evidence. Projection
renders through Primitives/ROTE. The web milestone may expose new controls with
explicit server-owned older-native dispositions; old native workflows remain.

## 8. Required verification boundaries

Start with the existing Deep suites:
`backend/persistent_agents/tests/{test_service,test_execution,test_runner,test_controls,test_approvals,test_child_authority,test_dispatch_integration,test_engine_postgres}.py`,
`backend/personalization/tests/`, `backend/llm_config/tests/`, and work-admission/
scheduler/owner/authority tests under `backend/tests/`. Plane baselines include
`components/AstralPlane/tests/repositories/{test_work_admission,test_scheduler,test_assignments,test_assignments_postgres,test_preferences,test_credentials}.py`,
`components/AstralPlane/tests/authority/` and
`components/AstralPlane/tests/test_schema_migrations.py`. Carry donor invariant
tests as adapted behavior tests, never
imports of donor storage/test-only authorization into production code.

Required new PostgreSQL cases: populated old profile preserved; one-shot Send
without offline consent and unattended refusal without it; duplicate admission
with unchanged allowance; mixed-profile capacities; simultaneous pause/cancel,
permit/settlement/publication; source-text reacquisition after a single model outage;
ten held jobs plus another owner's ready job; revocation/expiry while issuance
waits for owner lock; note expiry/edit versus permit; stale output cannot publish;
crash after reservation/permit/publication; audit failure rollback; native old
contract compatibility. Owner draft isolation/bootstrap ordering and simplified
one-Send browser regressions accompany the runtime cases. Real IAM/provider/egress
journeys and web/native milestones remain separate qualification evidence.
