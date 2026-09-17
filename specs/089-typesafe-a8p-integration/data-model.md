# Data Model: TypeSafe Routing and a8p UI Integration

Only one durable entity is added: the per-user TypeSafe credential. Everything else in this feature is ephemeral, per-turn state, or in-process state that is intentionally not persisted.

## 1. Durable: `user_typesafe_credential` (AstralPlane, revision `089.001`)

**Owner**: AstralPlane stores the ciphertext only. AstralDeep encrypts and decrypts with the existing Fernet key (`CREDENTIAL_ENCRYPTION_KEY`, resolved by `llm_config/user_store.py::_resolve_fernet`). Plane never sees plaintext.

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `user_id` | `TEXT` | `PRIMARY KEY` | Verified Keycloak subject; same owner key as `user_llm_config.user_id` |
| `api_key_enc` | `BYTEA` | `NOT NULL` | Fernet token of the TypeSafe key |
| `key_fingerprint` | `TEXT` | `NOT NULL` | `sha256(key)[:12]`, used only to detect "same key re-saved" and to reset a rejected status; never displayed and never logged together with a user identifier |
| `last_verified_at` | `TIMESTAMPTZ` | `NULL` | Time of last successful verification (save probe or successful turn) |
| `last_verification_outcome` | `TEXT` | `NOT NULL DEFAULT 'unverified'`, `CHECK IN ('unverified','valid','rejected','unavailable')` | Drives the FR-006 status line |
| `last_outcome_at` | `TIMESTAMPTZ` | `NULL` | When `last_verification_outcome` last changed |
| `created_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT now()` | |
| `updated_at` | `TIMESTAMPTZ` | `NOT NULL DEFAULT now()` | |

**Invariants**

- There is no `system_*` counterpart table. A deployment-wide or system TypeSafe key cannot be represented (FR-005).
- The table has no foreign key into `user_llm_config`. Deleting an LLM config never deletes this row, and vice versa (FR-004).
- No read path other than `EncryptedTypeSafeCredentialRepository` touches the table.

**Repository** — `astralplane.repositories.secrets.EncryptedTypeSafeCredentialRepository`, mirroring `EncryptedLLMConfigRepository`:

- `get_user(tx, user_id) -> EncryptedTypeSafeCredentialRecord | None`
- `upsert_user(tx, record)` — insert or replace. Sets `updated_at`, and sets `last_verification_outcome='valid'` plus `last_verified_at` from the record.
- `delete_user(tx, user_id) -> bool`
- `record_outcome(tx, user_id, outcome, at, expected_fingerprint)` — a conditional update that applies only when `key_fingerprint = expected_fingerprint`, so a late outcome from an old key can never mark a newly saved key as rejected.
- `EncryptedTypeSafeCredentialRecord.__repr__` omits `api_key_enc`.

**State transitions of `last_verification_outcome`**

```text
(no row) --save+probe ok--> valid
valid --turn auth failure (401/403)--> rejected
valid --turn outcome: repeated unavailable (circuit opens)--> unavailable
unavailable|rejected --successful turn--> valid
rejected --re-save same fingerprint, probe ok--> valid
any --clear--> (no row)
save+probe fail --> no change to existing row (FR-003)
```

## 2. Migration `089.001` (AstralPlane guarded registry)

This follows the pattern `088.008` used (Plane commit `1aee0db3`):

1. Add `src/astralplane/database/typesafe_credential_schema.py` with the `CREATE TABLE` and `CHECK` statements.
2. Add `_apply_plane_schema_089_001(tx)`, which first calls `_verify_predecessor_plane_schema(tx, "088.008")`.
3. Add `088.008` and its structure digest to `PREDECESSOR_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS`.
4. Append `Migration("089.001", …)` and its `REGISTRY_DIGEST` constant to `MIGRATION_REGISTRY`. Update `CURRENT_SCHEMA_STRUCTURE_DIGEST` and `CURRENT_DATA_PLANE_REVISION` read-compatible and predecessor digests.
5. Update `database/revision.py` constants and `baseline.py::_KNOWN_REVISIONS`.
6. Add the table to the catalog structure verification lists (next to `user_llm_config`).
7. Update `docs/migration-and-recovery.md`, `provenance/transformations.json` and the repository contract matrix test.
8. Tests:
   - upgrade from a populated `088.008` fixture;
   - repeat is a no-op;
   - catalog structure verification rejects a tampered table;
   - rollback/recovery procedure: `DROP TABLE user_typesafe_credential` plus revision marker restore, documented and tested on a copy;
   - repository owner isolation;
   - fingerprint-conditional outcome update.
9. Deep: bump `config/astral-composition.json` → `compatibility.data_plane.schema_revision = "089.001"` and the new `migration_sha256`, then repin the `components/AstralPlane` submodule.

**Rollback**: The table is additive and has no dependents. Recovery removes the table and restores the `088.008` registry marker. Every user reverts to standard routing, and no other data is affected.

## 2a. Durable: `user_data_sharing_acknowledgment` (AstralPlane, same revision `089.001`)

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `user_id` | `TEXT` | `PRIMARY KEY` | Verified Keycloak subject |
| `notice_version` | `TEXT` | `NOT NULL` | Version of the acknowledged warning text (`llm_config.data_sharing.NOTICE_VERSION`) |
| `acknowledged_at` | `TIMESTAMPTZ` | `NOT NULL` | Latest acknowledgment time |
| `first_acknowledged_at` | `TIMESTAMPTZ` | `NOT NULL` | First acknowledgment time, for audit correlation |

