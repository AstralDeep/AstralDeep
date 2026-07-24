# Phase 1 Data Model: Remote Compute Agents

**Feature**: `063-remote-compute-agents` | **Date**: 2026-07-24 | **Spec**: [spec.md](spec.md)

All schema changes ship as **idempotent, guarded startup deltas** inside
`shared/database.py::_apply_full_schema` (Constitution IX), gated by the
`SCHEMA_REVISION` marker (`database.py:296`). This feature bumps
**`SCHEMA_REVISION` `060.004` → `063.001`** (`database.py:32`) and updates the CI guard
constant `EXPECTED_SCHEMA_REVISION` in `backend/tests/test_schema_revision_guard.py:24`
(which hashes the `_init_db` source region and fails CI on an un-bumped change).

Four new tables, all owned per-user, none seeded. Rollback for the whole feature is at
the end of this document. Every DDL uses `CREATE TABLE IF NOT EXISTS` +
`CREATE [UNIQUE] INDEX IF NOT EXISTS`, so re-running the full schema is safe; the same
template as `agent_trust` (`database.py:1302-1311`) and `user_agent` (`:606-632`).

---

## Entity: Registered machine — `remote_machine`

A user-owned record of a remote computer. Owned by exactly one user; never shared,
never seeded; the inventory starts empty for every user (FR-008). Address and port are
authoritative — the transport reads them from this row, never from model arguments (FR-018).

```sql
-- Rollback: DROP TABLE IF EXISTS remote_machine;
CREATE TABLE IF NOT EXISTS remote_machine (
    machine_id            TEXT PRIMARY KEY,          -- uuid4 hex
    owner_user_id         TEXT NOT NULL,             -- per-user isolation (FR-010)
    label                 TEXT NOT NULL,             -- user-supplied display name
    address               TEXT NOT NULL,             -- hostname or IP as registered
    port                  INTEGER NOT NULL DEFAULT 22,
    username              TEXT NOT NULL,
    os_family             TEXT NOT NULL,             -- 'linux' | 'windows' | 'macos'
    role                  TEXT NOT NULL,             -- 'cluster' | 'plain'
    host_key_type         TEXT,                      -- e.g. 'ssh-ed25519' (recorded at 1st reg, FR-020)
    host_key_fingerprint  TEXT,                      -- 'SHA256:...' recorded identity
    host_key_blob         TEXT,                      -- base64 server public key for paramiko verify
    last_verdict          TEXT,                      -- last reachability verdict (FR-034 vocab)
    last_checked_at       BIGINT,
    created_at            BIGINT NOT NULL,
    updated_at            BIGINT NOT NULL,
    CONSTRAINT ck_remote_machine_os   CHECK (os_family IN ('linux','windows','macos')),
    CONSTRAINT ck_remote_machine_role CHECK (role IN ('cluster','plain'))
);
CREATE INDEX IF NOT EXISTS idx_remote_machine_owner ON remote_machine (owner_user_id);
```

**Rules & notes**
- No global uniqueness on `address` — a user may register the same machine twice under
  two labels, then delete one (edge case). Rows are addressed only by `machine_id`.
- `host_key_*` are NULL until the first successful key exchange records them; a
  subsequent connection whose key differs from `host_key_blob` yields `host_key_mismatch`
  and refuses (FR-020). Re-trust is an explicit `chrome_machine_retrust` action that
  overwrites `host_key_*` deliberately (never automatic).
- `last_verdict`/`last_checked_at` back the "list machines with last-known reachability"
  read verb (FR-023); they are advisory, never a substitute for a live probe.

---

## Entity: Machine credential — `machine_credential`

The secret used to authenticate to one machine for one user. Encrypted at rest under
`CREDENTIAL_ENCRYPTION_KEY` via the existing Fernet path (`credential_manager.py:57-76`).
1:1 with a machine; deletable independently of the machine (FR-015). **FR-014 honesty**:
these are in-process agents, so decrypted material transiently exists in orchestrator
memory during a connection — the protection is encryption at rest + per-user isolation,
not process isolation.

```sql
-- Rollback: DROP TABLE IF EXISTS machine_credential;
CREATE TABLE IF NOT EXISTS machine_credential (
    machine_id           TEXT PRIMARY KEY
                         REFERENCES remote_machine (machine_id) ON DELETE CASCADE,
    owner_user_id        TEXT NOT NULL,              -- for per-user revocation sweeps + isolation
    cred_type            TEXT NOT NULL,              -- 'ssh_key' | 'password'
    encrypted_secret     TEXT NOT NULL,             -- Fernet(PEM) or Fernet(password)
    encrypted_passphrase TEXT,                       -- Fernet(passphrase) for an encrypted key (FR-011)
    created_at           BIGINT NOT NULL,
    updated_at           BIGINT NOT NULL,
    CONSTRAINT ck_machine_credential_type CHECK (cred_type IN ('ssh_key','password'))
);
CREATE INDEX IF NOT EXISTS idx_machine_credential_owner ON machine_credential (owner_user_id);
```

