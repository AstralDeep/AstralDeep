# Persistent assignment interface contract

**Feature**: 079-persistent-agents

**Status**: Implemented locally; authenticated live verification remains required.
**Authority**: [spec.md](../spec.md), [data-model.md](../data-model.md), and the
[Plane persistence contract](plane-assignment-api.md) define lifecycle, bounds, and durable transitions.
This document defines their user-facing projection and authenticated command seam.

## Shared surface and ownership

Settings → Personalization → Schedule presents two clearly labeled sections:
**Ongoing agents** and **Scheduled tasks**. Existing scheduled jobs retain their
behavior. Ongoing agents are persistent assignments; enabling one does not create
a new user-authored executable agent or grant blanket permission to existing agents.

Projection owns the reusable view builder, primitive composition, themes, and
ROTE adaptation. Deep's `orchestrator/projection_surfaces/personalization.py`
loads owner-authorized snapshots and calls the reusable builder. Commands call
`persistent_agents.service`; neither client code nor Projection accesses Plane
or implements product authorization. Do not expand the adapter's duplicated
HTML/native scheduling renderers as a second source of assignment presentation.

Use existing `card`, `text`, `badge`, `keyvalue`, `form`, `field`, `button`,
`container`, and `alert` primitives. Use declared properties and `css`, existing
escaping/sanitization, and `.to_dict()`/`create_ui_response()`. No new primitive,
WS frame, native menu definition, or third-party runtime dependency is introduced.
Deliver through existing `chrome_surface`/`chrome_render` and chat
`ui_render`/`ui_upsert` paths. The server selects the form-factor disposition.

The existing `chrome_open` action selects the surface with
`{"surface":"personalization","params":{"tab":"schedule"}}`; an optional
`assignment_id` in `params` opens a detail view after an owner-scoped lookup.
It never treats an identifier as proof of ownership. Unknown/other-owner IDs
share a not-found response. Returning from a detail view restores Schedule.

## List and detail views

Each compact assignment card shows name, a human-readable state, source label,
current instruction revision/control epoch, last meaningful result, next check/reason, and a
short usage/limit summary. An unchanged check updates Last checked without adding
another attention notification. List pagination is bounded; opening a detail
loads a bounded activity page, never the full assignment history.

The detail view includes current instructions, source and exact selected tool
identities, cadence, scope/grant status and expiry, remaining resource limits,
current tasks/dependencies, latest accepted findings, and pending action proposals.
Show safe diagnostic codes with an actionable explanation. Never render refresh
tokens, credentials, raw authorization claims, unbounded tool output, or source
content as raw HTML. Source quotations remain labeled external content and cannot
become editable authority without an explicit owner instruction revision.

User-facing activity labels must distinguish Waiting, Checking, Investigating,
Delegating, Waiting for approval, Authorization required, Budget exhausted,
Reconciliation required, Paused, Stopped, Completed, and Failed. These labels are
projections of the canonical assignment/work state and reason fields, not a second
stored state machine. Canonical `lifecycle` is `active`, `paused`, `stopped`, or
`completed`; active assignments expose `phase` values `waiting`, `checking`,
`investigating`, `delegating`, `waiting_approval`, `waiting_authorization`,
`budget_exhausted`, `reconciliation`, or `failed`. Display why the assignment is
waiting and what can wake it.
An in-flight external request that cannot be recalled is identified explicitly;
acknowledging pause/stop must never claim that such a request was undone.

Controls remain usable while work runs. A committed pause/stop/revision/revocation
invalidates old execution fences before its successful acknowledgment. All command
responses include `instruction_revision`, `control_epoch`, and safe status. Owner
controls compare these values, never the worker's heartbeat-driven `state_version`.
A stale edit
shows a conflict and reload affordance; it does not silently overwrite newer work.
Stopped is terminal, with no Resume button. Resuming a pause coalesces missed
checks and revalidates authority and remaining limits.

## Creation and revision fields

The same validated request model powers the shared form, chat proposal, and API.
Reject unknown fields, invalid values, and overlong text; do not silently truncate
an instruction or approval the owner is being asked to authorize. Field-level
length/range constants come from the canonical model.

| Field/group | User-facing behavior |
| --- | --- |
| Name | Required short assignment name. |
| Instructions | Required standing instructions with an optional completion condition; explain what merits attention. |
| Source | Select a currently registered, owner-authorized reader. Initial profile: public release page, `web-research-1` / `fetch_page`, with an absolute public HTTPS URL. |
| Source bounds | Display the exact selected URL/resource and any declared linked-document scope. Followed links must satisfy that scope and ordinary egress validation. |
| Check interval | Integer seconds, at least 60 and subject to a stricter operator limit. Show the next UTC timestamp in the client's selected display timezone. |
| Permitted tools | Explicit bounded identities from current effective permissions; include the reader. A missing/disabled connector is unavailable, not a credential-free placeholder. |
| Resource limits | Mandatory finite tool/model/token/time limits, per-step timeout, retry count, concurrent work, task count/depth, and delegation limits from the canonical `limits` object. Show daily and lifetime limits, units, outstanding reservations, spent usage, and remaining capacity. A currency spending cap is optional; when selected, money is integer `spend_micro_units` in the declared currency, never floating point. |
| Delegation | Owner-visible setting within the operator's recursive-delegation posture. Children spend the same durable budget; disabling it keeps the assignment usable for direct work. |
| Unattended authorization | Explicit consent to the reviewed instructions, source/tool bounds, limits, grant duration, and revocation behavior. No preselected consent or model-authored approval. |
| Result destination | Owner's current conversation or another selected owner-owned conversation. Show where meaningful activity appears. |