**Invariants**:
- Independent of `user_llm_config` and `user_typesafe_credential`; clearing either leaves it intact.
- Upsert only; no delete path except account-level purge through existing owner purge flows.
- "Acknowledged" means `notice_version == NOTICE_VERSION`.

**Repository**: `astralplane.repositories.preferences.DataSharingAcknowledgmentRepository` with `get_user(tx, user_id)` and `acknowledge(tx, user_id, notice_version, at)`. `acknowledge` preserves `first_acknowledged_at`.

**Migration**: Added to the same `089.001` schema module and structure digest as §1. Tests cover owner isolation, repeat acknowledgment preserving `first_acknowledged_at`, and version replacement. Rollback drops both tables.

## 3. Deep facade: `llm_config/typesafe_store.py`

- `TypeSafeCredentialStore.get_key(user_id) -> str | None`
  - Decrypts the key.
  - An undecryptable row is deleted and a `typesafe_credential.discarded` audit event is recorded, mirroring `user_store` discard handling. That user then has no key.
- `status(user_id) -> TypeSafeKeyStatus` returns `not_set`, `active`, `rejected(at)` or `unavailable(at)`.
- `save(user_id, key)` is called only after a successful probe, inside the durable credential operation.
- `clear(user_id)`
- `record_outcome_async(user_id, outcome, fingerprint)` is fire-and-forget off the hot path and never awaited by a turn.

## 4. Ephemeral per-turn entities (never persisted)

### `RoutingRequest`

| Field | Source | Bound |
|---|---|---|
| `current_request` | user message after slash expansion, datamarked as sent to the LLM | truncated to `ROUTING_MAX_REQUEST_CHARS` |
| `recent_conversation` | last messages from `history_messages` (role + text only) | ≤ 6 messages, each truncated |
| `active_agent` | agent of the previous assistant turn, if any | id only |
| `user_selected_tools` | composer `selected_tools` | names only |
| `agents` | distinct agents in the eligible set: id, name, description | ≤ `MAX_ROUTING_AGENTS` |
| `tools_by_agent` | eligible prefixed tool names + descriptions | ≤ `MAX_TOOLS_PER_AGENT` per agent |

Excluded: attachment bodies, tool outputs, credentials, file maps, memory/guidance text (FR-038).

### `RoutingDecision`

| Field | Type |
|---|---|
| `jailbreak_p` | float 0–1 |
| `harm_score` | float (probability-weighted level) |
| `threat_category` | enum (7) |
| `agent_id`, `agent_p` | str / float, or `no_tool_needed` |
| `tool_name`, `tool_p`, `tool_margin`, `tool_distribution` | chosen agent's tool question only |
| `presentation_style` | enum: `dashboard`, `detailed_table`, `alert_focused`, `conversational`, `as_delivered` |
| `tier` | `high`, `medium` or `low` (derived) |
| `shortlist` | list of prefixed tool names (medium only) |
| `security_verdict` | `pass`, `confirm_tools` or `refuse` (derived) |
| `attempts`, `latency_ms`, `outcome` | int / int / `success`, `fallback_transient`, `fallback_nontransient`, `skipped_no_key`, `skipped_circuit`, `skipped_empty_eligible`, `skipped_unavailable`, `cancelled` |

Carried on the turn context object for round 1 and `_run_gate_stack`, then discarded at turn end.

### `RoutingCircuit` (in-process, per worker)

`{user_id: {consecutive_failures, state: closed|open|half_open, open_until, auth_blocked_fingerprint}}`

- It is lost on restart by design; a restart simply re-tries.
- It is bounded to users active in this process: entries idle for more than one hour are evicted.

## 5. Audit events (existing hash-chained audit, no content)

| Action | When | Metadata (no text, no key) |
|---|---|---|
| `llm_data_sharing.acknowledged` | a credential save carrying a checked acknowledgment for a version not yet acknowledged | `user_id`, `notice_version`, `surface` (`llm_settings`, `first_run` or `ws_llm_config_set`) |
| `llm_data_sharing.save_blocked` | a save rejected for missing acknowledgment | `user_id`, `notice_version`, `action` |
| `typesafe_credential.saved` | successful save | `user_id`, `outcome=valid` |
| `typesafe_credential.save_rejected` | probe failed | `user_id`, error class |
| `typesafe_credential.cleared` | clear | `user_id` |
| `typesafe_credential.discarded` | undecryptable row removed | `user_id` |
| `typesafe.security_verdict` | verdict ≠ pass | `turn_id`, `chat_id`, tier, threat category, rounded `jailbreak_p`/`harm_score`, `agent_id`, `tool_name`, `latency_ms` |
| `typesafe.routing_fallback` | fallback taken | `turn_id`, outcome class, attempts, `latency_ms` |

Successful routing decisions are **not** audited per turn. They are recorded as metrics only, to avoid audit volume.

## 6. Metrics (existing `RuntimeObservability`, low cardinality)

- `typesafe_routing_attempt_latency_ms` histogram (fixed buckets)
- `typesafe_routing_turn_outcome_total{outcome}`
- `typesafe_routing_tier_total{tier}`
- `typesafe_security_verdict_total{verdict}`
- `typesafe_layout_applied_total{result=template|designer_fallback|flat}`
- `perf_span("turn.typesafe")` log line per turn

## 7. Primitives (AstralPrimitives 0.4.0)

See `contracts/ui-primitives-089.md` for full shapes. These are wire-level additions only; no existing primitive shape changes. The `contract_sha256` and `package_version` in `config/astral-composition.json` are updated on repin.