**Rules & notes**
- `ON DELETE CASCADE` makes machine deletion destroy the secret (one of the four FR-015
  triggers). The other three — credential delete, agent retirement, account removal — are
  explicit `DELETE FROM machine_credential` paths (see Retirement & revocation below).
- A row that cannot be Fernet-decrypted (rotated key) → `credential_undecryptable` verdict
  (FR-016), distinct from a missing row (`credential_not_configured`) and from a rejected
  credential (`auth_failed`).
- Never returned in cleartext by any read path; never logged or audited (FR-049).

---

## Entity: Destructive proposal — `remote_operation_proposal`

A durable, single-use, expiring, user-bound and argument-bound record of an intended
destructive operation awaiting explicit approval (FR-029–FR-033). This is the net-new
mechanism (spec Dependencies). Survives an orchestrator restart (FR-032).

```sql
-- Rollback: DROP TABLE IF EXISTS remote_operation_proposal;
CREATE TABLE IF NOT EXISTS remote_operation_proposal (
    proposal_id      TEXT PRIMARY KEY,               -- uuid4 hex; the only handle a client sends
    owner_user_id    TEXT NOT NULL,                  -- only this user may approve (FR-031)
    chat_id          TEXT,                           -- originating conversation
    machine_id       TEXT NOT NULL,                  -- target machine (from inventory)
    agent_id         TEXT NOT NULL,                  -- 'remote-control-1'
    verb             TEXT NOT NULL,                  -- the destructive tool name
    args_json        TEXT NOT NULL,                  -- EXACT arguments the operation will use
    args_fingerprint TEXT NOT NULL,                  -- sha256(canonical args) — arg-binding (FR-031)
    summary          TEXT NOT NULL,                  -- "Delete /data/x on <label>" (machine+op+target)
    status           TEXT NOT NULL DEFAULT 'pending',-- pending|approved|declined|expired|consumed
    created_at       BIGINT NOT NULL,
    expires_at       BIGINT NOT NULL,                -- absolute server time; clock-skew-safe (FR-031)
    decided_at       BIGINT,
    consumed_at      BIGINT,                          -- set when executed; single-use marker
    CONSTRAINT ck_rop_status
        CHECK (status IN ('pending','approved','declined','expired','consumed'))
);
CREATE INDEX IF NOT EXISTS idx_rop_owner_status
    ON remote_operation_proposal (owner_user_id, status);
```

**State machine** (transitions are server-only; enforced in the decision handler and the gate):

```
                approve (owner, not expired)        execute (gate, atomic)
   pending ───────────────────────────────► approved ───────────────────► consumed
      │                                                                       ▲
      │ decline                          (no path back to pending or approved)│
      ├───────────────────────────► declined                                 │
      │                                                                       │
      │ read after expires_at                    a consumed/expired/declined  │
      └───────────────────────────► expired      proposal can NEVER reach ────┘
```

**Invariants (map to FR-031, US3 acceptance)**
- **Single-use**: execution transitions `approved → consumed` **atomically** (guarded
  `UPDATE … WHERE status='approved'`); a second approval/execution sees a non-pending
  status and is refused "already used".
- **Expiring**: any read past `expires_at` lazily marks `expired`; approval of an expired
  proposal is refused "expired, re-request". Absolute server time avoids the 3-clock skew
  edge case (client/orchestrator/cluster disagreement) — only the orchestrator clock counts.
- **User-bound**: a decision whose session `sub` ≠ `owner_user_id` is refused and audited
  (US3 scenario 4), regardless of who holds the `proposal_id`.
- **Argument-bound**: execution uses `args_json` from the row, and re-checks
  `args_fingerprint`; the client's decision payload carries only `{proposal_id, decision}`
  — it can never redirect the operation to different arguments (FR-031, US3 scenario 6).
- **Restart-durable**: because it is a table, a pending proposal is still approvable after
  a restart, and a restart never auto-approves (FR-032, SC-006).

---

## Entity: Tracked job — `tracked_job`

A durable record linking a scheduler job id to a machine, a user, and a conversation, with
polling state and a terminal outcome (FR-042–FR-046). Survives restart; closed honestly
when its machine or credential disappears.

