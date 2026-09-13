# Implementation and qualification

Continue on `codex/088-completion` after the owner merged and deleted the original
review branches. Set `SPECIFY_FEATURE_DIRECTORY=specs/088-rewrite-integration` for
each Spec Kit script. Do not allocate another number or mutate source 081–087
branches. Read the inventory, contracts and tasks before implementation.

## Baselines

Record Deep/component statuses and identities. Branch only clean pinned components being edited; preserve donor and unrelated trees. Use approved Python 3.11-compatible environments and pinned test tooling. Inspect each touched repository's CI for all module suites. Record preexisting failures before changes.

Run targeted Projection/Deep commands in `contracts/ui.md`; Plane assignment/admission/scheduler/migration tests for durable changes; auth/delegation/egress suites for security changes. Measure changed Python coverage >=90%, lint touched languages, then expand to relevant full CI. Logs/private fixtures stay outside committed source. Stage reviewed exact paths only.

## Integrated journeys

The current local backend slice registers `POST /api/work/v1/operations` for the
qualified retained, single-public-page research profile. It requires normal
institutional cookie authentication, same-origin JSON, the original server-owned
session incarnation, sufficient source/model budget and the exact encrypted USER
provider profile. The existing persistent-agent flag must compose a live runner.
Accepted receipt replay remains independent of current provider/runner availability.
This endpoint does not replace ordinary chat Send or establish native/framework
execution authority from a bare bearer token.

`GET /api/work/v1/operations/{id}/result` uses current owner read authority and
returns `{id, revision, result}`. The version-1 result envelope has `available`,
`reason` and `content`; supported completed retained results contain attributed
one-page excerpts. Otherwise the reason is `not_completed`, `unsupported`,
`not_retained` or `unavailable`, and content is null. Metadata list/detail/poll
responses stay payload-free. Missing historical verification keys refuse result
content; clearing provider settings does not erase a valid completed public result.
Resume/event/approval/reconciliation and shared Work views remain separate work.

Existing pause/cancel/delete commands require a coherent same-runtime application
audit repository. Their mutation and required audit now commit together, with
bounded SQL lock waits; a refused command leaves no new receipt or state change.
Retry pause/cancel using the same command and observed revision after an unknown
response. A matching existing receipt acknowledges the earlier command without
new execution or audit. Terminal deletion remains non-replayable and returns 404
when already absent. The new private continuation observation alone does not
activate resume/wake or grant dispatch.

- Existing Keycloak owner signs in; native PKCE remains functional. One ordinary Send starts chat/public research with existing provider, tool, PHI, egress and budget gates.
- Owner A drafts, logs out, owner B logs in in the same tab: no A data/selection/history flash. Test same-owner reconnect, both bootstrap completion orders and genuine outage recovery.
- Duplicate submission, reconnect and restart yield one logical task and no duplicated effect. Cancel/pause/resume and uncertainty reconciliation use current authorized bindings.
- Ten blocked older schedule episodes cannot starve a later eligible owner. Test cron/interval/one-shot and monitoring initial/unchanged/changed/insufficient-evidence outcomes; Stop prevents further admission and unchanged content avoids model generation.
- No-retention source fetch plus transient provider failure retries by safely refetching. Expired/revised/removed guidance cannot execute through stale approval; note plaintext never becomes evidence or durable action-request history.
- Credential mint racing revocation is denied at the final authority boundary. Independent owner-issued credential lifetime follows its documented contract; recursive delegation stays attenuated and parent-bound.
- Exact-result saving rejects changed/expired/consumed reviews. Verify grounding quality manually separately from publication mechanics.
- Exercise all inventory destinations and existing tools/providers/attachments/voice/history at 320px, 200% text, keyboard/reduced-motion/error/offline states. Five new users attempt their first task; record human observations, not inferred passes.

## Migration and staging

Use an isolated candidate database with representative existing synthetic/approved data and normal Plane guarded migrations. Verify populated upgrade, repeat application, schema/digest, preservation, backup/recovery and no duplicate effects before pinning Plane in Deep. Do not alter standalone donor or production data.

Staging must use exact candidate images, real configured institutional IAM, PostgreSQL/workers and affected clients. The user performs interactive sign-in. Keep secrets in existing mechanisms; no environment-backed LLM keys. Native compatibility gates apply to web delivery; redesigned native clients require later live qualification.

Before any separately authorized product push/release, run existing `scripts/prepare_release_evidence.py` using its current CLI/canonical inputs. Missing evidence stays fail-closed; this guide creates no release, bootstrap or exception approval. Track specified, implemented, tested, live-verified, merged and released separately.