No inbox or bug-tracker provider account is required for the public-page example.
Existing Keycloak login, product LLM configuration where model work is requested,
and offline-grant prerequisites still apply. The public monitor needs no new
price configuration: it can run with mandatory finite usage limits and no currency
cap. In that case show monetary cost as **Unpriced/unknown**, never zero or free.
If the owner selects a currency cap, activation or revision requires a trusted
finite quote covering each eligible action; reject the activation/revision when
that bound is unavailable. A zero currency cap permits only actions with an
explicit trusted zero-cost quote, not actions whose cost is unknown. A source
requiring additional credentials remains unavailable until independently
connected by its owner.

Before Create, show a review card containing the complete bounded instructions,
source, tools/scopes, all limits, grant expiry, result destination, and how to pause,
stop, and revoke. The owner activates **Approve & create ongoing agent**. A chat
proposal may prefill the form but cannot activate it. Failed grant capture leaves
an honest authorization-required proposal, never an apparently running agent.
Revising instructions or widening source/tool/limit authority produces a
new reviewable revision and requires explicit consent before that revision runs.
No older proposal or wider existing account grant substitutes for this consent.

## Exact UI action vocabulary

New actions are registered in Projection's `contracts/ui_protocol.json`
`accept_actions`, Deep's surface `HANDLERS`, and the exact controller command
registry. Existing `chrome_open` handles list/detail/refresh navigation.

| Action | Meaning and required payload |
| --- | --- |
| `chrome_assignment_create` | Create from an explicitly approved form; `submission_id`, reviewed create fields, `consent`. |
| `chrome_assignment_revise` | Replace reviewed editable fields; common mutation fields, revision fields and consent for changed authority. |
| `chrome_assignment_pause` | Fence and pause parent/children; common mutation fields. |
| `chrome_assignment_resume` | Revalidate and resume a paused assignment; common mutation fields. |
| `chrome_assignment_stop` | Terminal stop and invalidate outstanding approvals/work; common mutation fields. |
| `chrome_assignment_revoke` | Revoke this assignment's authorization binding and fence work; common mutation fields. Must not revoke unrelated grants/assignments implicitly. |
| `chrome_assignment_run_now` | Invoke Plane's owner-scoped `request_check` through the service to coalesce one eligible immediate check; common mutation fields. It does not lift cadence/rate/budget limits, invalidate a live claim, or revive stopped work. |
| `chrome_assignment_approval_decide` | Common mutation fields, `action_id`, exact `request_digest`, and `decision` (`approve` or `decline`); never replacement tool arguments. The proposal is the canonical action ledger row, not a separate ID namespace. |

Common mutation fields are `assignment_id`, positive
`expected_instruction_revision`, positive `expected_control_epoch`, and UUID4
`submission_id`. The server supplies submission IDs for rendered controls.
Transport retries reuse the same ID and exact body. A completed identical
submission returns its retained result; changing its body/resource is a conflict.
IDs are owner-scoped. Idempotency lookup precedes stale-revision rejection for an
identical already-completed submission, so a lost acknowledgment is recoverable.

UI state and activity use existing delivery identities to replace an existing
card or publish one committed finding exactly once. A disconnected client reloads
durable state/history; it does not require replay of every transient WS frame.

## Authenticated API and approval semantics

`persistent_agents/api.py` mounts `/api/persistent-agents`, documented by FastAPI
at `/docs`. REST uses the existing validated Keycloak bearer path
(`require_user_id`); UI commands use the validated owner UI session and existing
socket/origin protections. Do not introduce a cookie-only mutation endpoint.
Never accept a caller-supplied owner, delegated agent token as human approval,
or unauthenticated webhook.
All responses containing owner state use `Cache-Control: no-store`.

| Method/path | Contract |
| --- | --- |
| `GET /api/persistent-agents` | Bounded owner-only collection with opaque continuation cursor. |
| `POST /api/persistent-agents` | Explicitly consented create model plus submission ID; `201` for first creation, retained success on retry. |
| `GET /api/persistent-agents/{assignment_id}` | Current revision, public state/reason, bounded progress, authority summary, limits/usage and pending proposals. |
| `PATCH /api/persistent-agents/{assignment_id}` | Reviewed revision model with expected instruction revision/control epoch and submission ID. |
| `POST /api/persistent-agents/{assignment_id}/{pause,resume,stop,revoke,run-now}` | Corresponding service command, common mutation fields; the path ID must match any body ID. |
| `GET /api/persistent-agents/{assignment_id}/activity` | Bounded owner activity page with opaque cursor. |
| `GET /api/persistent-agents/{assignment_id}/tasks` | Bounded parent/child task view; safe results and incorporation state. |
| `POST /api/persistent-agents/{assignment_id}/approvals/{action_id}/decision` | Owner's exact approval/decline and request digest; no caller replacement of proposed arguments/preconditions. |