```sql
-- Rollback: DROP TABLE IF EXISTS tracked_job;
CREATE TABLE IF NOT EXISTS tracked_job (
    tracked_job_id   TEXT PRIMARY KEY,               -- uuid4 hex (internal handle)
    owner_user_id    TEXT NOT NULL,
    machine_id       TEXT NOT NULL,                  -- the cluster host
    chat_id          TEXT,                           -- originating conversation
    scheduler_job_id TEXT NOT NULL,                  -- Slurm job id on that cluster
    job_name         TEXT,                           -- bounded, sanitised
    submit_marker    TEXT,                           -- idempotency nonce (FR-037)
    state            TEXT NOT NULL DEFAULT 'submitted',
    terminal         BOOLEAN NOT NULL DEFAULT FALSE,
    exit_code        TEXT,                            -- Slurm exit code string, e.g. '0:0'
    notify_on_finish BOOLEAN NOT NULL DEFAULT FALSE,  -- opt-in (FR-045)
    last_polled_at   BIGINT,
    created_at       BIGINT NOT NULL,
    updated_at       BIGINT NOT NULL,
    CONSTRAINT ck_tracked_job_state CHECK (state IN
        ('submitted','pending','running','completed','failed','cancelled',
         'timeout','orphaned','unknown'))
);
CREATE INDEX IF NOT EXISTS idx_tracked_job_owner     ON tracked_job (owner_user_id);
CREATE INDEX IF NOT EXISTS idx_tracked_job_active    ON tracked_job (terminal, last_polled_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_tracked_job_sched
    ON tracked_job (machine_id, scheduler_job_id);
```

**Rules & notes**
- **Boot reconciliation** (FR-043): startup sweeps non-`terminal` rows and polls each
  (read-only, under machine-turn authority); a job that finished during the outage moves to
  its terminal `state` with `exit_code`, not left "running".
- **Unattended polling** (FR-044): the poller uses **read-only** authority only; it may
  update state and, if `notify_on_finish`, emit a completion notice. It can never submit or
  cancel — those are consequential verbs refused on a machine turn (see confirmation gate).
- **Orphaning** (FR-046): if `machine_id`'s row or its credential is gone at poll time,
  tracking stops and `state='orphaned'`, `terminal=TRUE`, with a user-visible honest status.
- `uq_tracked_job_sched` prevents a duplicate tracking row for the same cluster job (pairs
  with the FR-037 submit marker to make a retry-created duplicate detectable).

---

## Code-level entities (not schema)

These are fixed in code and enforced by the contract test (FR-051,
[contracts/verbs.md](contracts/verbs.md)), not by database rows:

- **Verb**: a `TOOL_REGISTRY` entry (`agents/<name>/mcp_tools.py`) with a typed
  `input_schema`, a declared `scope`, a declared time bound, a declared `retryable` posture,
  and a declared **destructive classification** (a per-verb boolean in a reviewed map in the
  mutating agent's module). Naming is not a control (FR-007); the classification map and the
  gate are.
- **Agent id sets**: `remote-observe-1` joins `_FIRST_PARTY_PUBLIC_AGENT_IDS`
  (`database.py:2990-2994`) **and** the new `_SAFE_SEED_AGENT_IDS` subset;
  `remote-control-1` joins **only** `_FIRST_PARTY_PUBLIC_AGENT_IDS` (visible, not seeded — R7/FR-004).
- **Taint sources**: both ids added to `taint._UNTRUSTED_AGENTS` (`taint.py:37`, FR-039).

---

## Retirement & revocation (FR-015, FR-053, US7)

An idempotent, guarded `_cleanup_retire_063(...)` (invoked only when the capability is
retired, mirroring `_cleanup_retired_agents_040`) performs, in one transaction:

1. `DELETE FROM machine_credential WHERE owner_user_id IN (…)` / all rows on full retire —
   destroy stored secrets.
2. `DELETE FROM agent_scopes / tool_overrides` for `remote-observe-1` + `remote-control-1`;
   `DELETE FROM agent_trust WHERE agent_id IN (…)`; drop their `agent_ownership` rows.
3. `UPDATE tracked_job SET state='orphaned', terminal=TRUE` for any non-terminal rows.
4. `DROP TABLE` the four new tables (full retirement) — safe because every dependent row is
   already removed; re-running changes nothing further (SC-014).

**Per-user revocation** (account removal / logout / explicit credential delete) reuses the
existing revocation flow and adds `DELETE FROM machine_credential WHERE owner_user_id = ?`
(and, on account removal, the user's `remote_machine`/`tracked_job` rows). This is the wired
path FR-015 requires — unlike today's unwired `remove_agent_credentials`
(`credential_manager.py:212`, zero production call sites).

## Rollback (whole feature)

1. Set `SCHEMA_REVISION` back and `DELETE FROM schema_meta WHERE key='revision'`
   (`database.py:273`) to force a clean re-derive, **or**
2. `DROP TABLE IF EXISTS remote_operation_proposal, tracked_job, machine_credential, remote_machine;`
   (order respects the `machine_credential → remote_machine` FK).

Because the feature sits behind a default-off flag (see plan.md), disabling the flag returns
observable behaviour to prior state without touching schema; the DROP path is the full
teardown.