Use `401` for absent/invalid authentication, `403` for insufficient current
authority, `404` for unavailable owner resources, `409` for stale revision,
idempotency conflict or illegal lifecycle transition, `422` for invalid/missing
consent or request fields, `429` for admission limits, and `503` for unavailable
runtime/feature prerequisites. Return a stable bounded error code and safe message.
Accepted asynchronous work is `202` with its durable identity; it is not reported
as an already completed external effect.

Approval cards show exact tool/agent, target, material arguments, consequence,
proposal/instruction revision, expiry, and required target preconditions. Approval
is immutable, owner-bound, single-use, expiring, and auditable. Clicking approval
authorizes only that proposal. The service rechecks lifecycle/revision, live
permissions, grant, budget, target preconditions, and the ordinary gate stack at
execution. Any mismatch requires a newly reviewed proposal. Existing
interactive-only actions execute through the live human dispatch path; their
accepted result may wake the assignment but does not make the action unattended.
After the background worker releases its lease while waiting, the foreground
approval path obtains the dedicated approved-action claim defined by Plane. That
claim authorizes only the exact reviewed action and current attended operation;
it does not reclaim general background work or require an obsolete worker lease.
It must race safely with a worker claim and owner pause/stop/revision/revocation.
The normal gate stack may still require `remote_confirmation` for the exact
attended operation. Assignment approval does not bypass or silently satisfy that
gate. Link its existing proposal to the assignment action and explain any second
review explicitly. The owner decision re-enters the full gate stack with stored
arguments; retain its exact completed/uncertain outcome in the assignment ledger
before waking the parent. A transient confirmation card is not action completion,
and a second review must not lose the original action identity or permit a repeat.
An uncertain external result requires reconciliation, not an automatic retry.

## Chat and wrist behavior

Chat offers creation proposals and read-only status/activity retrieval plus
revise, pause, resume, stop, revoke, and run-now operations against an unambiguous
owner assignment. Example intents: “Monitor this release page daily,” “What is
Release watch waiting for?”, “Change Release watch to only flag security changes,”
“Pause Release watch,” “Resume Release watch,” and “Stop Release watch.”
Creation/revision tools propose reviewable authority; they never synthesize
`consent=true` on the owner's behalf. Control tools invoke the same service
commands with current revision and durable submission identity. Ambiguous names
return a bounded selection prompt without mutating every matching assignment.
External page/email/bug text cannot invoke lifecycle controls or approve proposals.
Machine turns and delegated children cannot call owner lifecycle meta-tools.

Web, Windows, Android, macOS, and iOS render equivalent creation, detail, controls,
and approvals through the shared full-client surface. Comparable form factors
receive equivalent layouts with declared ROTE adaptation and server theme roles.

Wrist targets receive bounded status, findings, next-wake reason, and chat-based
lifecycle controls through their existing conversation path. Existing wrist
rendering does not make generic form/button cards interactive. The server omits
the full creation/permission/approval form and provides an explicit “Continue in
AstralDeep on your phone or computer” disposition tied to the same owner
conversation/assignment. Status, pause, resume, stop, and narrowing instruction
requests stay available through chat. A revision needing detailed consent remains
pending until full-client review. No new watch-local menu or hidden client-side
feature policy is introduced; an unavailable voice path retains typed/full-client
handoff behavior without granting additional authority.

## Contract verification

Fixtures must cover full and wrist dispositions, empty/offline/disabled views,
long escaped instructions, all state/reason labels, expired/rejected proposals,
conflicting edits, stale/duplicate controls, owner isolation, terminal stop,
and unchanged-check silence. Include no-currency-cap views with mandatory finite
usage limits and Unpriced/unknown money; missing-quote refusal when a currency cap
is selected; foreground approval after release of the background lease; and
competing-worker/control denial of stale approved-action claims. Exercise
linked assignment/remote confirmation, explicit second-review state, exact
gate re-entry, and durable result incorporation without duplicate dispatch. Exercise
`request_check` duplicate submissions, pending-wake coalescing, current-claim
preservation, cadence/budget refusal, and stopped/paused assignments. Assert that
UI, chat and REST call the same service
and that no client reconstructs permission or budget rules.

Keep Deep UI manifest/chrome/projection boundary suites and Projection reusable
view tests green. Run Windows/Android/Apple manifest drift guards; Apple's current
`ManifestDriftTests` also pins the action count and must be updated deliberately.
Exercise every affected live target against the same candidate backend, including
the explicit wrist handoff and chat controls. Automated renderer/contract tests
alone do not satisfy the live parity requirement. See [quickstart.md](../quickstart.md).
