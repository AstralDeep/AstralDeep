"""Guarded, repeat-safe PostgreSQL schema migration runner: MigrationRegistry chains
every additive schema module (assignment_schema.py, guidance_schema.py,
typesafe_credential_schema.py, ...) under checksum-pinned Migration edges.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from astralplane.contracts import MigrationCallable, PlaneDatabase, Transaction
from astralplane.database.assignment_schema import ASSIGNMENT_SCHEMA_STATEMENTS
from astralplane.database.atlas_link_schema import ATLAS_LINK_SCHEMA_STATEMENTS
from astralplane.database.atlas_page_schema import ATLAS_PAGE_SCHEMA_STATEMENTS
from astralplane.database.atlas_revision_schema import ATLAS_REVISION_SCHEMA_STATEMENTS
from astralplane.database.audit_conversation_schema import AUDIT_CONVERSATION_SCHEMA_STATEMENTS
from astralplane.database.capability_publication_schema import (
    CAPABILITY_PUBLICATION_SCHEMA_STATEMENTS,
)
from astralplane.database.chat_queue_schema import CHAT_QUEUE_SCHEMA_STATEMENTS
from astralplane.database.chat_read_receipt_schema import CHAT_READ_RECEIPT_SCHEMA_STATEMENTS
from astralplane.database.completion_wake_schema import COMPLETION_WAKE_SCHEMA_STATEMENTS
from astralplane.database.computer_use_schema import COMPUTER_USE_SCHEMA_STATEMENTS
from astralplane.database.conversation_branch_schema import (
    CONVERSATION_BRANCH_SCHEMA_STATEMENTS,
)
from astralplane.database.declarative_agent_schema import DECLARATIVE_AGENT_SCHEMA_STATEMENTS
from astralplane.database.framework_credential_schema import (
    FRAMEWORK_CREDENTIAL_SCHEMA_STATEMENTS,
)
from astralplane.database.guidance_schema import GUIDANCE_SCHEMA_STATEMENTS
from astralplane.database.harness_binding_schema import HARNESS_BINDING_SCHEMA_STATEMENTS
from astralplane.database.harness_capture_schema import HARNESS_CAPTURE_SCHEMA_STATEMENTS
from astralplane.database.mesh_event_schema import MESH_EVENT_SCHEMA_STATEMENTS
from astralplane.database.mesh_schema import MESH_SCHEMA_STATEMENTS
from astralplane.database.notification_schema import NOTIFICATION_SCHEMA_STATEMENTS
from astralplane.database.observation_schema import OBSERVATION_SCHEMA_STATEMENTS
from astralplane.database.operation_schema import OPERATION_SCHEMA_STATEMENTS
from astralplane.database.project_schema import PROJECT_SCHEMA_STATEMENTS
from astralplane.database.push_delivery_schema import PUSH_DELIVERY_SCHEMA_STATEMENTS
from astralplane.database.revision import DataPlaneRevision, validate_revision
from astralplane.database.run_ledger_schema import RUN_LEDGER_SCHEMA_STATEMENTS
from astralplane.database.scheduler_policy_schema import SCHEDULER_POLICY_SCHEMA_STATEMENTS
from astralplane.database.selected_input_schema import SELECTED_INPUT_SCHEMA_STATEMENTS
from astralplane.database.stop_schema import STOP_SCHEMA_STATEMENTS
from astralplane.database.typesafe_credential_schema import (
    TYPESAFE_CREDENTIAL_SCHEMA_STATEMENTS,
)
from astralplane.database.verification_credential_schema import (
    VERIFICATION_CREDENTIAL_SCHEMA_STATEMENTS,
)
from astralplane.errors import MigrationDefinitionError, SchemaRevisionError

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SCHEMA_META_REVISION: Final = "revision"
_SCHEMA_META_DIGEST: Final = "astralplane_migration_digest"

_ADVISORY_LOCK_SQL: Final = "SELECT pg_advisory_xact_lock(%s, %s)"
_CREATE_SCHEMA_META_SQL: Final = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
""".strip()
_READ_SCHEMA_META_SQL: Final = """
SELECT key, value
FROM schema_meta
WHERE key IN (%s, %s)
""".strip()
_WRITE_SCHEMA_META_SQL: Final = """
INSERT INTO schema_meta (key, value)
VALUES (%s, %s)
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
""".strip()

_PLANE_SCHEMA_067_PREFIX: Final = (
    "ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS chain_sequence BIGINT",
    "SET LOCAL audit.allow_purge = 'true'",
    """
DO $astralplane_audit_backfill$
DECLARE
    invalid_rows BIGINT;
BEGIN
    SELECT COUNT(*) INTO invalid_rows
    FROM (
        SELECT
            actor_user_id,
            event_id,
            prev_hash,
            LAG(entry_hash) OVER (
                PARTITION BY actor_user_id ORDER BY recorded_at, event_id
            ) AS expected_previous,
            ROW_NUMBER() OVER (
                PARTITION BY actor_user_id ORDER BY recorded_at, event_id
            ) AS sequence
        FROM audit_events
    ) AS ordered
    WHERE (
        sequence = 1
        AND prev_hash <> decode(repeat('00', 32), 'hex')
    ) OR (
        sequence > 1
        AND prev_hash IS DISTINCT FROM expected_previous
    );
    IF invalid_rows <> 0 THEN
        RAISE EXCEPTION 'audit chain topology is not a complete ordered chain'
            USING ERRCODE = '23514';
    END IF;

    SELECT COUNT(*) INTO invalid_rows
    FROM (
        SELECT actor_user_id, entry_hash
        FROM audit_events
        GROUP BY actor_user_id, entry_hash
        HAVING COUNT(*) <> 1
    ) AS duplicate_hashes;
    IF invalid_rows <> 0 THEN
        RAISE EXCEPTION 'audit chain contains duplicate entry hashes'
            USING ERRCODE = '23514';
    END IF;

    WITH ordered AS (
        SELECT
            event_id,
            ROW_NUMBER() OVER (
                PARTITION BY actor_user_id ORDER BY recorded_at, event_id
            ) AS sequence
        FROM audit_events
    )
    UPDATE audit_events AS event
    SET chain_sequence = ordered.sequence
    FROM ordered
    WHERE event.event_id = ordered.event_id
      AND event.chain_sequence IS DISTINCT FROM ordered.sequence;
END
$astralplane_audit_backfill$
""".strip(),
    "ALTER TABLE audit_events ALTER COLUMN chain_sequence SET NOT NULL",
    """
CREATE UNIQUE INDEX IF NOT EXISTS uq_audit_events_actor_sequence
ON audit_events (actor_user_id, chain_sequence)
""".strip(),
    """
CREATE OR REPLACE FUNCTION audit_events_assign_chain_sequence()
RETURNS trigger AS $astralplane_audit_sequence$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('audit_events:' || NEW.actor_user_id));
    IF NEW.chain_sequence IS NULL THEN
        SELECT COALESCE(MAX(chain_sequence), 0) + 1
        INTO NEW.chain_sequence
        FROM audit_events
        WHERE actor_user_id = NEW.actor_user_id;
    END IF;
    RETURN NEW;
END
$astralplane_audit_sequence$ LANGUAGE plpgsql
""".strip(),
    "DROP TRIGGER IF EXISTS audit_events_assign_sequence ON audit_events",
    """
CREATE TRIGGER audit_events_assign_sequence
BEFORE INSERT ON audit_events
FOR EACH ROW EXECUTE FUNCTION audit_events_assign_chain_sequence()
""".strip(),
    """
DO $astralplane_audit_constraints$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'audit_events'::regclass
          AND conname = 'audit_events_chain_sequence_check'
    ) THEN
        ALTER TABLE audit_events
        ADD CONSTRAINT audit_events_chain_sequence_check
        CHECK (chain_sequence > 0) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'audit_events'::regclass
          AND conname = 'audit_events_digest_length_check'
    ) THEN
        ALTER TABLE audit_events
        ADD CONSTRAINT audit_events_digest_length_check
        CHECK (octet_length(prev_hash) = 32 AND octet_length(entry_hash) = 32)
        NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'audit_events'::regclass
          AND conname = 'audit_events_schema_version_check'
    ) THEN
        ALTER TABLE audit_events
        ADD CONSTRAINT audit_events_schema_version_check
        CHECK (schema_version IN (1, 2)) NOT VALID;
    END IF;
    ALTER TABLE audit_events
        VALIDATE CONSTRAINT audit_events_chain_sequence_check;
    ALTER TABLE audit_events
        VALIDATE CONSTRAINT audit_events_digest_length_check;
    ALTER TABLE audit_events
        VALIDATE CONSTRAINT audit_events_schema_version_check;
END
$astralplane_audit_constraints$
""".strip(),
    """
CREATE TABLE IF NOT EXISTS astralplane_outbox (
    entry_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    canonical_payload BYTEA NOT NULL,
    payload_sha256 CHAR(64) NOT NULL
        CONSTRAINT astralplane_outbox_payload_digest_check
        CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    idempotency_key TEXT NOT NULL UNIQUE,
    available_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'retry', 'claimed', 'succeeded', 'dead_letter')),
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    last_error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT astralplane_outbox_lease_shape_check CHECK (
        (
            status = 'claimed'
            AND lease_owner IS NOT NULL
            AND lease_expires_at IS NOT NULL
        )
        OR (
            status <> 'claimed'
            AND lease_owner IS NULL
            AND lease_expires_at IS NULL
        )
    ),
    CONSTRAINT astralplane_outbox_error_shape_check CHECK (
        (
            status IN ('pending', 'claimed', 'succeeded')
            AND last_error_code IS NULL
        )
        OR (
            status IN ('retry', 'dead_letter')
            AND last_error_code IS NOT NULL
        )
    )
)
""".strip(),
)

PLANE_SCHEMA_074_STATEMENTS: Final = (
    """
CREATE OR REPLACE FUNCTION astralplane_identifier_is_canonical(candidate TEXT)
RETURNS BOOLEAN AS $astralplane_identifier$
    SELECT candidate ~ '^[A-Za-z0-9]([A-Za-z0-9._:@/-]{0,126}[A-Za-z0-9])?$'
$astralplane_identifier$ LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE
""".strip(),
    """
CREATE OR REPLACE FUNCTION astralplane_capabilities_are_canonical(candidate TEXT[])
RETURNS BOOLEAN AS $astralplane_capabilities$
    SELECT cardinality(candidate) > 0
       AND array_ndims(candidate) = 1
       AND array_lower(candidate, 1) = 1
       AND NOT EXISTS (
            SELECT 1
            FROM generate_subscripts(candidate, 1) AS item(position)
            WHERE NOT astralplane_identifier_is_canonical(candidate[position])
               OR (
                    position > 1
                    AND candidate[position - 1] COLLATE "C"
                        >= candidate[position] COLLATE "C"
               )
       )
$astralplane_capabilities$ LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE
""".strip(),
    """
CREATE TABLE IF NOT EXISTS astralplane_authority_binding (
    binding_id VARCHAR(128) NOT NULL,
    owner_id VARCHAR(128) NOT NULL,
    agent_id VARCHAR(128) NOT NULL,
    runtime_id VARCHAR(128) NOT NULL,
    runtime_generation BIGINT NOT NULL,
    population TEXT NOT NULL,
    tenant_id VARCHAR(128) NOT NULL,
    envelope_id VARCHAR(128) NOT NULL,
    warden_id VARCHAR(128) NOT NULL,
    lease_id VARCHAR(128) NOT NULL,
    lineage_id VARCHAR(128) NOT NULL,
    subject_id VARCHAR(128) NOT NULL,
    policy_digest CHAR(71) NOT NULL,
    machine_digest CHAR(71) NOT NULL,
    config_epoch BIGINT NOT NULL,
    capabilities TEXT[] NOT NULL,
    lease_sequence BIGINT NOT NULL,
    lease_expires_at_ns BIGINT NOT NULL,
    state TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    version BIGINT NOT NULL,
    CONSTRAINT astralplane_authority_binding_pk PRIMARY KEY (binding_id),
    CONSTRAINT astralplane_authority_binding_owner_key
        UNIQUE (owner_id, binding_id),
    CONSTRAINT astralplane_authority_binding_runtime_key
        UNIQUE (owner_id, agent_id, runtime_id, runtime_generation),
    CONSTRAINT astralplane_authority_binding_identity_check CHECK (
        astralplane_identifier_is_canonical(binding_id)
        AND astralplane_identifier_is_canonical(owner_id)
        AND astralplane_identifier_is_canonical(agent_id)
        AND astralplane_identifier_is_canonical(runtime_id)
        AND astralplane_identifier_is_canonical(tenant_id)
        AND astralplane_identifier_is_canonical(envelope_id)
        AND astralplane_identifier_is_canonical(warden_id)
        AND astralplane_identifier_is_canonical(lease_id)
        AND astralplane_identifier_is_canonical(lineage_id)
        AND astralplane_identifier_is_canonical(subject_id)
    ),
    CONSTRAINT astralplane_authority_binding_population_check
        CHECK (population IN ('server_dynamic', 'byo_user')),
    CONSTRAINT astralplane_authority_binding_digest_check CHECK (
        policy_digest ~ '^sha256:[0-9a-f]{64}$'
        AND machine_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    CONSTRAINT astralplane_authority_binding_capabilities_check
        CHECK (astralplane_capabilities_are_canonical(capabilities)),
    CONSTRAINT astralplane_authority_binding_numeric_check CHECK (
        runtime_generation > 0
        AND config_epoch > 0
        AND lease_sequence >= 0
        AND lease_expires_at_ns >= 0
        AND version >= 0
    ),
    CONSTRAINT astralplane_authority_binding_state_check CHECK (
        state IN (
            'provisioning', 'active', 'quiescent', 'closing', 'closed',
            'revoking', 'revoked', 'reconciling', 'expired'
        )
    ),
    CONSTRAINT astralplane_authority_binding_time_check
        CHECK (updated_at >= created_at),
    CONSTRAINT astralplane_authority_binding_remote_state_check CHECK (
        (
            state IN ('provisioning', 'closed')
            AND warden_id ~ '^pending:warden:[0-9a-f]{32}$'
            AND lease_id ~ '^pending:lease:[0-9a-f]{32}$'
            AND lineage_id ~ '^pending:lineage:[0-9a-f]{32}$'
            AND subject_id ~ '^pending:subject:[0-9a-f]{32}$'
            AND lease_sequence = 0
            AND lease_expires_at_ns = 0
        )
        OR
        (
            state <> 'provisioning'
            AND warden_id !~ '^pending:'
            AND lease_id !~ '^pending:'
            AND lineage_id !~ '^pending:'
            AND subject_id !~ '^pending:'
            AND lease_expires_at_ns > 0
        )
    )
)
""".strip(),
    """
DO $astralplane_authority_binding_constraints$
BEGIN
    ALTER TABLE astralplane_authority_binding
        DROP CONSTRAINT IF EXISTS astralplane_authority_binding_numeric_check;
    ALTER TABLE astralplane_authority_binding
        ADD CONSTRAINT astralplane_authority_binding_numeric_check CHECK (
            runtime_generation > 0
            AND config_epoch > 0
            AND lease_sequence >= 0
            AND lease_expires_at_ns >= 0
            AND version >= 0
        ) NOT VALID;

    ALTER TABLE astralplane_authority_binding
        DROP CONSTRAINT IF EXISTS astralplane_authority_binding_remote_state_check;
    ALTER TABLE astralplane_authority_binding
        ADD CONSTRAINT astralplane_authority_binding_remote_state_check CHECK (
            (
                state IN ('provisioning', 'closed')
                AND warden_id ~ '^pending:warden:[0-9a-f]{32}$'
                AND lease_id ~ '^pending:lease:[0-9a-f]{32}$'
                AND lineage_id ~ '^pending:lineage:[0-9a-f]{32}$'
                AND subject_id ~ '^pending:subject:[0-9a-f]{32}$'
                AND lease_sequence = 0
                AND lease_expires_at_ns = 0
            )
            OR
            (
                state <> 'provisioning'
                AND warden_id !~ '^pending:'
                AND lease_id !~ '^pending:'
                AND lineage_id !~ '^pending:'
                AND subject_id !~ '^pending:'
                AND lease_expires_at_ns > 0
            )
        ) NOT VALID;

    ALTER TABLE astralplane_authority_binding
        VALIDATE CONSTRAINT astralplane_authority_binding_numeric_check;
    ALTER TABLE astralplane_authority_binding
        VALIDATE CONSTRAINT astralplane_authority_binding_remote_state_check;
END
$astralplane_authority_binding_constraints$
""".strip(),
    """
CREATE UNIQUE INDEX IF NOT EXISTS uq_astralplane_authority_binding_nonterminal
ON astralplane_authority_binding (owner_id, agent_id, population)
WHERE state IN (
    'provisioning', 'active', 'quiescent', 'closing', 'revoking', 'reconciling'
)
""".strip(),
    """
CREATE TABLE IF NOT EXISTS astralplane_authority_lifecycle_operation (
    operation_id VARCHAR(128) NOT NULL,
    owner_id VARCHAR(128) NOT NULL,
    binding_id VARCHAR(128) NOT NULL,
    kind TEXT NOT NULL,
    expected_binding_version BIGINT NOT NULL,
    expected_lease_sequence BIGINT,
    request_fingerprint CHAR(64) NOT NULL,
    status TEXT NOT NULL,
    remote_request_id VARCHAR(128) NOT NULL,
    result_digest CHAR(64),
    error_code VARCHAR(128),
    attempt_count INTEGER NOT NULL,
    next_attempt_at TIMESTAMPTZ,
    last_attempt_at TIMESTAMPTZ,
    reconciled_at TIMESTAMPTZ,
    reconciliation_digest CHAR(64),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    version BIGINT NOT NULL,
    CONSTRAINT astralplane_authority_lifecycle_operation_pk PRIMARY KEY (operation_id),
    CONSTRAINT astralplane_authority_lifecycle_owner_key
        UNIQUE (owner_id, operation_id),
    CONSTRAINT astralplane_authority_lifecycle_binding_fk
        FOREIGN KEY (owner_id, binding_id)
        REFERENCES astralplane_authority_binding (owner_id, binding_id),
    CONSTRAINT astralplane_authority_lifecycle_identity_check CHECK (
        astralplane_identifier_is_canonical(operation_id)
        AND astralplane_identifier_is_canonical(owner_id)
        AND astralplane_identifier_is_canonical(binding_id)
        AND astralplane_identifier_is_canonical(remote_request_id)
        AND remote_request_id = operation_id
        AND (error_code IS NULL OR astralplane_identifier_is_canonical(error_code))
    ),
    CONSTRAINT astralplane_authority_lifecycle_kind_check CHECK (
        kind IN ('provision', 'spawn', 'renew', 'quiesce', 'resume', 'close', 'revoke', 'reconcile')
    ),
    CONSTRAINT astralplane_authority_lifecycle_numeric_check CHECK (
        expected_binding_version >= 0
        AND (expected_lease_sequence IS NULL OR expected_lease_sequence >= 0)
        AND attempt_count >= 0
        AND version >= 0
    ),
    CONSTRAINT astralplane_authority_lifecycle_digest_check CHECK (
        request_fingerprint ~ '^[0-9a-f]{64}$'
        AND (result_digest IS NULL OR result_digest ~ '^[0-9a-f]{64}$')
        AND (
            reconciliation_digest IS NULL
            OR reconciliation_digest ~ '^[0-9a-f]{64}$'
        )
    ),
    CONSTRAINT astralplane_authority_lifecycle_status_check CHECK (
        status IN ('pending', 'in_flight', 'succeeded', 'failed', 'uncertain', 'reconciled')
    ),
    CONSTRAINT astralplane_authority_lifecycle_attempt_check CHECK (
        ((attempt_count = 0 AND last_attempt_at IS NULL)
            OR (attempt_count > 0 AND last_attempt_at IS NOT NULL))
        AND (status = 'pending' OR attempt_count > 0)
    ),
    CONSTRAINT astralplane_authority_lifecycle_result_check CHECK (
        (status IN ('pending', 'in_flight') AND result_digest IS NULL AND error_code IS NULL)
        OR (status = 'succeeded' AND result_digest IS NOT NULL AND error_code IS NULL)
        OR (status IN ('failed', 'uncertain') AND result_digest IS NULL AND error_code IS NOT NULL)
        OR (
            status = 'reconciled'
            AND ((result_digest IS NOT NULL) <> (error_code IS NOT NULL))
        )
    ),
    CONSTRAINT astralplane_authority_lifecycle_reconciliation_check CHECK (
        (
            status = 'reconciled'
            AND reconciled_at IS NOT NULL
            AND reconciliation_digest IS NOT NULL
        )
        OR (
            status <> 'reconciled'
            AND reconciled_at IS NULL
            AND reconciliation_digest IS NULL
        )
    ),
    CONSTRAINT astralplane_authority_lifecycle_time_check CHECK (
        updated_at >= created_at
        AND (next_attempt_at IS NULL OR next_attempt_at >= created_at)
        AND (last_attempt_at IS NULL OR last_attempt_at >= created_at)
        AND (reconciled_at IS NULL OR reconciled_at >= created_at)
        AND (
            status NOT IN ('succeeded', 'failed', 'reconciled')
            OR next_attempt_at IS NULL
        )
    )
)
""".strip(),
    """
CREATE INDEX IF NOT EXISTS idx_astralplane_authority_lifecycle_work
ON astralplane_authority_lifecycle_operation (
    owner_id, binding_id, status, next_attempt_at, operation_id
)
WHERE status IN ('pending', 'in_flight', 'uncertain')
""".strip(),
    """
CREATE TABLE IF NOT EXISTS astralplane_protected_effect_operation (
    operation_id VARCHAR(128) NOT NULL,
    owner_id VARCHAR(128) NOT NULL,
    agent_id VARCHAR(128) NOT NULL,
    binding_id VARCHAR(128) NOT NULL,
    tool_id VARCHAR(128) NOT NULL,
    astral_scope TEXT NOT NULL,
    lets_capability VARCHAR(128) NOT NULL,
    lets_transition VARCHAR(128) NOT NULL,
    executor_audience VARCHAR(128) NOT NULL,
    nonce VARCHAR(256) NOT NULL,
    effect_digest CHAR(64) NOT NULL,
    expected_sequence BIGINT NOT NULL,
    audit_correlation_id VARCHAR(128) NOT NULL,
    status TEXT NOT NULL,
    receipt_id VARCHAR(128),
    receipt_digest CHAR(64),
    effect_result_digest CHAR(64),
    error_code VARCHAR(128),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    version BIGINT NOT NULL,
    CONSTRAINT astralplane_protected_effect_operation_pk PRIMARY KEY (operation_id),
    CONSTRAINT astralplane_protected_effect_owner_key UNIQUE (owner_id, operation_id),
    CONSTRAINT astralplane_protected_effect_receipt_key UNIQUE (receipt_id),
    CONSTRAINT astralplane_protected_effect_nonce_key
        UNIQUE (owner_id, binding_id, executor_audience, nonce),
    CONSTRAINT astralplane_protected_effect_binding_fk
        FOREIGN KEY (owner_id, binding_id)
        REFERENCES astralplane_authority_binding (owner_id, binding_id),
    CONSTRAINT astralplane_protected_effect_identity_check CHECK (
        astralplane_identifier_is_canonical(operation_id)
        AND astralplane_identifier_is_canonical(owner_id)
        AND astralplane_identifier_is_canonical(agent_id)
        AND astralplane_identifier_is_canonical(binding_id)
        AND astralplane_identifier_is_canonical(tool_id)
        AND astralplane_identifier_is_canonical(lets_capability)
        AND astralplane_identifier_is_canonical(lets_transition)
        AND astralplane_identifier_is_canonical(executor_audience)
        AND astralplane_identifier_is_canonical(audit_correlation_id)
        AND (receipt_id IS NULL OR astralplane_identifier_is_canonical(receipt_id))
        AND (error_code IS NULL OR astralplane_identifier_is_canonical(error_code))
        AND length(nonce) BETWEEN 16 AND 256
        AND nonce = btrim(nonce)
        AND nonce !~ '[[:cntrl:]]'
    ),
    CONSTRAINT astralplane_protected_effect_scope_check CHECK (
        astral_scope IN (
            'tools:read', 'tools:write', 'tools:search',
            'tools:system', 'tools:files', 'tools:execute'
        )
    ),
    CONSTRAINT astralplane_protected_effect_digest_check CHECK (
        effect_digest ~ '^[0-9a-f]{64}$'
        AND (receipt_digest IS NULL OR receipt_digest ~ '^[0-9a-f]{64}$')
        AND (
            effect_result_digest IS NULL
            OR effect_result_digest ~ '^[0-9a-f]{64}$'
        )
    ),
    CONSTRAINT astralplane_protected_effect_numeric_check
        CHECK (expected_sequence >= 0 AND version >= 0),
    CONSTRAINT astralplane_protected_effect_status_check CHECK (
        status IN (
            'created', 'astral_authorized', 'lets_pending', 'receipt_received',
            'receipt_claimed', 'executing', 'succeeded', 'denied', 'failed_closed',
            'effect_failed', 'outcome_uncertain'
        )
    ),
    CONSTRAINT astralplane_protected_effect_receipt_shape_check CHECK (
        ((receipt_id IS NULL) = (receipt_digest IS NULL))
        AND (
            status NOT IN (
                'receipt_received', 'receipt_claimed', 'executing', 'succeeded',
                'effect_failed', 'outcome_uncertain'
            )
            OR receipt_id IS NOT NULL
        )
        AND (
            status NOT IN ('created', 'astral_authorized', 'lets_pending')
            OR receipt_id IS NULL
        )
    ),
    CONSTRAINT astralplane_protected_effect_result_shape_check CHECK (
        ((status IN ('denied', 'failed_closed', 'effect_failed', 'outcome_uncertain'))
            = (error_code IS NOT NULL))
        AND ((status IN ('succeeded', 'effect_failed')) = (effect_result_digest IS NOT NULL))
    ),
    CONSTRAINT astralplane_protected_effect_time_check
        CHECK (updated_at >= created_at)
)
""".strip(),
    """
CREATE INDEX IF NOT EXISTS idx_astralplane_protected_effect_work
ON astralplane_protected_effect_operation (owner_id, binding_id, status, updated_at, operation_id)
WHERE status NOT IN ('succeeded', 'denied', 'failed_closed', 'effect_failed')
""".strip(),
    """
CREATE INDEX IF NOT EXISTS idx_astralplane_protected_effect_audit
ON astralplane_protected_effect_operation (owner_id, audit_correlation_id, operation_id)
""".strip(),
    """
CREATE TABLE IF NOT EXISTS astralplane_receipt_sequence_watermark (
    warden_id VARCHAR(128) NOT NULL,
    lease_id VARCHAR(128) NOT NULL,
    audience VARCHAR(128) NOT NULL,
    last_sequence BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    expires_at_ns BIGINT NOT NULL,
    version BIGINT NOT NULL,
    CONSTRAINT astralplane_receipt_watermark_pk PRIMARY KEY (warden_id, lease_id, audience),
    CONSTRAINT astralplane_receipt_watermark_identity_check CHECK (
        astralplane_identifier_is_canonical(warden_id)
        AND astralplane_identifier_is_canonical(lease_id)
        AND astralplane_identifier_is_canonical(audience)
    ),
    CONSTRAINT astralplane_receipt_watermark_numeric_check
        CHECK (last_sequence > 0 AND expires_at_ns > 0 AND version >= 0)
)
""".strip(),
    """
CREATE OR REPLACE FUNCTION astralplane_receipt_watermark_require_advance()
RETURNS trigger AS $astralplane_watermark_advance$
BEGIN
    IF ROW(NEW.warden_id, NEW.lease_id, NEW.audience)
        IS DISTINCT FROM ROW(OLD.warden_id, OLD.lease_id, OLD.audience)
        OR NEW.last_sequence <= OLD.last_sequence
        OR NEW.version <> OLD.version + 1
        OR NEW.updated_at < OLD.updated_at
    THEN
        RAISE EXCEPTION 'receipt watermark update must strictly advance its fenced sequence'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$astralplane_watermark_advance$ LANGUAGE plpgsql
""".strip(),
    (
        "DROP TRIGGER IF EXISTS astralplane_receipt_watermark_advance "
        "ON astralplane_receipt_sequence_watermark"
    ),
    """
CREATE TRIGGER astralplane_receipt_watermark_advance
BEFORE UPDATE ON astralplane_receipt_sequence_watermark
FOR EACH ROW EXECUTE FUNCTION astralplane_receipt_watermark_require_advance()
""".strip(),
    """
CREATE TABLE IF NOT EXISTS astralplane_receipt_claim (
    receipt_id VARCHAR(128) NOT NULL,
    operation_id VARCHAR(128) NOT NULL,
    owner_id VARCHAR(128) NOT NULL,
    binding_id VARCHAR(128) NOT NULL,
    tenant_id VARCHAR(128) NOT NULL,
    envelope_id VARCHAR(128) NOT NULL,
    warden_id VARCHAR(128) NOT NULL,
    lease_id VARCHAR(128) NOT NULL,
    subject_id VARCHAR(128) NOT NULL,
    lineage_id VARCHAR(128) NOT NULL,
    policy_digest CHAR(71) NOT NULL,
    machine_digest CHAR(71) NOT NULL,
    config_epoch BIGINT NOT NULL,
    audience VARCHAR(128) NOT NULL,
    transition VARCHAR(128) NOT NULL,
    nonce VARCHAR(128) NOT NULL,
    resulting_sequence BIGINT NOT NULL,
    evidence_digest CHAR(71),
    issued_at_ns BIGINT NOT NULL,
    expires_at_ns BIGINT NOT NULL,
    claimed_at TIMESTAMPTZ NOT NULL,
    canonical_digest CHAR(64) NOT NULL,
    anchor_format TEXT NOT NULL,
    anchor_executor_policy_sha256 CHAR(64) NOT NULL,
    anchor_trust_registry_sha256 CHAR(64) NOT NULL,
    anchor_schema_version BIGINT NOT NULL,
    anchor_database_instance_id CHAR(64) NOT NULL,
    anchor_claim_sequence BIGINT NOT NULL,
    anchor_claim_digest CHAR(64) NOT NULL,
    anchor_clock_floor_ns BIGINT NOT NULL,
    anchor_confirmed_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT astralplane_receipt_claim_pk PRIMARY KEY (receipt_id),
    CONSTRAINT astralplane_receipt_claim_operation_key UNIQUE (operation_id),
    CONSTRAINT astralplane_receipt_claim_nonce_key
        UNIQUE (tenant_id, envelope_id, audience, nonce),
    CONSTRAINT astralplane_receipt_claim_sequence_key
        UNIQUE (warden_id, lease_id, audience, resulting_sequence),
    CONSTRAINT astralplane_receipt_claim_digest_key UNIQUE (canonical_digest),
    CONSTRAINT astralplane_receipt_claim_binding_fk
        FOREIGN KEY (owner_id, binding_id)
        REFERENCES astralplane_authority_binding (owner_id, binding_id),
    CONSTRAINT astralplane_receipt_claim_effect_fk
        FOREIGN KEY (owner_id, operation_id)
        REFERENCES astralplane_protected_effect_operation (owner_id, operation_id),
    CONSTRAINT astralplane_receipt_claim_identity_check CHECK (
        astralplane_identifier_is_canonical(receipt_id)
        AND astralplane_identifier_is_canonical(operation_id)
        AND astralplane_identifier_is_canonical(owner_id)
        AND astralplane_identifier_is_canonical(binding_id)
        AND astralplane_identifier_is_canonical(tenant_id)
        AND astralplane_identifier_is_canonical(envelope_id)
        AND astralplane_identifier_is_canonical(warden_id)
        AND astralplane_identifier_is_canonical(lease_id)
        AND astralplane_identifier_is_canonical(subject_id)
        AND astralplane_identifier_is_canonical(lineage_id)
        AND astralplane_identifier_is_canonical(audience)
        AND astralplane_identifier_is_canonical(transition)
        AND astralplane_identifier_is_canonical(nonce)
    ),
    CONSTRAINT astralplane_receipt_claim_lets_digest_check CHECK (
        policy_digest ~ '^sha256:[0-9a-f]{64}$'
        AND machine_digest ~ '^sha256:[0-9a-f]{64}$'
        AND (evidence_digest IS NULL OR evidence_digest ~ '^sha256:[0-9a-f]{64}$')
    ),
    CONSTRAINT astralplane_receipt_claim_numeric_check CHECK (
        config_epoch > 0
        AND resulting_sequence > 0
        AND issued_at_ns >= 0
        AND expires_at_ns > issued_at_ns
    ),
    CONSTRAINT astralplane_receipt_claim_digest_check CHECK (
        canonical_digest ~ '^[0-9a-f]{64}$'
        AND anchor_executor_policy_sha256 ~ '^[0-9a-f]{64}$'
        AND anchor_trust_registry_sha256 ~ '^[0-9a-f]{64}$'
        AND anchor_database_instance_id ~ '^[0-9a-f]{64}$'
        AND anchor_claim_digest ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT astralplane_receipt_claim_anchor_check CHECK (
        anchor_format = 'LETS-EXECUTOR-AUTHORITY-ANCHOR/1'
        AND anchor_schema_version > 0
        AND anchor_claim_sequence > 0
        AND anchor_clock_floor_ns >= 0
        AND anchor_confirmed_at >= claimed_at
    )
)
""".strip(),
    """
CREATE INDEX IF NOT EXISTS idx_astralplane_receipt_claim_binding
ON astralplane_receipt_claim (owner_id, binding_id, claimed_at, receipt_id)
""".strip(),
    """
DO $astralplane_authority_outbox_constraints$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'astralplane_outbox'::regclass
          AND conname = 'astralplane_outbox_payload_size_check'
    ) THEN
        ALTER TABLE astralplane_outbox
        ADD CONSTRAINT astralplane_outbox_payload_size_check
        CHECK (octet_length(canonical_payload) BETWEEN 1 AND 1048576) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'astralplane_outbox'::regclass
          AND conname = 'astralplane_outbox_topic_format_check'
    ) THEN
        ALTER TABLE astralplane_outbox
        ADD CONSTRAINT astralplane_outbox_topic_format_check
        CHECK (topic ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$') NOT VALID;
    END IF;
    ALTER TABLE astralplane_outbox
        VALIDATE CONSTRAINT astralplane_outbox_payload_size_check;
    ALTER TABLE astralplane_outbox
        VALIDATE CONSTRAINT astralplane_outbox_topic_format_check;
END
$astralplane_authority_outbox_constraints$
""".strip(),
    """
CREATE INDEX IF NOT EXISTS idx_astralplane_outbox_authority_pending
ON astralplane_outbox (topic, available_at, entry_id)
WHERE status IN ('pending', 'retry') AND topic LIKE 'authority.%'
""".strip(),
    """
DO $astralplane_authority_postcondition$
DECLARE
    invalid_objects TEXT;
BEGIN
    SELECT string_agg(required.table_name || '.' || required.column_name, ', ')
    INTO invalid_objects
    FROM (
        VALUES
            ('astralplane_authority_binding', 'binding_id', 'varchar', TRUE),
            ('astralplane_authority_binding', 'runtime_generation', 'int8', TRUE),
            ('astralplane_authority_binding', 'capabilities', '_text', TRUE),
            ('astralplane_authority_binding', 'state', 'text', TRUE),
            ('astralplane_authority_lifecycle_operation', 'operation_id', 'varchar', TRUE),
            ('astralplane_authority_lifecycle_operation', 'request_fingerprint', 'bpchar', TRUE),
            ('astralplane_authority_lifecycle_operation', 'reconciliation_digest', 'bpchar', FALSE),
            ('astralplane_protected_effect_operation', 'operation_id', 'varchar', TRUE),
            ('astralplane_protected_effect_operation', 'nonce', 'varchar', TRUE),
            ('astralplane_protected_effect_operation', 'effect_digest', 'bpchar', TRUE),
            ('astralplane_receipt_sequence_watermark', 'last_sequence', 'int8', TRUE),
            ('astralplane_receipt_sequence_watermark', 'version', 'int8', TRUE),
            ('astralplane_receipt_claim', 'receipt_id', 'varchar', TRUE),
            ('astralplane_receipt_claim', 'canonical_digest', 'bpchar', TRUE),
            ('astralplane_receipt_claim', 'anchor_claim_digest', 'bpchar', TRUE),
            ('astralplane_receipt_claim', 'anchor_clock_floor_ns', 'int8', TRUE),
            ('astralplane_outbox', 'topic', 'text', TRUE),
            ('astralplane_outbox', 'canonical_payload', 'bytea', TRUE),
            ('astralplane_outbox', 'payload_sha256', 'bpchar', TRUE)
    ) AS required(table_name, column_name, type_name, not_null)
    LEFT JOIN pg_attribute AS attribute
      ON attribute.attrelid = to_regclass(required.table_name)
     AND attribute.attname = required.column_name
     AND attribute.attnum > 0
     AND NOT attribute.attisdropped
    LEFT JOIN pg_type AS data_type ON data_type.oid = attribute.atttypid
    WHERE attribute.attname IS NULL
       OR data_type.typname <> required.type_name
       OR attribute.attnotnull <> required.not_null;
    IF invalid_objects IS NOT NULL THEN
        RAISE EXCEPTION 'AstralPlane authority columns are missing or incompatible: %',
            invalid_objects USING ERRCODE = '42P16';
    END IF;

    SELECT string_agg(required.constraint_name, ', ')
    INTO invalid_objects
    FROM (
        VALUES
            ('astralplane_authority_binding', 'astralplane_authority_binding_pk', 'p'),
            ('astralplane_authority_binding', 'astralplane_authority_binding_owner_key', 'u'),
            ('astralplane_authority_binding', 'astralplane_authority_binding_runtime_key', 'u'),
            ('astralplane_authority_binding', 'astralplane_authority_binding_identity_check', 'c'),
            ('astralplane_authority_binding', 'astralplane_authority_binding_numeric_check', 'c'),
            (
                'astralplane_authority_binding',
                'astralplane_authority_binding_remote_state_check',
                'c'
            ),
            (
                'astralplane_authority_binding',
                'astralplane_authority_binding_capabilities_check',
                'c'
            ),
            (
                'astralplane_authority_lifecycle_operation',
                'astralplane_authority_lifecycle_operation_pk',
                'p'
            ),
            (
                'astralplane_authority_lifecycle_operation',
                'astralplane_authority_lifecycle_binding_fk',
                'f'
            ),
            (
                'astralplane_authority_lifecycle_operation',
                'astralplane_authority_lifecycle_result_check',
                'c'
            ),
            (
                'astralplane_protected_effect_operation',
                'astralplane_protected_effect_operation_pk',
                'p'
            ),
            (
                'astralplane_protected_effect_operation',
                'astralplane_protected_effect_binding_fk',
                'f'
            ),
            (
                'astralplane_protected_effect_operation',
                'astralplane_protected_effect_nonce_key',
                'u'
            ),
            (
                'astralplane_protected_effect_operation',
                'astralplane_protected_effect_receipt_shape_check',
                'c'
            ),
            ('astralplane_receipt_sequence_watermark', 'astralplane_receipt_watermark_pk', 'p'),
            ('astralplane_receipt_claim', 'astralplane_receipt_claim_pk', 'p'),
            ('astralplane_receipt_claim', 'astralplane_receipt_claim_nonce_key', 'u'),
            ('astralplane_receipt_claim', 'astralplane_receipt_claim_sequence_key', 'u'),
            ('astralplane_receipt_claim', 'astralplane_receipt_claim_binding_fk', 'f'),
            ('astralplane_receipt_claim', 'astralplane_receipt_claim_effect_fk', 'f'),
            ('astralplane_receipt_claim', 'astralplane_receipt_claim_anchor_check', 'c'),
            ('astralplane_outbox', 'astralplane_outbox_payload_digest_check', 'c'),
            ('astralplane_outbox', 'astralplane_outbox_payload_size_check', 'c'),
            ('astralplane_outbox', 'astralplane_outbox_topic_format_check', 'c')
    ) AS required(table_name, constraint_name, constraint_type)
    LEFT JOIN pg_constraint AS constraint_record
      ON constraint_record.conrelid = to_regclass(required.table_name)
     AND constraint_record.conname = required.constraint_name
     AND constraint_record.contype = required.constraint_type::"char"
     AND constraint_record.convalidated
    WHERE constraint_record.oid IS NULL;
    IF invalid_objects IS NOT NULL THEN
        RAISE EXCEPTION 'AstralPlane authority constraints are missing: %',
            invalid_objects USING ERRCODE = '42P16';
    END IF;

    SELECT string_agg(required.index_name, ', ')
    INTO invalid_objects
    FROM (
        VALUES
            ('uq_astralplane_authority_binding_nonterminal', TRUE, TRUE),
            ('idx_astralplane_authority_lifecycle_work', FALSE, TRUE),
            ('idx_astralplane_protected_effect_work', FALSE, TRUE),
            ('idx_astralplane_protected_effect_audit', FALSE, FALSE),
            ('idx_astralplane_receipt_claim_binding', FALSE, FALSE),
            ('idx_astralplane_outbox_authority_pending', FALSE, TRUE)
    ) AS required(index_name, is_unique, is_partial)
    LEFT JOIN pg_class AS index_record
      ON index_record.oid = to_regclass(required.index_name)
     AND index_record.relkind = 'i'
    LEFT JOIN pg_index AS index_state
      ON index_state.indexrelid = index_record.oid
     AND index_state.indisvalid
     AND index_state.indisready
     AND index_state.indisunique = required.is_unique
     AND (index_state.indpred IS NOT NULL) = required.is_partial
    WHERE index_state.indexrelid IS NULL;
    IF invalid_objects IS NOT NULL THEN
        RAISE EXCEPTION 'AstralPlane authority indexes are missing or invalid: %',
            invalid_objects USING ERRCODE = '42P16';
    END IF;

    IF to_regprocedure('astralplane_identifier_is_canonical(text)') IS NULL
       OR to_regprocedure('astralplane_capabilities_are_canonical(text[])') IS NULL
       OR to_regprocedure('astralplane_receipt_watermark_require_advance()') IS NULL
    THEN
        RAISE EXCEPTION 'AstralPlane authority validation functions are missing'
            USING ERRCODE = '42P16';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgrelid = 'astralplane_receipt_sequence_watermark'::regclass
          AND tgname = 'astralplane_receipt_watermark_advance'
          AND tgenabled = 'O'
          AND NOT tgisinternal
    ) THEN
        RAISE EXCEPTION 'AstralPlane receipt watermark trigger is missing or disabled'
            USING ERRCODE = '42P16';
    END IF;
END
$astralplane_authority_postcondition$
""".strip(),
)

_PLANE_SCHEMA_067_REMAINDER: Final = (
    """
CREATE INDEX IF NOT EXISTS idx_astralplane_outbox_claim_v2
ON astralplane_outbox (topic, available_at, entry_id)
WHERE status IN ('pending', 'retry')
""".strip(),
    """
CREATE INDEX IF NOT EXISTS idx_astralplane_outbox_lease
ON astralplane_outbox (lease_expires_at, entry_id)
WHERE status = 'claimed'
""".strip(),
    """
CREATE TABLE IF NOT EXISTS audit_retention_anchor (
    anchor_id TEXT NOT NULL UNIQUE,
    owner_or_chain TEXT NOT NULL,
    first_retained_sequence BIGINT NOT NULL CHECK (first_retained_sequence > 1),
    previous_entry_digest BYTEA NOT NULL
        CONSTRAINT audit_retention_previous_digest_check
        CHECK (octet_length(previous_entry_digest) = 32),
    retention_policy_digest BYTEA NOT NULL
        CONSTRAINT audit_retention_policy_digest_check
        CHECK (octet_length(retention_policy_digest) = 32),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    key_id TEXT NOT NULL,
    signature_or_mac BYTEA NOT NULL,
    PRIMARY KEY (owner_or_chain, first_retained_sequence)
)
""".strip(),
    """
CREATE TABLE IF NOT EXISTS astralplane_purge_tombstone (
    tombstone_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    object_kind TEXT NOT NULL
        CHECK (object_kind IN ('attachment', 'artifact', 'knowledge', 'generated_agent')),
    object_id TEXT NOT NULL,
    storage_key TEXT NOT NULL,
    storage_locator_sha256 CHAR(64) NOT NULL
        CONSTRAINT astralplane_purge_locator_digest_check
        CHECK (storage_locator_sha256 ~ '^[0-9a-f]{64}$'),
    requested_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'purged', 'failed', 'manual_review')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
    available_at TIMESTAMPTZ NOT NULL,
    verified_absent_at TIMESTAMPTZ,
    last_error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT astralplane_purge_state_shape_check CHECK (
        (
            status = 'pending'
            AND verified_absent_at IS NULL
            AND last_error_code IS NULL
        )
        OR (
            status = 'purged'
            AND verified_absent_at IS NOT NULL
            AND last_error_code IS NULL
        )
        OR (
            status IN ('failed', 'manual_review')
            AND verified_absent_at IS NULL
            AND last_error_code IS NOT NULL
        )
    ),
    UNIQUE (owner_id, object_kind, object_id)
)
""".strip(),
    """
CREATE INDEX IF NOT EXISTS idx_astralplane_purge_pending
ON astralplane_purge_tombstone (status, available_at, requested_at, tombstone_id)
WHERE status <> 'purged'
""".strip(),
    """
CREATE INDEX IF NOT EXISTS idx_astralplane_purge_owner_pending
ON astralplane_purge_tombstone (owner_id, requested_at, tombstone_id)
WHERE status <> 'purged'
""".strip(),
    """
CREATE TABLE IF NOT EXISTS astralplane_reconciliation_marker (
    schema_revision TEXT NOT NULL,
    plan_digest CHAR(64) NOT NULL
        CONSTRAINT astralplane_reconciliation_plan_digest_check
        CHECK (plan_digest ~ '^[0-9a-f]{64}$'),
    hook_name TEXT NOT NULL,
    hook_version TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('started', 'completed', 'failed')),
    attempt INTEGER NOT NULL CHECK (attempt > 0),
    result_digest CHAR(64)
        CONSTRAINT astralplane_reconciliation_result_digest_check
        CHECK (result_digest IS NULL OR result_digest ~ '^[0-9a-f]{64}$'),
    error_type TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT astralplane_reconciliation_state_shape_check CHECK (
        (state = 'started' AND result_digest IS NULL AND error_type IS NULL)
        OR (state = 'completed' AND result_digest IS NOT NULL AND error_type IS NULL)
        OR (state = 'failed' AND result_digest IS NULL AND error_type IS NOT NULL)
    ),
    PRIMARY KEY (schema_revision, plan_digest, hook_name, hook_version)
)
""".strip(),
    """
DO $astralplane_schema_postcondition$
DECLARE
    invalid_objects TEXT;
BEGIN
    SELECT string_agg(required.table_name || '.' || required.column_name, ', ')
    INTO invalid_objects
    FROM (
        VALUES
            ('audit_events', 'chain_sequence', 'int8', TRUE),
            ('astralplane_outbox', 'entry_id', 'text', TRUE),
            ('astralplane_outbox', 'canonical_payload', 'bytea', TRUE),
            ('astralplane_outbox', 'payload_sha256', 'bpchar', TRUE),
            ('astralplane_outbox', 'lease_owner', 'text', FALSE),
            ('astralplane_outbox', 'lease_expires_at', 'timestamptz', FALSE),
            ('audit_retention_anchor', 'first_retained_sequence', 'int8', TRUE),
            ('audit_retention_anchor', 'previous_entry_digest', 'bytea', TRUE),
            ('audit_retention_anchor', 'retention_policy_digest', 'bytea', TRUE),
            ('astralplane_purge_tombstone', 'tombstone_id', 'text', TRUE),
            ('astralplane_purge_tombstone', 'storage_locator_sha256', 'bpchar', TRUE),
            ('astralplane_purge_tombstone', 'updated_at', 'timestamptz', TRUE),
            ('astralplane_reconciliation_marker', 'plan_digest', 'bpchar', TRUE),
            ('astralplane_reconciliation_marker', 'result_digest', 'bpchar', FALSE),
            ('astralplane_reconciliation_marker', 'error_type', 'text', FALSE)
    ) AS required(table_name, column_name, type_name, not_null)
    LEFT JOIN pg_attribute AS attribute
      ON attribute.attrelid = to_regclass(required.table_name)
     AND attribute.attname = required.column_name
     AND attribute.attnum > 0
     AND NOT attribute.attisdropped
    LEFT JOIN pg_type AS data_type
      ON data_type.oid = attribute.atttypid
    WHERE attribute.attname IS NULL
       OR data_type.typname <> required.type_name
       OR attribute.attnotnull <> required.not_null;
    IF invalid_objects IS NOT NULL THEN
        RAISE EXCEPTION 'AstralPlane schema columns are missing or incompatible: %',
            invalid_objects USING ERRCODE = '42P16';
    END IF;

    SELECT string_agg(required.constraint_name, ', ')
    INTO invalid_objects
    FROM (
        VALUES
            ('audit_events', 'audit_events_chain_sequence_check'),
            ('audit_events', 'audit_events_digest_length_check'),
            ('audit_events', 'audit_events_schema_version_check'),
            ('astralplane_outbox', 'astralplane_outbox_payload_digest_check'),
            ('astralplane_outbox', 'astralplane_outbox_lease_shape_check'),
            ('astralplane_outbox', 'astralplane_outbox_error_shape_check'),
            ('audit_retention_anchor', 'audit_retention_previous_digest_check'),
            ('audit_retention_anchor', 'audit_retention_policy_digest_check'),
            ('astralplane_purge_tombstone', 'astralplane_purge_locator_digest_check'),
            ('astralplane_purge_tombstone', 'astralplane_purge_state_shape_check'),
            (
                'astralplane_reconciliation_marker',
                'astralplane_reconciliation_plan_digest_check'
            ),
            (
                'astralplane_reconciliation_marker',
                'astralplane_reconciliation_result_digest_check'
            ),
            (
                'astralplane_reconciliation_marker',
                'astralplane_reconciliation_state_shape_check'
            )
    ) AS required(table_name, constraint_name)
    LEFT JOIN pg_constraint AS constraint_record
      ON constraint_record.conrelid = to_regclass(required.table_name)
     AND constraint_record.conname = required.constraint_name
     AND constraint_record.convalidated
    WHERE constraint_record.oid IS NULL;
    IF invalid_objects IS NOT NULL THEN
        RAISE EXCEPTION 'AstralPlane schema constraints are missing: %',
            invalid_objects USING ERRCODE = '42P16';
    END IF;

    SELECT string_agg(required.index_name, ', ')
    INTO invalid_objects
    FROM (
        VALUES
            ('uq_audit_events_actor_sequence'),
            ('idx_astralplane_outbox_claim_v2'),
            ('idx_astralplane_outbox_lease'),
            ('idx_astralplane_purge_pending'),
            ('idx_astralplane_purge_owner_pending')
    ) AS required(index_name)
    LEFT JOIN pg_class AS index_record
      ON index_record.oid = to_regclass(required.index_name)
     AND index_record.relkind = 'i'
    LEFT JOIN pg_index AS index_state
      ON index_state.indexrelid = index_record.oid
     AND index_state.indisvalid
     AND index_state.indisready
    WHERE index_state.indexrelid IS NULL;
    IF invalid_objects IS NOT NULL THEN
        RAISE EXCEPTION 'AstralPlane schema indexes are missing or invalid: %',
            invalid_objects USING ERRCODE = '42P16';
    END IF;

    SELECT string_agg(required.trigger_name, ', ')
    INTO invalid_objects
    FROM (
        VALUES
            ('audit_events_no_update'),
            ('audit_events_assign_sequence')
    ) AS required(trigger_name)
    LEFT JOIN pg_trigger AS trigger_record
      ON trigger_record.tgrelid = 'audit_events'::regclass
     AND trigger_record.tgname = required.trigger_name
     AND trigger_record.tgenabled = 'O'
     AND NOT trigger_record.tgisinternal
    WHERE trigger_record.oid IS NULL;
    IF invalid_objects IS NOT NULL THEN
        RAISE EXCEPTION 'AstralPlane audit triggers are missing or disabled: %',
            invalid_objects USING ERRCODE = '42P16';
    END IF;
END
$astralplane_schema_postcondition$
""".strip(),
)

PLANE_SCHEMA_067_STATEMENTS: Final = (
    *_PLANE_SCHEMA_067_PREFIX,
    *_PLANE_SCHEMA_067_REMAINDER,
)

PLANE_SCHEMA_074_002_STATEMENTS: Final = (
    """
CREATE TABLE IF NOT EXISTS test_runs (
    owner_id TEXT NOT NULL,
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    system_state TEXT NOT NULL,
    categories TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running'
)
""".strip(),
    """
CREATE TABLE IF NOT EXISTS test_case_results (
    owner_id TEXT NOT NULL,
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    suite TEXT NOT NULL,
    test_name TEXT NOT NULL,
    outcome TEXT NOT NULL,
    duration_ms DOUBLE PRECISION DEFAULT 0.0,
    metrics TEXT,
    qualitative TEXT DEFAULT '',
    evidence_hash TEXT DEFAULT '',
    verification_status TEXT NOT NULL DEFAULT 'pending'
)
""".strip(),
    """
CREATE TABLE IF NOT EXISTS test_evidence (
    owner_id TEXT NOT NULL,
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    evidence_type TEXT NOT NULL,
    data TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    captured_at TEXT NOT NULL
)
""".strip(),
    """
CREATE TABLE IF NOT EXISTS audit_entries (
    owner_id TEXT NOT NULL,
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    action TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    rationale TEXT DEFAULT '',
    timestamp TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    hash_version SMALLINT NOT NULL DEFAULT 2
)
""".strip(),
    """
CREATE TABLE IF NOT EXISTS latex_artifacts (
    owner_id TEXT NOT NULL,
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    generated_from TEXT NOT NULL,
    verification_complete BOOLEAN NOT NULL DEFAULT FALSE,
    generated_at TEXT NOT NULL
)
""".strip(),
    "ALTER TABLE test_runs ADD COLUMN IF NOT EXISTS owner_id TEXT",
    "ALTER TABLE test_case_results ADD COLUMN IF NOT EXISTS owner_id TEXT",
    "ALTER TABLE test_evidence ADD COLUMN IF NOT EXISTS owner_id TEXT",
    "ALTER TABLE audit_entries ADD COLUMN IF NOT EXISTS owner_id TEXT",
    "ALTER TABLE audit_entries ADD COLUMN IF NOT EXISTS hash_version SMALLINT",
    "ALTER TABLE latex_artifacts ADD COLUMN IF NOT EXISTS owner_id TEXT",
    """
UPDATE test_runs
SET owner_id = 'system:quality-audit'
WHERE owner_id IS NULL
""".strip(),
    """
UPDATE test_case_results AS child
SET owner_id = parent.owner_id
FROM test_runs AS parent
WHERE child.owner_id IS NULL AND child.run_id = parent.id
""".strip(),
    """
UPDATE test_evidence AS child
SET owner_id = parent.owner_id
FROM test_case_results AS parent
WHERE child.owner_id IS NULL AND child.case_id = parent.id
""".strip(),
    """
UPDATE audit_entries AS child
SET owner_id = parent.owner_id
FROM test_case_results AS parent
WHERE child.owner_id IS NULL AND child.case_id = parent.id
""".strip(),
    "UPDATE audit_entries SET hash_version = 1 WHERE hash_version IS NULL",
    """
UPDATE latex_artifacts AS child
SET owner_id = parent.owner_id
FROM test_runs AS parent
WHERE child.owner_id IS NULL AND child.run_id = parent.id
""".strip(),
    "ALTER TABLE test_runs ALTER COLUMN owner_id SET NOT NULL",
    "ALTER TABLE test_case_results ALTER COLUMN owner_id SET NOT NULL",
    "ALTER TABLE test_evidence ALTER COLUMN owner_id SET NOT NULL",
    "ALTER TABLE audit_entries ALTER COLUMN owner_id SET NOT NULL",
    "ALTER TABLE audit_entries ALTER COLUMN hash_version SET NOT NULL",
    "ALTER TABLE audit_entries ALTER COLUMN hash_version SET DEFAULT 2",
    "ALTER TABLE latex_artifacts ALTER COLUMN owner_id SET NOT NULL",
    """
CREATE UNIQUE INDEX IF NOT EXISTS uq_test_runs_owner_id
ON test_runs (owner_id, id)
""".strip(),
    """
CREATE UNIQUE INDEX IF NOT EXISTS uq_test_case_results_owner_id
ON test_case_results (owner_id, id)
""".strip(),
    """
CREATE UNIQUE INDEX IF NOT EXISTS uq_test_evidence_owner_id
ON test_evidence (owner_id, id)
""".strip(),
    """
CREATE UNIQUE INDEX IF NOT EXISTS uq_audit_entries_owner_id
ON audit_entries (owner_id, id)
""".strip(),
    """
CREATE UNIQUE INDEX IF NOT EXISTS uq_latex_artifacts_owner_id
ON latex_artifacts (owner_id, id)
""".strip(),
    """
CREATE INDEX IF NOT EXISTS idx_test_cases_owner_run
ON test_case_results (owner_id, run_id, suite, test_name, id)
""".strip(),
    """
CREATE INDEX IF NOT EXISTS idx_test_evidence_owner_case
ON test_evidence (owner_id, case_id, captured_at, id)
""".strip(),
    """
CREATE INDEX IF NOT EXISTS idx_audit_entries_owner_case
ON audit_entries (owner_id, case_id, timestamp, id)
""".strip(),
    """
CREATE INDEX IF NOT EXISTS idx_latex_artifacts_owner_run
ON latex_artifacts (owner_id, run_id, filename, id)
""".strip(),
    """
DO $astralplane_quality_audit_constraints$
DECLARE
    table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'test_runs',
        'test_case_results',
        'test_evidence',
        'audit_entries',
        'latex_artifacts'
    ]
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = to_regclass(table_name)
              AND conname = table_name || '_owner_id_check'
        ) THEN
            EXECUTE format(
                'ALTER TABLE %I ADD CONSTRAINT %I CHECK '
                || '(astralplane_identifier_is_canonical(owner_id)) NOT VALID',
                table_name,
                table_name || '_owner_id_check'
            );
        END IF;
        EXECUTE format(
            'ALTER TABLE %I VALIDATE CONSTRAINT %I',
            table_name,
            table_name || '_owner_id_check'
        );
    END LOOP;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'test_runs'::regclass
          AND conname = 'test_runs_state_check'
    ) THEN
        ALTER TABLE test_runs
        ADD CONSTRAINT test_runs_state_check CHECK (
            (status = 'running' AND finished_at IS NULL)
            OR (status IN ('completed', 'failed') AND finished_at IS NOT NULL)
        ) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'test_case_results'::regclass
          AND conname = 'test_case_results_values_check'
    ) THEN
        ALTER TABLE test_case_results
        ADD CONSTRAINT test_case_results_values_check CHECK (
            outcome IN ('passed', 'failed', 'error', 'skipped')
            AND verification_status IN (
                'pending', 'verified', 'disputed', 'needs_rerun'
            )
            AND duration_ms >= 0
            AND duration_ms NOT IN (
                'Infinity'::float8, '-Infinity'::float8, 'NaN'::float8
            )
            AND (
                evidence_hash = ''
                OR evidence_hash ~ '^[0-9a-f]{64}$'
            )
        ) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'test_evidence'::regclass
          AND conname = 'test_evidence_digest_check'
    ) THEN
        ALTER TABLE test_evidence
        ADD CONSTRAINT test_evidence_digest_check CHECK (
            sha256 = '' OR sha256 ~ '^[0-9a-f]{64}$'
        ) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'audit_entries'::regclass
          AND conname = 'audit_entries_values_check'
    ) THEN
        ALTER TABLE audit_entries
        ADD CONSTRAINT audit_entries_values_check CHECK (
            action IN ('verified', 'disputed', 'needs_rerun')
            AND (
                previous_hash = ''
                OR previous_hash ~ '^[0-9a-f]{64}$'
            )
        ) NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'audit_entries'::regclass
          AND conname = 'audit_entries_hash_version_check'
    ) THEN
        ALTER TABLE audit_entries
        ADD CONSTRAINT audit_entries_hash_version_check CHECK (
            hash_version IN (1, 2)
        ) NOT VALID;
    END IF;

    ALTER TABLE test_runs VALIDATE CONSTRAINT test_runs_state_check;
    ALTER TABLE test_case_results
        VALIDATE CONSTRAINT test_case_results_values_check;
    ALTER TABLE test_evidence VALIDATE CONSTRAINT test_evidence_digest_check;
    ALTER TABLE audit_entries VALIDATE CONSTRAINT audit_entries_values_check;
    ALTER TABLE audit_entries
        VALIDATE CONSTRAINT audit_entries_hash_version_check;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'test_case_results'::regclass
          AND conname = 'test_case_results_owner_run_fk'
    ) THEN
        ALTER TABLE test_case_results
        ADD CONSTRAINT test_case_results_owner_run_fk
        FOREIGN KEY (owner_id, run_id) REFERENCES test_runs(owner_id, id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'test_evidence'::regclass
          AND conname = 'test_evidence_owner_case_fk'
    ) THEN
        ALTER TABLE test_evidence
        ADD CONSTRAINT test_evidence_owner_case_fk
        FOREIGN KEY (owner_id, case_id)
        REFERENCES test_case_results(owner_id, id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'audit_entries'::regclass
          AND conname = 'audit_entries_owner_case_fk'
    ) THEN
        ALTER TABLE audit_entries
        ADD CONSTRAINT audit_entries_owner_case_fk
        FOREIGN KEY (owner_id, case_id)
        REFERENCES test_case_results(owner_id, id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'latex_artifacts'::regclass
          AND conname = 'latex_artifacts_owner_run_fk'
    ) THEN
        ALTER TABLE latex_artifacts
        ADD CONSTRAINT latex_artifacts_owner_run_fk
        FOREIGN KEY (owner_id, run_id) REFERENCES test_runs(owner_id, id);
    END IF;
END
$astralplane_quality_audit_constraints$
""".strip(),
    """
DO $astralplane_quality_audit_postcondition$
DECLARE
    invalid_objects TEXT;
BEGIN
    SELECT string_agg(required.table_name || '.' || required.column_name, ', ')
    INTO invalid_objects
    FROM (
        VALUES
            ('test_runs', 'owner_id'),
            ('test_case_results', 'owner_id'),
            ('test_evidence', 'owner_id'),
            ('audit_entries', 'owner_id'),
            ('audit_entries', 'hash_version'),
            ('latex_artifacts', 'owner_id')
    ) AS required(table_name, column_name)
    LEFT JOIN pg_attribute AS attribute
      ON attribute.attrelid = to_regclass(required.table_name)
     AND attribute.attname = required.column_name
     AND attribute.attnum > 0
     AND NOT attribute.attisdropped
    WHERE attribute.attname IS NULL OR NOT attribute.attnotnull;
    IF invalid_objects IS NOT NULL THEN
        RAISE EXCEPTION 'qualification audit owner columns are incompatible: %',
            invalid_objects USING ERRCODE = '42P16';
    END IF;

    SELECT string_agg(required.index_name, ', ')
    INTO invalid_objects
    FROM (
        VALUES
            ('uq_test_runs_owner_id'),
            ('uq_test_case_results_owner_id'),
            ('uq_test_evidence_owner_id'),
            ('uq_audit_entries_owner_id'),
            ('uq_latex_artifacts_owner_id'),
            ('idx_test_cases_owner_run'),
            ('idx_test_evidence_owner_case'),
            ('idx_audit_entries_owner_case'),
            ('idx_latex_artifacts_owner_run')
    ) AS required(index_name)
    LEFT JOIN pg_class AS index_record
      ON index_record.oid = to_regclass(required.index_name)
     AND index_record.relkind = 'i'
    LEFT JOIN pg_index AS index_state
      ON index_state.indexrelid = index_record.oid
     AND index_state.indisvalid
     AND index_state.indisready
    WHERE index_state.indexrelid IS NULL;
    IF invalid_objects IS NOT NULL THEN
        RAISE EXCEPTION 'qualification audit indexes are missing or invalid: %',
            invalid_objects USING ERRCODE = '42P16';
    END IF;

    SELECT string_agg(required.constraint_name, ', ')
    INTO invalid_objects
    FROM (
        VALUES
            ('test_runs', 'test_runs_owner_id_check'),
            ('test_runs', 'test_runs_state_check'),
            ('test_case_results', 'test_case_results_owner_id_check'),
            ('test_case_results', 'test_case_results_values_check'),
            ('test_case_results', 'test_case_results_owner_run_fk'),
            ('test_evidence', 'test_evidence_owner_id_check'),
            ('test_evidence', 'test_evidence_digest_check'),
            ('test_evidence', 'test_evidence_owner_case_fk'),
            ('audit_entries', 'audit_entries_owner_id_check'),
            ('audit_entries', 'audit_entries_values_check'),
            ('audit_entries', 'audit_entries_hash_version_check'),
            ('audit_entries', 'audit_entries_owner_case_fk'),
            ('latex_artifacts', 'latex_artifacts_owner_id_check'),
            ('latex_artifacts', 'latex_artifacts_owner_run_fk')
    ) AS required(table_name, constraint_name)
    LEFT JOIN pg_constraint AS constraint_record
      ON constraint_record.conrelid = to_regclass(required.table_name)
     AND constraint_record.conname = required.constraint_name
     AND constraint_record.convalidated
    WHERE constraint_record.oid IS NULL;
    IF invalid_objects IS NOT NULL THEN
        RAISE EXCEPTION 'qualification audit constraints are missing: %',
            invalid_objects USING ERRCODE = '42P16';
    END IF;
END
$astralplane_quality_audit_postcondition$
""".strip(),
)

PLANE_SCHEMA_074_003_STATEMENTS: Final = (
    """
DO $astralplane_runtime_contract_migration$
BEGIN
    IF to_regclass('agent_host_session') IS NULL THEN
        RETURN;
    END IF;

    ALTER TABLE agent_host_session
        ADD COLUMN IF NOT EXISTS legacy_runtime_contract BOOLEAN;
    ALTER TABLE agent_host_session
        ALTER COLUMN legacy_runtime_contract SET DEFAULT FALSE;

    UPDATE agent_host_session
    SET legacy_runtime_contract = TRUE
    WHERE runtime_contract_version = 2
      AND legacy_runtime_contract IS DISTINCT FROM TRUE;

    UPDATE agent_host_session
    SET legacy_runtime_contract = FALSE
    WHERE runtime_contract_version = 3
      AND legacy_runtime_contract IS DISTINCT FROM FALSE;

    IF EXISTS (
        SELECT 1
        FROM agent_host_session
        WHERE runtime_contract_version NOT IN (2, 3)
           OR legacy_runtime_contract IS NULL
    ) THEN
        RAISE EXCEPTION
            'agent host sessions contain an unsupported runtime contract version'
            USING ERRCODE = '23514';
    END IF;

    ALTER TABLE agent_host_session
        ALTER COLUMN legacy_runtime_contract SET NOT NULL;
    ALTER TABLE agent_host_session
        DROP CONSTRAINT IF EXISTS agent_host_session_check;
    ALTER TABLE agent_host_session
        DROP CONSTRAINT IF EXISTS
            agent_host_session_runtime_contract_version_check;
    ALTER TABLE agent_host_session
        ADD CONSTRAINT agent_host_session_runtime_contract_version_check CHECK (
            runtime_contract_version = ANY(supported_runtime_contract_versions)
            AND (
                (legacy_runtime_contract AND runtime_contract_version = 2)
                OR (
                    NOT legacy_runtime_contract
                    AND runtime_contract_version = 3
                )
            )
        ) NOT VALID;
    ALTER TABLE agent_host_session
        VALIDATE CONSTRAINT
            agent_host_session_runtime_contract_version_check;
END
$astralplane_runtime_contract_migration$
""".strip(),
    """
DO $astralplane_runtime_contract_postcondition$
DECLARE
    column_default TEXT;
    column_not_null BOOLEAN;
    column_type OID;
    constraint_definition TEXT;
    constraint_validated BOOLEAN;
BEGIN
    IF to_regclass('agent_host_session') IS NULL THEN
        RETURN;
    END IF;

    SELECT
        pg_get_expr(default_record.adbin, default_record.adrelid, TRUE),
        attribute.attnotnull,
        attribute.atttypid
    INTO column_default, column_not_null, column_type
    FROM pg_attribute AS attribute
    LEFT JOIN pg_attrdef AS default_record
      ON default_record.adrelid = attribute.attrelid
     AND default_record.adnum = attribute.attnum
    WHERE attribute.attrelid = 'agent_host_session'::regclass
      AND attribute.attname = 'legacy_runtime_contract'
      AND attribute.attnum > 0
      AND NOT attribute.attisdropped;

    IF column_default IS DISTINCT FROM 'false'
       OR column_not_null IS DISTINCT FROM TRUE
       OR column_type IS DISTINCT FROM 'boolean'::regtype::oid THEN
        RAISE EXCEPTION
            'agent host legacy runtime contract column is incompatible'
            USING ERRCODE = '42P16';
    END IF;

    SELECT
        pg_get_expr(
            constraint_record.conbin,
            constraint_record.conrelid,
            TRUE
        ),
        constraint_record.convalidated
    INTO constraint_definition, constraint_validated
    FROM pg_constraint AS constraint_record
    WHERE constraint_record.conrelid = 'agent_host_session'::regclass
      AND constraint_record.conname =
          'agent_host_session_runtime_contract_version_check'
      AND constraint_record.contype = 'c';

    IF constraint_definition IS DISTINCT FROM
            '(runtime_contract_version = ANY (supported_runtime_contract_versions)) '
            'AND (legacy_runtime_contract AND runtime_contract_version = 2 '
            'OR NOT legacy_runtime_contract '
            'AND runtime_contract_version = 3)'
       OR constraint_validated IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION
            'agent host runtime contract constraint is incompatible'
            USING ERRCODE = '42P16';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_constraint AS constraint_record
        WHERE constraint_record.conrelid = 'agent_host_session'::regclass
          AND constraint_record.contype = 'c'
          AND constraint_record.conname <>
              'agent_host_session_runtime_contract_version_check'
          AND pg_get_expr(
              constraint_record.conbin,
              constraint_record.conrelid,
              TRUE
          ) ~ '(^|[^A-Za-z0-9_])runtime_contract_version([^A-Za-z0-9_]|$)'
    ) THEN
        RAISE EXCEPTION
            'agent host runtime contract carries a conflicting constraint'
            USING ERRCODE = '42P16';
    END IF;
END
$astralplane_runtime_contract_postcondition$
""".strip(),
)

PLANE_SCHEMA_074_004_STATEMENTS: Final = (
    """
    DO $astralplane_074_004_clean_predecessor$
    DECLARE
        conflicting_object TEXT;
    BEGIN
        SELECT object_identity
        INTO conflicting_object
        FROM (
            SELECT 'user_attachments.' || attribute.attname AS object_identity
            FROM pg_attribute AS attribute
            WHERE attribute.attrelid = 'user_attachments'::regclass
              AND attribute.attname IN (
                  'materialization_state',
                  'materialization_lease_id',
                  'materialization_lease_version',
                  'materialization_lease_expires_at',
                  'materialization_max_bytes',
                  'materialization_storage_key'
              )
              AND attribute.attnum > 0
              AND NOT attribute.attisdropped
            UNION ALL
            SELECT 'astralplane_purge_tombstone.' || attribute.attname
            FROM pg_attribute AS attribute
            WHERE attribute.attrelid = 'astralplane_purge_tombstone'::regclass
              AND attribute.attname IN (
                  'target_scope',
                  'manual_resolution_evidence_sha256',
                  'manual_resolved_at'
              )
              AND attribute.attnum > 0
              AND NOT attribute.attisdropped
            UNION ALL
            SELECT 'astralplane_blob_owner_state'
            WHERE to_regclass('astralplane_blob_owner_state') IS NOT NULL
            UNION ALL
            SELECT 'idx_user_attachments_pending_materialization_expiry'
            WHERE to_regclass(
                'idx_user_attachments_pending_materialization_expiry'
            ) IS NOT NULL
            UNION ALL
            SELECT 'uq_astralplane_blob_owner_state_casefold'
            WHERE to_regclass('uq_astralplane_blob_owner_state_casefold') IS NOT NULL
            UNION ALL
            SELECT 'uq_user_attachments_attachment_id_casefold'
            WHERE to_regclass('uq_user_attachments_attachment_id_casefold') IS NOT NULL
            UNION ALL
            SELECT 'function-config:' || function_signature
            FROM unnest(ARRAY[
                'audit_events_protect()',
                'audit_events_assign_chain_sequence()',
                'astraldeep_positive_unique_int_array(integer[])',
                'astralplane_identifier_is_canonical(text)',
                'astralplane_capabilities_are_canonical(text[])',
                'astralplane_receipt_watermark_require_advance()'
            ]) AS predecessor_function(function_signature)
            JOIN pg_proc AS function_record
              ON function_record.oid = to_regprocedure(function_signature)
            WHERE function_record.proconfig IS NOT NULL
            UNION ALL
            SELECT 'legacy-index:' || expected.index_name
            FROM (
                VALUES
                    (
                        'idx_user_attachments_user',
                        'user_attachments',
                        ARRAY['user_id', 'created_at']::text[],
                        ARRAY[0, 3]::smallint[],
                        ''::text
                    ),
                    (
                        'idx_user_attachments_live',
                        'user_attachments',
                        ARRAY['user_id']::text[],
                        ARRAY[0]::smallint[],
                        'deleted_at IS NULL'::text
                    ),
                    (
                        'idx_audit_user_recorded',
                        'audit_events',
                        ARRAY[
                            'actor_user_id',
                            'recorded_at',
                            'event_id'
                        ]::text[],
                        ARRAY[0, 3, 3]::smallint[],
                        ''::text
                    ),
                    (
                        'idx_audit_correlation',
                        'audit_events',
                        ARRAY['correlation_id']::text[],
                        ARRAY[0]::smallint[],
                        ''::text
                    ),
                    (
                        'idx_audit_user_class_recorded',
                        'audit_events',
                        ARRAY[
                            'actor_user_id',
                            'event_class',
                            'recorded_at'
                        ]::text[],
                        ARRAY[0, 0, 3]::smallint[],
                        ''::text
                    ),
                    (
                        'idx_audit_user_failures',
                        'audit_events',
                        ARRAY['actor_user_id', 'recorded_at']::text[],
                        ARRAY[0, 3]::smallint[],
                        'outcome = ANY (ARRAY[''failure''::text, '
                            '''interrupted''::text])'::text
                    )
            ) AS expected(index_name, table_name, keys, options, predicate)
            LEFT JOIN pg_class AS index_record
              ON index_record.oid = to_regclass(expected.index_name)
             AND index_record.relnamespace = current_schema()::regnamespace
             AND index_record.relkind = 'i'
            LEFT JOIN pg_index AS index_state
              ON index_state.indexrelid = index_record.oid
            LEFT JOIN pg_class AS table_record
              ON table_record.oid = index_state.indrelid
             AND table_record.relnamespace = current_schema()::regnamespace
            LEFT JOIN pg_am AS access_method
              ON access_method.oid = index_record.relam
            WHERE index_record.oid IS NULL
               OR table_record.relname IS DISTINCT FROM expected.table_name
               OR access_method.amname IS DISTINCT FROM 'btree'
               OR index_state.indisunique IS DISTINCT FROM FALSE
               OR index_state.indisprimary IS DISTINCT FROM FALSE
               OR index_state.indisexclusion IS DISTINCT FROM FALSE
               OR index_state.indisvalid IS DISTINCT FROM TRUE
               OR index_state.indisready IS DISTINCT FROM TRUE
               OR index_state.indnullsnotdistinct IS DISTINCT FROM FALSE
               OR index_state.indnkeyatts IS DISTINCT FROM cardinality(expected.keys)
               OR index_state.indnatts IS DISTINCT FROM cardinality(expected.keys)
               OR ARRAY(
                    SELECT pg_get_indexdef(index_state.indexrelid, position, TRUE)
                    FROM generate_series(1, index_state.indnkeyatts) AS position
                    ORDER BY position
               ) IS DISTINCT FROM expected.keys
               OR ARRAY(
                    SELECT option
                    FROM unnest(index_state.indoption::smallint[])
                        WITH ORDINALITY AS index_option(option, position)
                    ORDER BY index_option.position
               ) IS DISTINCT FROM expected.options
               OR COALESCE(
                    pg_get_expr(index_state.indpred, index_state.indrelid, TRUE),
                    ''
               ) IS DISTINCT FROM expected.predicate
            UNION ALL
            SELECT function_name
            FROM unnest(ARRAY[
                'astralplane_blob_owner_is_canonical',
                'astralplane_blob_storage_key_is_canonical',
                'astralplane_attachment_id_is_canonical'
            ]) AS function_record(function_name)
            WHERE to_regprocedure(function_name || '(text)') IS NOT NULL
        ) AS conflicts
        ORDER BY object_identity
        LIMIT 1;

        IF conflicting_object IS NOT NULL THEN
            RAISE EXCEPTION
                '074.004 requires a clean 074.003 predecessor; found %',
                conflicting_object
                USING ERRCODE = '42P16';
        END IF;
    END
    $astralplane_074_004_clean_predecessor$
    """.strip(),
    """
    DO $astralplane_owned_schema_hardening$
    DECLARE
        schema_name TEXT := current_schema();
    BEGIN
        IF schema_name IS NULL THEN
            RAISE EXCEPTION 'Plane migration has no current schema'
                USING ERRCODE = '3F000';
        END IF;
        EXECUTE format(
            'ALTER SCHEMA %I OWNER TO %I',
            schema_name,
            current_user
        );
        EXECUTE format(
            'REVOKE ALL ON SCHEMA %I FROM PUBLIC',
            schema_name
        );
    END
    $astralplane_owned_schema_hardening$
    """.strip(),
    """
    CREATE OR REPLACE FUNCTION astralplane_blob_owner_is_canonical(candidate TEXT)
    RETURNS BOOLEAN AS $astralplane_blob_owner_identifier$
        SELECT (
            candidate ~ '^[A-Za-z0-9][A-Za-z0-9._@-]{0,254}$'
            OR candidate ~ '^__verif__[A-Za-z0-9][A-Za-z0-9._-]{0,245}$'
        )
        AND candidate = rtrim(candidate, ' .')
        AND split_part(lower(candidate), '.', 1) NOT IN (
            'con', 'prn', 'aux', 'nul',
            'com1', 'com2', 'com3', 'com4', 'com5',
            'com6', 'com7', 'com8', 'com9',
            'lpt1', 'lpt2', 'lpt3', 'lpt4', 'lpt5',
            'lpt6', 'lpt7', 'lpt8', 'lpt9'
        )
    $astralplane_blob_owner_identifier$
    LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE;

    CREATE OR REPLACE FUNCTION astralplane_blob_storage_key_is_canonical(candidate TEXT)
    RETURNS BOOLEAN AS $astralplane_blob_storage_key$
        SELECT char_length(candidate) BETWEEN 1 AND 4096
           AND candidate NOT LIKE '/%'
           AND position(E'\\\\' IN candidate) = 0
           AND cardinality(string_to_array(candidate, '/')) BETWEEN 1 AND 32
           AND NOT EXISTS (
               SELECT 1
               FROM unnest(string_to_array(candidate, '/')) AS component(value)
               WHERE char_length(component.value) NOT BETWEEN 1 AND 255
                  OR component.value IN ('.', '..')
                  OR component.value LIKE '.astralplane-%'
                  OR component.value <> rtrim(component.value, ' .')
                  OR component.value ~ '[<>:"|?*]'
                  OR component.value ~ '[[:cntrl:]]'
                  OR split_part(lower(component.value), '.', 1) IN (
                      'con', 'prn', 'aux', 'nul',
                      'com1', 'com2', 'com3', 'com4', 'com5',
                      'com6', 'com7', 'com8', 'com9',
                      'lpt1', 'lpt2', 'lpt3', 'lpt4', 'lpt5',
                      'lpt6', 'lpt7', 'lpt8', 'lpt9'
                  )
           )
    $astralplane_blob_storage_key$
    LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE;

    CREATE OR REPLACE FUNCTION astralplane_attachment_id_is_canonical(candidate TEXT)
    RETURNS BOOLEAN AS $astralplane_attachment_identifier$
        SELECT candidate ~ '^[A-Za-z0-9][A-Za-z0-9._@-]{0,254}$'
           AND position('/' IN candidate) = 0
           AND astralplane_blob_storage_key_is_canonical(candidate)
    $astralplane_attachment_identifier$
    LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE
    """.strip(),
    """
    DO $astralplane_074_004_legacy_data_preflight$
    DECLARE
        invalid_attachment TEXT;
        invalid_tombstone TEXT;
        invalid_legacy_foreign_key TEXT;
        colliding_owner TEXT;
        colliding_attachment TEXT;
        valid_audit_protection BOOLEAN;
        valid_positive_array_function BOOLEAN;
    BEGIN
        IF to_regprocedure('astraldeep_positive_unique_int_array(integer[])')
            IS NOT NULL THEN
            SELECT count(*) = 1 AND COALESCE(
                bool_and(
                    encode(
                        sha256(convert_to(function_record.prosrc, 'UTF8')),
                        'hex'
                    ) =
                        'cfb4e8be9f6d99b1d6941256d9c873087ef0b413549457b84d9e60e6f0e99a57'
                    AND language_record.lanname = 'sql'
                    AND function_record.prorettype = 'boolean'::regtype
                    AND function_record.proargtypes[0] =
                        'integer[]'::regtype::oid
                    AND NOT function_record.prosecdef
                    AND function_record.proisstrict
                    AND function_record.provolatile = 'i'
                    AND function_record.proparallel = 'u'
                    AND function_record.prokind = 'f'
                    AND function_record.proconfig IS NULL
                    AND NOT function_record.proleakproof
                    AND pg_get_userbyid(function_record.proowner) = current_user
                ),
                FALSE
            )
            INTO valid_positive_array_function
            FROM pg_proc AS function_record
            JOIN pg_namespace AS function_namespace
              ON function_namespace.oid = function_record.pronamespace
             AND function_namespace.nspname = current_schema()
            JOIN pg_language AS language_record
              ON language_record.oid = function_record.prolang
            WHERE function_record.proname =
                'astraldeep_positive_unique_int_array'
              AND function_record.pronargs = 1;

            IF NOT valid_positive_array_function THEN
                RAISE EXCEPTION USING
                    ERRCODE = '42P16',
                    MESSAGE =
                        '074.004 positive integer-array predecessor is incompatible; ' ||
                        'remain on 074.003 and restore the exact function before retry';
            END IF;
        END IF;

        -- 074.004 intentionally canonicalizes the two whitespace-distinct 074.003 function
        -- bodies.  Prove the predecessor function and trigger are one of those exact known
        -- shapes before replacing anything, so a hostile implementation is never normalized.
        SELECT count(*) = 1 AND COALESCE(
            bool_and(
                encode(
                    sha256(convert_to(function_record.prosrc, 'UTF8')),
                    'hex'
                ) IN (
                    '03cc5de35d3cfc1fb0bcc4b9c1c703e40b27557a0d2b5b87d00dfd02ee680af7',
                    '9034034a05d1e7fc9d9ed43d442a65c2a899224a4c7f297bfe0d4c77cda6cc07'
                )
                AND language_record.lanname = 'plpgsql'
                AND function_record.prorettype = 'trigger'::regtype
                AND NOT function_record.prosecdef
                AND NOT function_record.proisstrict
                AND function_record.provolatile = 'v'
                AND function_record.proparallel = 'u'
                AND function_record.prokind = 'f'
                AND function_record.proconfig IS NULL
                AND NOT function_record.proleakproof
                AND pg_get_userbyid(function_record.proowner) = current_user
                AND trigger_record.tgtype = 27
                AND trigger_record.tgenabled = 'O'
                AND NOT trigger_record.tgisinternal
                AND trigger_record.tgqual IS NULL
            ),
            FALSE
        )
        INTO valid_audit_protection
        FROM pg_proc AS function_record
        JOIN pg_namespace AS function_namespace
          ON function_namespace.oid = function_record.pronamespace
         AND function_namespace.nspname = current_schema()
        JOIN pg_language AS language_record
          ON language_record.oid = function_record.prolang
        JOIN pg_trigger AS trigger_record
          ON trigger_record.tgfoid = function_record.oid
         AND trigger_record.tgname = 'audit_events_no_update'
        JOIN pg_class AS table_record
          ON table_record.oid = trigger_record.tgrelid
         AND table_record.relnamespace = current_schema()::regnamespace
         AND table_record.relname = 'audit_events'
        WHERE function_record.proname = 'audit_events_protect'
          AND function_record.pronargs = 0;

        IF NOT valid_audit_protection THEN
            RAISE EXCEPTION USING
                ERRCODE = '42P16',
                MESSAGE =
                    '074.004 audit protection predecessor is incompatible; ' ||
                    'remain on 074.003 and restore the exact function and trigger before retry';
        END IF;

        -- Historical Deep stored root-relative locators as
        -- owner/attachment/filename (or the same components with Windows separators).  Prove
        -- every predecessor row can be mapped to the hardened owner/key contract before adding
        -- lifecycle state.  Metadata is not silently rewritten because physical bytes are not
        -- relocated by this database migration.
        SELECT attachment.attachment_id
        INTO invalid_attachment
        FROM user_attachments AS attachment
        WHERE NOT astralplane_blob_owner_is_canonical(attachment.user_id)
           OR NOT astralplane_attachment_id_is_canonical(attachment.attachment_id)
           OR NOT astralplane_blob_storage_key_is_canonical(
               attachment.attachment_id || '/' || attachment.filename
           )
           OR replace(attachment.storage_path, E'\\\\', '/') <>
               attachment.user_id || '/' || attachment.attachment_id || '/' ||
               attachment.filename
           OR attachment.size_bytes < 0
           OR attachment.sha256 !~ '^[0-9a-f]{64}$'
        ORDER BY attachment.attachment_id
        LIMIT 1;

        IF invalid_attachment IS NOT NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = '42P16',
                MESSAGE = format(
                    '074.004 cannot represent legacy attachment %s; ' ||
                    'remain on 074.003 and repair or remove its metadata and physical bytes ' ||
                    'before retry',
                    invalid_attachment
                );
        END IF;

        SELECT lower(attachment.user_id)
        INTO colliding_owner
        FROM user_attachments AS attachment
        GROUP BY lower(attachment.user_id)
        HAVING count(DISTINCT attachment.user_id) > 1
        ORDER BY lower(attachment.user_id)
        LIMIT 1;

        IF colliding_owner IS NOT NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = '42P16',
                MESSAGE = format(
                    '074.004 legacy attachment owners collide under case-folding at %s; ' ||
                    'remain on 074.003 and reconcile the namespaces before retry',
                    colliding_owner
                );
        END IF;

        SELECT lower(attachment.attachment_id)
        INTO colliding_attachment
        FROM user_attachments AS attachment
        GROUP BY lower(attachment.attachment_id)
        HAVING count(DISTINCT attachment.attachment_id) > 1
        ORDER BY lower(attachment.attachment_id)
        LIMIT 1;

        IF colliding_attachment IS NOT NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = '42P16',
                MESSAGE = format(
                    '074.004 legacy attachment identifiers collide under case-folding at %s; ' ||
                    'remain on 074.003 and reconcile the physical prefix before retry',
                    colliding_attachment
                );
        END IF;

        -- Migrated purge rows are never sent to the new blob store.  They still need to be
        -- bounded enough for the administrative attestation path to deserialize them without
        -- poisoning readiness after the revision marker is advanced.
        SELECT tombstone.tombstone_id
        INTO invalid_tombstone
        FROM astralplane_purge_tombstone AS tombstone
        WHERE tombstone.tombstone_id !~ '^[A-Za-z0-9][A-Za-z0-9._:@-]{0,254}$'
           OR tombstone.owner_id !~ '^[A-Za-z0-9][A-Za-z0-9._:@-]{0,254}$'
           OR tombstone.object_id !~ '^[A-Za-z0-9][A-Za-z0-9._:@-]{0,254}$'
           OR tombstone.object_kind NOT IN (
               'attachment', 'artifact', 'knowledge', 'generated_agent'
           )
           OR char_length(tombstone.storage_key) NOT BETWEEN 1 AND 4096
           OR tombstone.storage_locator_sha256 !~ '^[0-9a-f]{64}$'
        ORDER BY tombstone.tombstone_id
        LIMIT 1;

        IF invalid_tombstone IS NOT NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = '42P16',
                MESSAGE = format(
                    '074.004 cannot bound legacy purge tombstone %s; ' ||
                    'remain on 074.003 and resolve or remove it with the predecessor operator ' ||
                    'procedure before retry',
                    invalid_tombstone
                );
        END IF;

        -- The immutable pre-split fixture can retain redundant single-column foreign keys in
        -- addition to the qualified composite owner keys.  Validate every namesake before the
        -- canonicalizing DROP below; a hostile partial predecessor must never be normalized by
        -- name alone.
        WITH expected(
            constraint_name,
            table_name,
            key_name,
            reference_table_name,
            reference_key_name
        ) AS (
            VALUES
                (
                    'test_case_results_run_id_fkey',
                    'test_case_results',
                    'run_id',
                    'test_runs',
                    'id'
                ),
                (
                    'test_evidence_case_id_fkey',
                    'test_evidence',
                    'case_id',
                    'test_case_results',
                    'id'
                ),
                (
                    'audit_entries_case_id_fkey',
                    'audit_entries',
                    'case_id',
                    'test_case_results',
                    'id'
                ),
                (
                    'latex_artifacts_run_id_fkey',
                    'latex_artifacts',
                    'run_id',
                    'test_runs',
                    'id'
                )
        )
        SELECT expected.constraint_name
        INTO invalid_legacy_foreign_key
        FROM expected
        JOIN pg_constraint AS constraint_record
          ON constraint_record.conrelid = to_regclass(expected.table_name)
         AND constraint_record.conname = expected.constraint_name
        LEFT JOIN pg_class AS reference_table
          ON reference_table.oid = constraint_record.confrelid
        WHERE constraint_record.contype <> 'f'
           OR constraint_record.condeferrable
           OR constraint_record.condeferred
           OR NOT constraint_record.convalidated
           OR constraint_record.confmatchtype <> 's'
           OR constraint_record.confupdtype <> 'a'
           OR constraint_record.confdeltype <> 'a'
           OR reference_table.relname <> expected.reference_table_name
           OR reference_table.relnamespace <> current_schema()::regnamespace
           OR ARRAY(
                SELECT attribute.attname::TEXT
                FROM unnest(constraint_record.conkey)
                    WITH ORDINALITY AS key(attnum, position)
                JOIN pg_attribute AS attribute
                  ON attribute.attrelid = constraint_record.conrelid
                 AND attribute.attnum = key.attnum
                ORDER BY key.position
            ) <> ARRAY[expected.key_name]
           OR ARRAY(
                SELECT attribute.attname::TEXT
                FROM unnest(constraint_record.confkey)
                    WITH ORDINALITY AS key(attnum, position)
                JOIN pg_attribute AS attribute
                  ON attribute.attrelid = constraint_record.confrelid
                 AND attribute.attnum = key.attnum
                ORDER BY key.position
            ) <> ARRAY[expected.reference_key_name]
        ORDER BY expected.constraint_name
        LIMIT 1;

        IF invalid_legacy_foreign_key IS NOT NULL THEN
            RAISE EXCEPTION USING
                ERRCODE = '42P16',
                MESSAGE = format(
                    '074.004 legacy foreign key %s is incompatible; ' ||
                    'remain on 074.003 and restore its exact predecessor shape before retry',
                    invalid_legacy_foreign_key
                );
        END IF;
    END
    $astralplane_074_004_legacy_data_preflight$
    """.strip(),
    """
    ALTER TABLE test_case_results
        DROP CONSTRAINT IF EXISTS test_case_results_run_id_fkey;
    ALTER TABLE test_evidence
        DROP CONSTRAINT IF EXISTS test_evidence_case_id_fkey;
    ALTER TABLE audit_entries
        DROP CONSTRAINT IF EXISTS audit_entries_case_id_fkey;
    ALTER TABLE latex_artifacts
        DROP CONSTRAINT IF EXISTS latex_artifacts_run_id_fkey
    """.strip(),
    """
    CREATE OR REPLACE FUNCTION astraldeep_positive_unique_int_array(
        input_values INTEGER[]
    ) RETURNS BOOLEAN
    LANGUAGE SQL IMMUTABLE STRICT
    AS $function$
        SELECT cardinality(input_values) > 0
           AND NOT EXISTS (
               SELECT 1 FROM unnest(input_values) AS item(value)
               WHERE value <= 0
           )
           AND cardinality(input_values) = (
               SELECT count(DISTINCT value) FROM unnest(input_values) AS item(value)
           )
    $function$
    """.strip(),
    """
    CREATE OR REPLACE FUNCTION audit_events_protect()
    RETURNS trigger
    LANGUAGE plpgsql
    VOLATILE
    CALLED ON NULL INPUT
    SECURITY INVOKER
    PARALLEL UNSAFE
    AS $astralplane_audit_protect$
    BEGIN
        IF current_setting('audit.allow_purge', true) IS DISTINCT FROM 'true' THEN
            RAISE EXCEPTION 'audit_events is append-only (TG_OP=%)', TG_OP
                USING ERRCODE = '42501';
        END IF;
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END;
    $astralplane_audit_protect$
    """.strip(),
    """
    DO $astralplane_owned_function_search_path$
    DECLARE
        function_signature TEXT;
    BEGIN
        FOREACH function_signature IN ARRAY ARRAY[
            'audit_events_protect()',
            'audit_events_assign_chain_sequence()',
            'astraldeep_positive_unique_int_array(integer[])',
            'astralplane_identifier_is_canonical(text)',
            'astralplane_capabilities_are_canonical(text[])',
            'astralplane_blob_owner_is_canonical(text)',
            'astralplane_blob_storage_key_is_canonical(text)',
            'astralplane_attachment_id_is_canonical(text)',
            'astralplane_receipt_watermark_require_advance()'
        ] LOOP
            EXECUTE format(
                'ALTER FUNCTION %I.%s SET search_path TO pg_catalog, %I, pg_temp',
                current_schema(),
                function_signature,
                current_schema()
            );
        END LOOP;
    END
    $astralplane_owned_function_search_path$
    """.strip(),
    """
    ALTER TABLE user_attachments
        ADD COLUMN IF NOT EXISTS materialization_state TEXT;
    ALTER TABLE user_attachments
        ADD COLUMN IF NOT EXISTS materialization_lease_id TEXT;
    ALTER TABLE user_attachments
        ADD COLUMN IF NOT EXISTS materialization_lease_version BIGINT;
    ALTER TABLE user_attachments
        ADD COLUMN IF NOT EXISTS materialization_lease_expires_at TIMESTAMPTZ;
    ALTER TABLE user_attachments
        ADD COLUMN IF NOT EXISTS materialization_max_bytes BIGINT;
    ALTER TABLE user_attachments
        ADD COLUMN IF NOT EXISTS materialization_storage_key TEXT;

    -- 074.003 has no materialization lifecycle.  Normalize every predecessor row rather than
    -- trusting namesake columns from a partial/hostile failed deployment.
    UPDATE user_attachments
    SET materialization_state = 'ready',
        materialization_lease_id = NULL,
        materialization_lease_version = NULL,
        materialization_lease_expires_at = NULL,
        materialization_max_bytes = NULL,
        materialization_storage_key = NULL;

    -- Existing predecessor rows are qualified above, but every post-cutover writer must select
    -- the pending lifecycle explicitly.  A READY default would let a stale baseline-shaped
    -- INSERT bypass durable staging and publication fences during a mixed rollout.
    ALTER TABLE user_attachments
        ALTER COLUMN materialization_state DROP DEFAULT;
    ALTER TABLE user_attachments
        ALTER COLUMN materialization_state SET NOT NULL;
    ALTER TABLE user_attachments
        DROP CONSTRAINT IF EXISTS user_attachments_materialization_state_check;
    ALTER TABLE user_attachments
        ADD CONSTRAINT user_attachments_materialization_state_check CHECK (
            (
                materialization_state = 'ready'
                AND astralplane_blob_owner_is_canonical(user_id)
                AND astralplane_attachment_id_is_canonical(attachment_id)
                AND astralplane_blob_storage_key_is_canonical(
                    attachment_id || '/' || filename
                )
                AND replace(storage_path, E'\\\\', '/') =
                    user_id || '/' || attachment_id || '/' || filename
                AND size_bytes >= 0
                AND sha256 ~ '^[0-9a-f]{64}$'
                AND (
                    (
                        materialization_lease_id IS NULL
                        AND materialization_lease_version IS NULL
                        AND materialization_lease_expires_at IS NULL
                        AND materialization_max_bytes IS NULL
                        AND materialization_storage_key IS NULL
                    )
                    OR (
                        materialization_lease_id IS NOT NULL
                        AND astralplane_identifier_is_canonical(
                            materialization_lease_id
                        )
                        AND materialization_lease_version IS NOT NULL
                        AND materialization_lease_version >= 1
                        AND materialization_lease_expires_at IS NOT NULL
                        AND materialization_max_bytes IS NOT NULL
                        AND materialization_max_bytes > 0
                        AND materialization_storage_key IS NOT NULL
                        AND astralplane_blob_owner_is_canonical(user_id)
                        AND astralplane_attachment_id_is_canonical(attachment_id)
                        AND astralplane_blob_storage_key_is_canonical(
                            materialization_storage_key
                        )
                        AND materialization_storage_key =
                            attachment_id || '/' || filename
                        AND replace(storage_path, E'\\\\', '/') =
                            user_id || '/' || materialization_storage_key
                    )
                )
            )
            OR (
                materialization_state = 'pending'
                AND materialization_lease_id IS NOT NULL
                AND astralplane_identifier_is_canonical(materialization_lease_id)
                AND materialization_lease_version IS NOT NULL
                AND materialization_lease_version >= 0
                AND materialization_lease_expires_at IS NOT NULL
                AND materialization_max_bytes IS NOT NULL
                AND materialization_max_bytes > 0
                AND materialization_storage_key IS NOT NULL
                AND astralplane_blob_owner_is_canonical(user_id)
                AND astralplane_attachment_id_is_canonical(attachment_id)
                AND astralplane_blob_storage_key_is_canonical(
                    materialization_storage_key
                )
                AND materialization_storage_key = attachment_id || '/' || filename
                AND replace(storage_path, E'\\\\', '/') =
                    user_id || '/' || materialization_storage_key
            )
        ) NOT VALID;
    ALTER TABLE user_attachments
        VALIDATE CONSTRAINT user_attachments_materialization_state_check;
    """.strip(),
    """
    CREATE INDEX IF NOT EXISTS idx_user_attachments_pending_materialization_expiry
    ON user_attachments (
        materialization_lease_expires_at,
        attachment_id
    )
    WHERE materialization_state = 'pending' AND deleted_at IS NULL
    """.strip(),
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_user_attachments_attachment_id_casefold
    ON user_attachments (lower(attachment_id))
    """.strip(),
    """
    ALTER TABLE astralplane_purge_tombstone
        ADD COLUMN IF NOT EXISTS target_scope TEXT;
    ALTER TABLE astralplane_purge_tombstone
        ADD COLUMN IF NOT EXISTS manual_resolution_evidence_sha256 CHAR(64);
    ALTER TABLE astralplane_purge_tombstone
        ADD COLUMN IF NOT EXISTS manual_resolved_at TIMESTAMPTZ;
    -- No 074.003 tombstone carried publication-revocation authority.  Downgrade every predecessor
    -- record, including a namesake typed/PURGED row from an interrupted partial rollout.
    UPDATE astralplane_purge_tombstone
    SET target_scope = 'exact_key',
        status = 'manual_review',
        version = version + 1,
        verified_absent_at = NULL,
        manual_resolution_evidence_sha256 = NULL,
        manual_resolved_at = NULL,
        last_error_code = 'legacy_scope_unqualified',
        updated_at = clock_timestamp();

    -- Every predecessor exact attachment tombstone lacks publication-revocation authority.
    -- Preserve a colliding row for operator attestation under a deterministic private object
    -- identity so it cannot permanently squat the typed attachment-prefix identity.  A later
    -- logical delete of a currently-live attachment can then schedule the qualified typed scope;
    -- already-deleted attachments are scheduled immediately below.
    UPDATE astralplane_purge_tombstone AS tombstone
    SET object_id = 'legacy-unqualified-' || encode(
            sha256(convert_to(tombstone.tombstone_id, 'UTF8')),
            'hex'
        ),
        updated_at = clock_timestamp()
    WHERE tombstone.object_kind = 'attachment'
      AND EXISTS (
          SELECT 1
          FROM user_attachments AS attachment
          WHERE attachment.user_id = tombstone.owner_id
            AND attachment.attachment_id = tombstone.object_id
      );

    -- A predecessor soft delete admitted that physical cleanup may have failed.  Enqueue an
    -- independently executable attachment-prefix tombstone for every deleted attachment.  The
    -- typed identity matches PostgresPurgeStore exactly.

    INSERT INTO astralplane_purge_tombstone (
        tombstone_id, owner_id, object_kind, object_id, storage_key,
        target_scope, storage_locator_sha256, requested_at, status,
        attempt_count, version, available_at, verified_absent_at,
        manual_resolution_evidence_sha256, manual_resolved_at,
        last_error_code
    )
    SELECT
        'purge-attachment_prefix-' || encode(
            sha256(
                convert_to('attachment_prefix', 'UTF8') || decode('00', 'hex') ||
                convert_to(attachment.user_id, 'UTF8') || decode('00', 'hex') ||
                convert_to(attachment.attachment_id, 'UTF8')
            ),
            'hex'
        ),
        attachment.user_id,
        'attachment',
        attachment.attachment_id,
        attachment.attachment_id,
        'attachment_prefix',
        encode(
            sha256(
                convert_to(attachment.user_id, 'UTF8') || decode('00', 'hex') ||
                convert_to(attachment.attachment_id, 'UTF8')
            ),
            'hex'
        ),
        to_timestamp(attachment.deleted_at::DOUBLE PRECISION / 1000.0),
        'pending',
        0,
        0,
        to_timestamp(attachment.deleted_at::DOUBLE PRECISION / 1000.0),
        NULL,
        NULL,
        NULL,
        NULL
    FROM user_attachments AS attachment
    WHERE attachment.deleted_at IS NOT NULL;

    DO $astralplane_legacy_deleted_attachment_postcondition$
    BEGIN
        IF EXISTS (
            SELECT 1
            FROM user_attachments AS attachment
            LEFT JOIN astralplane_purge_tombstone AS tombstone
              ON tombstone.owner_id = attachment.user_id
             AND tombstone.object_kind = 'attachment'
             AND tombstone.object_id = attachment.attachment_id
             AND tombstone.target_scope = 'attachment_prefix'
             AND tombstone.storage_key = attachment.attachment_id
            WHERE attachment.deleted_at IS NOT NULL
              AND tombstone.tombstone_id IS NULL
        ) THEN
            RAISE EXCEPTION
                '074.004 could not schedule every legacy deleted attachment for bounded cleanup'
                USING ERRCODE = '42P16';
        END IF;
    END
    $astralplane_legacy_deleted_attachment_postcondition$;

    ALTER TABLE astralplane_purge_tombstone
        ALTER COLUMN target_scope SET DEFAULT 'exact_key';
    ALTER TABLE astralplane_purge_tombstone
        ALTER COLUMN target_scope SET NOT NULL;
    ALTER TABLE astralplane_purge_tombstone
        DROP CONSTRAINT IF EXISTS astralplane_purge_tombstone_target_scope_check;
    ALTER TABLE astralplane_purge_tombstone
        ADD CONSTRAINT astralplane_purge_tombstone_target_scope_check CHECK (
            target_scope IN ('exact_key', 'attachment_prefix', 'owner_namespace')
        ) NOT VALID;
    ALTER TABLE astralplane_purge_tombstone
        VALIDATE CONSTRAINT astralplane_purge_tombstone_target_scope_check;
    ALTER TABLE astralplane_purge_tombstone
        DROP CONSTRAINT IF EXISTS astralplane_purge_tombstone_target_shape_check;
    ALTER TABLE astralplane_purge_tombstone
        ADD CONSTRAINT astralplane_purge_tombstone_target_shape_check CHECK (
            (
                target_scope = 'exact_key'
                AND status IN ('manual_review', 'purged')
                AND (
                    (
                        status = 'manual_review'
                        AND manual_resolution_evidence_sha256 IS NULL
                        AND manual_resolved_at IS NULL
                    )
                    OR (
                        status = 'purged'
                        AND manual_resolution_evidence_sha256 IS NOT NULL
                        AND manual_resolution_evidence_sha256 ~ '^[0-9a-f]{64}$'
                        AND manual_resolved_at IS NOT NULL
                    )
                )
            )
            OR (
                target_scope = 'attachment_prefix'
                AND object_kind = 'attachment'
                AND astralplane_attachment_id_is_canonical(object_id)
                AND storage_key = object_id
                AND tombstone_id ~ '^purge-attachment_prefix-[0-9a-f]{64}$'
                AND manual_resolution_evidence_sha256 IS NULL
                AND manual_resolved_at IS NULL
            )
            OR (
                target_scope = 'owner_namespace'
                AND object_kind = 'attachment'
                AND object_id ~ '^owner-namespace:[0-9a-f]{64}$'
                AND storage_key = 'owner-namespace'
                AND tombstone_id ~ '^purge-owner_namespace-[0-9a-f]{64}$'
                AND manual_resolution_evidence_sha256 IS NULL
                AND manual_resolved_at IS NULL
            )
        ) NOT VALID;
    ALTER TABLE astralplane_purge_tombstone
        VALIDATE CONSTRAINT astralplane_purge_tombstone_target_shape_check;
    """.strip(),
    """
    CREATE TABLE IF NOT EXISTS astralplane_blob_owner_state (
        owner_id TEXT PRIMARY KEY,
        state TEXT NOT NULL DEFAULT 'active',
        version BIGINT NOT NULL DEFAULT 0,
        retired_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        CONSTRAINT astralplane_blob_owner_state_owner_id_check CHECK (
            astralplane_blob_owner_is_canonical(owner_id)
        ),
        CONSTRAINT astralplane_blob_owner_state_state_check CHECK (
            state IN ('active', 'retired')
        ),
        CONSTRAINT astralplane_blob_owner_state_version_check CHECK (version >= 0),
        CONSTRAINT astralplane_blob_owner_state_retired_at_check CHECK (
            (state = 'active' AND retired_at IS NULL)
            OR (state = 'retired' AND retired_at IS NOT NULL)
        )
    )
    """.strip(),
    """
    -- Seed the durable owner-admission fence from every qualified predecessor namespace
    -- before enabling the case-fold uniqueness guard.  Otherwise a differently-cased new
    -- owner could be admitted beside legacy READY metadata and alias the same physical tree
    -- on a case-insensitive filesystem.
    INSERT INTO astralplane_blob_owner_state (
        owner_id,
        state,
        version,
        retired_at,
        updated_at
    )
    SELECT DISTINCT
        attachment.user_id,
        'active',
        0,
        NULL::TIMESTAMPTZ,
        clock_timestamp()
    FROM user_attachments AS attachment
    """.strip(),
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_astralplane_blob_owner_state_casefold
    ON astralplane_blob_owner_state (lower(owner_id))
    """.strip(),
    """
    DO $astralplane_pending_materialization_postcondition$
    DECLARE
        invalid_columns TEXT;
        invalid_constraints TEXT;
        invalid_index BOOLEAN;
        invalid_casefold_index BOOLEAN;
    BEGIN
        SELECT string_agg(required.column_name, ', ' ORDER BY required.column_name)
        INTO invalid_columns
        FROM (
            VALUES
                ('materialization_state', 'text'::regtype, TRUE),
                ('materialization_lease_id', 'text'::regtype, FALSE),
                ('materialization_lease_version', 'bigint'::regtype, FALSE),
                ('materialization_lease_expires_at',
                    'timestamp with time zone'::regtype, FALSE),
                ('materialization_max_bytes', 'bigint'::regtype, FALSE),
                ('materialization_storage_key', 'text'::regtype, FALSE)
        ) AS required(column_name, type_oid, not_null)
        LEFT JOIN pg_attribute AS attribute
          ON attribute.attrelid = 'user_attachments'::regclass
         AND attribute.attname = required.column_name
         AND attribute.attnum > 0
         AND NOT attribute.attisdropped
        WHERE attribute.attname IS NULL
           OR attribute.atttypid <> required.type_oid
           OR attribute.attnotnull <> required.not_null;
        IF invalid_columns IS NOT NULL THEN
            RAISE EXCEPTION 'attachment materialization columns are incompatible: %',
                invalid_columns USING ERRCODE = '42P16';
        END IF;

        IF EXISTS (
            SELECT 1
            FROM pg_attribute AS attribute
            JOIN pg_attrdef AS default_record
              ON default_record.adrelid = attribute.attrelid
             AND default_record.adnum = attribute.attnum
            WHERE attribute.attrelid = 'user_attachments'::regclass
              AND attribute.attname = 'materialization_state'
        ) THEN
            RAISE EXCEPTION 'attachment materialization state must not have a default'
                USING ERRCODE = '42P16';
        END IF;

        SELECT string_agg(required.constraint_name, ', '
            ORDER BY required.constraint_name)
        INTO invalid_constraints
        FROM (
            VALUES
                ('user_attachments'::regclass,
                    'user_attachments_materialization_state_check'),
                ('astralplane_purge_tombstone'::regclass,
                    'astralplane_purge_tombstone_target_scope_check'),
                ('astralplane_purge_tombstone'::regclass,
                    'astralplane_purge_tombstone_target_shape_check'),
                ('astralplane_blob_owner_state'::regclass,
                    'astralplane_blob_owner_state_owner_id_check'),
                ('astralplane_blob_owner_state'::regclass,
                    'astralplane_blob_owner_state_state_check'),
                ('astralplane_blob_owner_state'::regclass,
                    'astralplane_blob_owner_state_version_check'),
                ('astralplane_blob_owner_state'::regclass,
                    'astralplane_blob_owner_state_retired_at_check')
        ) AS required(table_oid, constraint_name)
        LEFT JOIN pg_constraint AS constraint_record
          ON constraint_record.conrelid = required.table_oid
         AND constraint_record.conname = required.constraint_name
         AND constraint_record.contype = 'c'
         AND constraint_record.convalidated
        WHERE constraint_record.oid IS NULL;
        IF invalid_constraints IS NOT NULL THEN
            RAISE EXCEPTION 'blob lifecycle constraints are incompatible: %',
                invalid_constraints USING ERRCODE = '42P16';
        END IF;

        SELECT NOT (
            index_record.indisvalid
            AND index_record.indisready
            AND NOT index_record.indisunique
            AND pg_get_expr(
                index_record.indpred,
                index_record.indrelid,
                TRUE
            ) = 'materialization_state = ''pending''::text AND deleted_at IS NULL'
            AND ARRAY(
                SELECT attribute.attname
                FROM unnest(index_record.indkey::smallint[]) WITH ORDINALITY
                    AS key(attnum, ordinal)
                JOIN pg_attribute AS attribute
                  ON attribute.attrelid = index_record.indrelid
                 AND attribute.attnum = key.attnum
                ORDER BY key.ordinal
            ) = ARRAY[
                'materialization_lease_expires_at',
                'attachment_id'
            ]::name[]
        )
        INTO invalid_index
        FROM pg_index AS index_record
        WHERE index_record.indexrelid =
            to_regclass('idx_user_attachments_pending_materialization_expiry');
        IF invalid_index IS DISTINCT FROM FALSE THEN
            RAISE EXCEPTION 'pending materialization recovery index is incompatible'
                USING ERRCODE = '42P16';
        END IF;

        SELECT NOT (
            index_record.indisvalid
            AND index_record.indisready
            AND index_record.indisunique
            AND index_record.indnkeyatts = 1
            AND pg_get_indexdef(index_record.indexrelid, 1, TRUE)
                = 'lower(attachment_id)'
            AND index_record.indpred IS NULL
        )
        INTO invalid_casefold_index
        FROM pg_index AS index_record
        WHERE index_record.indexrelid =
            to_regclass('uq_user_attachments_attachment_id_casefold');
        IF invalid_casefold_index IS DISTINCT FROM FALSE THEN
            RAISE EXCEPTION 'attachment case-fold identity index is incompatible'
                USING ERRCODE = '42P16';
        END IF;
    END
    $astralplane_pending_materialization_postcondition$
    """.strip(),
)

PLANE_SCHEMA_075_STATEMENTS: Final = (
    """
DO $astralplane_075_clean_predecessor$
DECLARE
    observed_revision TEXT;
    invalid_object TEXT;
BEGIN
    SELECT value INTO observed_revision
    FROM schema_meta
    WHERE key = 'revision';
    IF observed_revision IS DISTINCT FROM '074.004' THEN
        RAISE EXCEPTION '075.001 requires a clean 074.004 predecessor; found %',
            COALESCE(observed_revision, '<empty>') USING ERRCODE = '42P16';
    END IF;
    IF to_regclass('voice_session') IS NULL THEN
        RAISE EXCEPTION '075.001 requires the voice_session predecessor table'
            USING ERRCODE = '42P16';
    END IF;
    IF EXISTS (
        SELECT 1
        FROM pg_attribute
        WHERE attrelid = 'voice_session'::regclass
          AND attname = 'speech_backend'
          AND attnum > 0
          AND NOT attisdropped
    ) THEN
        RAISE EXCEPTION '075.001 requires speech_backend to be absent from 074.004'
            USING ERRCODE = '42P16';
    END IF;

    SELECT string_agg(required.column_name, ', ' ORDER BY required.column_name)
    INTO invalid_object
    FROM (
        VALUES
            ('transport', 'text'::regtype::oid, TRUE),
            ('room_name', 'text'::regtype::oid, TRUE),
            ('participant_identity', 'text'::regtype::oid, TRUE),
            ('worker_identity', 'text'::regtype::oid, FALSE),
            ('media_grant_nonce_hash', 'bytea'::regtype::oid, TRUE),
            ('media_grant_expires_at', 'timestamptz'::regtype::oid, TRUE),
            ('media_grant_consumed_at', 'timestamptz'::regtype::oid, FALSE),
            ('last_media_refresh_id', 'uuid'::regtype::oid, FALSE),
            ('media_grant_issued_at', 'timestamptz'::regtype::oid, TRUE),
            ('worker_assignment_id', 'uuid'::regtype::oid, FALSE),
            ('worker_rtc_grant_revision', 'int8'::regtype::oid, TRUE),
            ('worker_rtc_grant_issued_at', 'timestamptz'::regtype::oid, FALSE),
            ('worker_rtc_grant_expires_at', 'timestamptz'::regtype::oid, FALSE)
    ) AS required(column_name, type_oid, not_null)
    LEFT JOIN pg_attribute AS attribute
      ON attribute.attrelid = 'voice_session'::regclass
     AND attribute.attname = required.column_name
     AND attribute.attnum > 0
     AND NOT attribute.attisdropped
    WHERE attribute.attname IS NULL
       OR attribute.atttypid <> required.type_oid
       OR attribute.attnotnull <> required.not_null;
    IF invalid_object IS NOT NULL THEN
        RAISE EXCEPTION '075.001 voice predecessor columns are incompatible: %',
            invalid_object USING ERRCODE = '42P16';
    END IF;

    IF (
        SELECT pg_get_expr(default_record.adbin, default_record.adrelid, TRUE)
        FROM pg_attribute AS attribute
        LEFT JOIN pg_attrdef AS default_record
          ON default_record.adrelid = attribute.attrelid
         AND default_record.adnum = attribute.attnum
        WHERE attribute.attrelid = 'voice_session'::regclass
          AND attribute.attname = 'worker_rtc_grant_revision'
          AND attribute.attnum > 0
          AND NOT attribute.attisdropped
    ) IS DISTINCT FROM '1' THEN
        RAISE EXCEPTION '075.001 worker grant revision predecessor is incompatible'
            USING ERRCODE = '42P16';
    END IF;

    SELECT string_agg(required.constraint_name, ', ' ORDER BY required.constraint_name)
    INTO invalid_object
    FROM (
        VALUES
            ('voice_session_transport_check',
             '0420e327861bd7015033af7aa2d7c7af'),
            ('voice_session_identity_065_check',
             '06316b7e99a1b1a3b0bd0a9efbb13c97'),
            ('voice_session_revisions_065_check',
             '862dce9f94228085078b2b262125787d'),
            ('voice_session_media_grant_065_check',
             'd3c2516b0392df7259346adb34771135'),
            ('voice_session_worker_grant_065_check',
             '71cfd36a2a3b22c427388ed8f1d07a04')
    ) AS required(constraint_name, expression_md5)
    LEFT JOIN pg_constraint AS constraint_record
      ON constraint_record.conrelid = 'voice_session'::regclass
     AND constraint_record.conname = required.constraint_name
     AND constraint_record.contype = 'c'
    WHERE constraint_record.oid IS NULL
       OR NOT constraint_record.convalidated
       OR md5(pg_get_expr(
            constraint_record.conbin,
            constraint_record.conrelid,
            TRUE
       )) <> required.expression_md5;
    IF invalid_object IS NOT NULL THEN
        RAISE EXCEPTION '075.001 voice predecessor constraints are incompatible: %',
            invalid_object USING ERRCODE = '42P16';
    END IF;
END
$astralplane_075_clean_predecessor$
""".strip(),
    "ALTER TABLE voice_session ADD COLUMN IF NOT EXISTS speech_backend TEXT",
    "UPDATE voice_session SET speech_backend = 'llm_factory' WHERE speech_backend IS NULL",
    "ALTER TABLE voice_session ALTER COLUMN speech_backend SET NOT NULL",
    """
ALTER TABLE voice_session
    ALTER COLUMN room_name DROP NOT NULL,
    ALTER COLUMN participant_identity DROP NOT NULL,
    ALTER COLUMN media_grant_nonce_hash DROP NOT NULL,
    ALTER COLUMN media_grant_expires_at DROP NOT NULL,
    ALTER COLUMN media_grant_issued_at DROP NOT NULL,
    ALTER COLUMN worker_rtc_grant_revision DROP NOT NULL,
    ALTER COLUMN worker_rtc_grant_revision DROP DEFAULT
""".strip(),
    """
DO $astralplane_075_constraints$
BEGIN
    ALTER TABLE voice_session
        DROP CONSTRAINT voice_session_transport_check;
    ALTER TABLE voice_session
        DROP CONSTRAINT voice_session_identity_065_check;
    ALTER TABLE voice_session
        DROP CONSTRAINT voice_session_revisions_065_check;
    ALTER TABLE voice_session
        DROP CONSTRAINT voice_session_media_grant_065_check;
    ALTER TABLE voice_session
        DROP CONSTRAINT voice_session_worker_grant_065_check;

    ALTER TABLE voice_session
        ADD CONSTRAINT voice_session_identity_075_check CHECK (
            length(btrim(user_id)) BETWEEN 1 AND 512
            AND (
                worker_identity IS NULL
                OR length(worker_identity) BETWEEN 1 AND 255
            )
            AND length(visible_chat_id) BETWEEN 1 AND 255
            AND (
                control_owner_id IS NULL
                OR length(control_owner_id) BETWEEN 1 AND 128
            )
        ) NOT VALID;
    ALTER TABLE voice_session
        ADD CONSTRAINT voice_session_revisions_075_check CHECK (
            generation >= 1
            AND media_grant_revision >= 1
            AND chat_context_revision >= 1
            AND (
                (
                    applied_visible_chat_id IS NULL
                    AND applied_chat_context_revision IS NULL
                )
                OR (
                    applied_visible_chat_id IS NOT NULL
                    AND length(applied_visible_chat_id) BETWEEN 1 AND 255
                    AND applied_chat_context_revision IS NOT NULL
                    AND applied_chat_context_revision BETWEEN 1
                        AND chat_context_revision
                )
            )
        ) NOT VALID;
    ALTER TABLE voice_session
        ADD CONSTRAINT voice_session_speech_backend_075_check CHECK (
            (
                speech_backend = 'llm_factory'
                AND transport IN ('livekit', 'watch_pcm_websocket')
                AND room_name IS NOT NULL
                AND length(room_name) BETWEEN 1 AND 255
                AND participant_identity IS NOT NULL
                AND length(participant_identity) BETWEEN 1 AND 255
                AND media_grant_nonce_hash IS NOT NULL
                AND octet_length(media_grant_nonce_hash) = 32
                AND media_grant_issued_at IS NOT NULL
                AND media_grant_expires_at IS NOT NULL
                AND media_grant_expires_at > media_grant_issued_at
                AND (
                    media_grant_consumed_at IS NULL
                    OR media_grant_consumed_at >= media_grant_issued_at
                )
                AND worker_rtc_grant_revision IS NOT NULL
                AND worker_rtc_grant_revision >= 1
                AND (
                    (
                        worker_identity IS NULL
                        AND worker_assignment_id IS NULL
                        AND worker_rtc_grant_issued_at IS NULL
                        AND worker_rtc_grant_expires_at IS NULL
                    )
                    OR (
                        worker_identity IS NOT NULL
                        AND worker_assignment_id IS NOT NULL
                        AND worker_rtc_grant_issued_at IS NOT NULL
                        AND worker_rtc_grant_expires_at IS NOT NULL
                        AND worker_rtc_grant_expires_at
                            > worker_rtc_grant_issued_at
                    )
                )
            )
            OR (
                speech_backend = 'client_local'
                AND transport = 'client_local'
                AND room_name IS NULL
                AND participant_identity IS NULL
                AND worker_identity IS NULL
                AND media_grant_nonce_hash IS NULL
                AND media_grant_expires_at IS NULL
                AND media_grant_consumed_at IS NULL
                AND last_media_refresh_id IS NULL
                AND media_grant_issued_at IS NULL
                AND worker_assignment_id IS NULL
                AND worker_rtc_grant_revision IS NULL
                AND worker_rtc_grant_issued_at IS NULL
                AND worker_rtc_grant_expires_at IS NULL
            )
        ) NOT VALID;

    ALTER TABLE voice_session
        VALIDATE CONSTRAINT voice_session_identity_075_check;
    ALTER TABLE voice_session
        VALIDATE CONSTRAINT voice_session_revisions_075_check;
    ALTER TABLE voice_session
        VALIDATE CONSTRAINT voice_session_speech_backend_075_check;
END
$astralplane_075_constraints$
""".strip(),
    """
DO $astralplane_075_postcondition$
DECLARE
    invalid_object TEXT;
BEGIN
    SELECT string_agg(required.column_name, ', ' ORDER BY required.column_name)
    INTO invalid_object
    FROM (
        VALUES
            ('speech_backend', 'text'::regtype::oid, TRUE),
            ('transport', 'text'::regtype::oid, TRUE),
            ('room_name', 'text'::regtype::oid, FALSE),
            ('participant_identity', 'text'::regtype::oid, FALSE),
            ('media_grant_nonce_hash', 'bytea'::regtype::oid, FALSE),
            ('media_grant_expires_at', 'timestamptz'::regtype::oid, FALSE),
            ('media_grant_issued_at', 'timestamptz'::regtype::oid, FALSE),
            ('worker_rtc_grant_revision', 'int8'::regtype::oid, FALSE)
    ) AS required(column_name, type_oid, not_null)
    LEFT JOIN pg_attribute AS attribute
      ON attribute.attrelid = 'voice_session'::regclass
     AND attribute.attname = required.column_name
     AND attribute.attnum > 0
     AND NOT attribute.attisdropped
    WHERE attribute.attname IS NULL
       OR attribute.atttypid <> required.type_oid
       OR attribute.attnotnull <> required.not_null;
    IF invalid_object IS NOT NULL THEN
        RAISE EXCEPTION 'voice speech backend columns are incompatible: %',
            invalid_object USING ERRCODE = '42P16';
    END IF;

    IF (
        SELECT pg_get_expr(default_record.adbin, default_record.adrelid, TRUE)
        FROM pg_attribute AS attribute
        LEFT JOIN pg_attrdef AS default_record
          ON default_record.adrelid = attribute.attrelid
         AND default_record.adnum = attribute.attnum
        WHERE attribute.attrelid = 'voice_session'::regclass
          AND attribute.attname = 'worker_rtc_grant_revision'
          AND attribute.attnum > 0
          AND NOT attribute.attisdropped
    ) IS NOT NULL THEN
        RAISE EXCEPTION 'voice worker grant revision default is incompatible'
            USING ERRCODE = '42P16';
    END IF;

    SELECT string_agg(required.constraint_name, ', ' ORDER BY required.constraint_name)
    INTO invalid_object
    FROM (
        VALUES
            ('voice_session_identity_075_check',
             'eb4bb8a941e65dea22304fc9cf908d0d'),
            ('voice_session_revisions_075_check',
             'd081758b7eaf338f8c9c7d87c79bcfbe'),
            ('voice_session_speech_backend_075_check',
             'e229755e487ab22f36039c3efec82204')
    ) AS required(constraint_name, expression_md5)
    LEFT JOIN pg_constraint AS constraint_record
      ON constraint_record.conrelid = 'voice_session'::regclass
     AND constraint_record.conname = required.constraint_name
     AND constraint_record.contype = 'c'
    WHERE constraint_record.oid IS NULL
       OR NOT constraint_record.convalidated
       OR md5(pg_get_expr(
            constraint_record.conbin,
            constraint_record.conrelid,
            TRUE
       )) <> required.expression_md5;
    IF invalid_object IS NOT NULL THEN
        RAISE EXCEPTION 'voice speech backend constraint is incompatible: %',
            invalid_object USING ERRCODE = '42P16';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'voice_session'::regclass
          AND conname IN (
              'voice_session_transport_check',
              'voice_session_identity_065_check',
              'voice_session_revisions_065_check',
              'voice_session_media_grant_065_check',
              'voice_session_worker_grant_065_check'
          )
    ) THEN
        RAISE EXCEPTION 'legacy voice remote-field constraints remain installed'
            USING ERRCODE = '42P16';
    END IF;
END
$astralplane_075_postcondition$
""".strip(),
)


@dataclass(frozen=True, slots=True)
class Migration:
    name: str
    source_revisions: tuple[str | None, ...]
    target_revision: str
    checksum: str
    operation: MigrationCallable

    def __post_init__(self) -> None:
        if not self.name or not self.name.isascii():
            raise MigrationDefinitionError("migration name must be non-empty ASCII")
        if not isinstance(self.source_revisions, tuple) or not self.source_revisions:
            raise MigrationDefinitionError("migration must declare at least one source revision")
        normalized_sources = tuple(
            None if source is None else validate_revision(source, field="migration source")
            for source in self.source_revisions
        )
        if len(set(normalized_sources)) != len(normalized_sources):
            raise MigrationDefinitionError("migration source revisions must be unique")
        target = validate_revision(self.target_revision, field="migration target")
        if target in normalized_sources:
            raise MigrationDefinitionError("migration target must differ from every source")
        if _SHA256_PATTERN.fullmatch(self.checksum) is None:
            raise MigrationDefinitionError("migration checksum must be lowercase SHA-256")
        if not callable(self.operation):
            raise MigrationDefinitionError("migration operation must be callable")

    def apply(self, transaction: Transaction) -> None:
        self.operation(transaction)


class MigrationRegistry:
    def __init__(
        self,
        migrations: tuple[Migration, ...],
        *,
        current_schema_verifier: MigrationCallable | None = None,
        current_schema_verifier_checksum: str | None = None,
        predecessor_schema_verifier: Callable[[Transaction, str | None], None] | None = None,
        predecessor_schema_verifier_checksum: str | None = None,
    ) -> None:
        if not isinstance(migrations, tuple) or not migrations:
            raise MigrationDefinitionError("migration registry must not be empty")
        by_source: dict[str | None, Migration] = {}
        names: set[str] = set()
        for migration in migrations:
            if migration.name in names:
                raise MigrationDefinitionError("migration names must be unique")
            names.add(migration.name)
            for source in migration.source_revisions:
                if source in by_source:
                    raise MigrationDefinitionError(
                        f"multiple migrations claim source revision {source!r}"
                    )
                by_source[source] = migration
        self._migrations = tuple(migrations)
        self._by_source = by_source
        if (current_schema_verifier is None) != (current_schema_verifier_checksum is None):
            raise MigrationDefinitionError(
                "current schema verifier and checksum must be declared together"
            )
        if current_schema_verifier is not None and not callable(current_schema_verifier):
            raise MigrationDefinitionError("current schema verifier must be callable")
        if (
            current_schema_verifier_checksum is not None
            and _SHA256_PATTERN.fullmatch(current_schema_verifier_checksum) is None
        ):
            raise MigrationDefinitionError(
                "current schema verifier checksum must be lowercase SHA-256"
            )
        self._current_schema_verifier = current_schema_verifier
        self._current_schema_verifier_checksum = current_schema_verifier_checksum
        if (predecessor_schema_verifier is None) != (predecessor_schema_verifier_checksum is None):
            raise MigrationDefinitionError(
                "predecessor schema verifier and checksum must be declared together"
            )
        if predecessor_schema_verifier is not None and not callable(predecessor_schema_verifier):
            raise MigrationDefinitionError("predecessor schema verifier must be callable")
        if (
            predecessor_schema_verifier_checksum is not None
            and _SHA256_PATTERN.fullmatch(predecessor_schema_verifier_checksum) is None
        ):
            raise MigrationDefinitionError(
                "predecessor schema verifier checksum must be lowercase SHA-256"
            )
        self._predecessor_schema_verifier = predecessor_schema_verifier
        self._predecessor_schema_verifier_checksum = predecessor_schema_verifier_checksum
        manifest = [
            {
                "checksum": migration.checksum,
                "name": migration.name,
                "source_revisions": [
                    "<empty>" if source is None else source for source in migration.source_revisions
                ],
                "target_revision": migration.target_revision,
            }
            for migration in sorted(migrations, key=lambda item: item.name)
        ]
        if current_schema_verifier_checksum is not None:
            manifest.append(
                {
                    "checksum": current_schema_verifier_checksum,
                    "name": "@current-schema-verifier",
                    "source_revisions": [],
                    "target_revision": "@current",
                }
            )
        if predecessor_schema_verifier_checksum is not None:
            manifest.append(
                {
                    "checksum": predecessor_schema_verifier_checksum,
                    "name": "@predecessor-schema-verifier",
                    "source_revisions": [],
                    "target_revision": "@predecessor",
                }
            )
        canonical = json.dumps(
            manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        self.digest = hashlib.sha256(canonical).hexdigest()

    @property
    def migrations(self) -> tuple[Migration, ...]:
        return self._migrations

    @property
    def current_schema_verifier(self) -> MigrationCallable | None:
        return self._current_schema_verifier

    @property
    def current_schema_verifier_checksum(self) -> str | None:
        return self._current_schema_verifier_checksum

    @property
    def predecessor_schema_verifier_checksum(self) -> str | None:
        return self._predecessor_schema_verifier_checksum

    def next_after(self, revision: str | None) -> Migration | None:
        return self._by_source.get(revision)

    def verify_current(self, transaction: Transaction) -> None:
        if self._current_schema_verifier is None:
            raise MigrationDefinitionError(
                "migration registry has no current schema structural verifier"
            )
        self._current_schema_verifier(transaction)

    def verify_predecessor(
        self,
        transaction: Transaction,
        revision: str | None,
    ) -> None:
        if self._predecessor_schema_verifier is not None:
            self._predecessor_schema_verifier(transaction, revision)


@dataclass(frozen=True, slots=True)
class MigrationReport:
    source_revision: str | None
    target_revision: str
    applied_steps: tuple[str, ...]
    already_current: bool
    migration_digest: str


class MigrationRunner:
    def __init__(
        self,
        database: PlaneDatabase,
        *,
        revision: DataPlaneRevision,
        registry: MigrationRegistry,
    ) -> None:
        if registry.digest != revision.migration_digest:
            raise MigrationDefinitionError(
                "migration registry digest does not match the declared data-plane revision",
                metadata={
                    "declared": revision.migration_digest,
                    "observed": registry.digest,
                },
            )
        if registry.current_schema_verifier is None:
            raise MigrationDefinitionError(
                "migration registry has no current schema structural verifier"
            )
        self._database = database
        self.revision = revision
        self.registry = registry

    def _read_metadata(self, transaction: Transaction) -> dict[str, str]:
        records = transaction.fetch_all(
            _READ_SCHEMA_META_SQL,
            (_SCHEMA_META_REVISION, _SCHEMA_META_DIGEST),
        )
        metadata: dict[str, str] = {}
        for record in records:
            key = str(record["key"])
            if key in metadata:
                raise SchemaRevisionError("schema metadata contains duplicate keys")
            metadata[key] = str(record["value"])
        return metadata

    @staticmethod
    def _write_metadata(transaction: Transaction, key: str, value: str) -> None:
        transaction.execute(_WRITE_SCHEMA_META_SQL, (key, value))

    def run(self, *, expected_revision: str) -> MigrationReport:
        expected = validate_revision(expected_revision, field="expected revision")
        if expected != self.revision.schema_revision:
            raise SchemaRevisionError(
                "composition expected a different data-plane revision",
                metadata={"declared": self.revision.schema_revision, "expected": expected},
            )

        lock_key, lock_id = self.revision.migration_lock
        with self._database.transaction() as transaction:
            transaction.fetch_one(_ADVISORY_LOCK_SQL, (lock_key, lock_id))
            transaction.execute(_CREATE_SCHEMA_META_SQL)
            metadata = self._read_metadata(transaction)
            source_revision = metadata.get(_SCHEMA_META_REVISION)
            if source_revision is not None:
                validate_revision(source_revision, field="stored schema revision")
            stored_digest = metadata.get(_SCHEMA_META_DIGEST)

            if source_revision == expected:
                if stored_digest is None:
                    raise SchemaRevisionError(
                        "current schema is missing migration-set evidence",
                        metadata={"revision": source_revision},
                    )
                if stored_digest != self.registry.digest:
                    raise SchemaRevisionError(
                        "current schema carries a different migration-set digest",
                        metadata={"revision": source_revision},
                    )
                self.registry.verify_current(transaction)
                return MigrationReport(
                    source_revision=source_revision,
                    target_revision=expected,
                    applied_steps=(),
                    already_current=True,
                    migration_digest=self.registry.digest,
                )

            accepted_predecessor_digest = self.revision.predecessor_digest_for(source_revision)
            if accepted_predecessor_digest is not None:
                if stored_digest is None:
                    raise SchemaRevisionError(
                        "predecessor schema is missing required migration-set evidence",
                        metadata={"revision": source_revision or "<empty>"},
                    )
                if stored_digest != accepted_predecessor_digest:
                    raise SchemaRevisionError(
                        "predecessor schema carries an unrecognized migration-set digest",
                        metadata={"revision": source_revision or "<empty>"},
                    )
            elif stored_digest is not None:
                raise SchemaRevisionError(
                    "predecessor schema carries an unrecognized migration-set digest",
                    metadata={"revision": source_revision or "<empty>"},
                )

            self.registry.verify_predecessor(transaction, source_revision)

            applied: list[str] = []
            observed = source_revision
            visited: set[str | None] = set()
            while observed != expected:
                if observed in visited:
                    raise MigrationDefinitionError("migration registry contains a cycle")
                visited.add(observed)
                migration = self.registry.next_after(observed)
                if migration is None:
                    raise SchemaRevisionError(
                        "no declared migration path from the observed schema",
                        metadata={"observed": observed or "<empty>", "target": expected},
                    )
                migration.apply(transaction)
                observed = migration.target_revision
                self._write_metadata(transaction, _SCHEMA_META_REVISION, observed)
                applied.append(migration.name)

            self.registry.verify_current(transaction)
            self._write_metadata(transaction, _SCHEMA_META_DIGEST, self.registry.digest)
            return MigrationReport(
                source_revision=source_revision,
                target_revision=expected,
                applied_steps=tuple(applied),
                already_current=False,
                migration_digest=self.registry.digest,
            )


def _apply_plane_schema_067(transaction: Transaction) -> None:
    for statement in PLANE_SCHEMA_067_STATEMENTS:
        transaction.execute(statement)


def _apply_plane_schema_074(transaction: Transaction) -> None:
    for statement in PLANE_SCHEMA_074_STATEMENTS:
        transaction.execute(statement)


def _apply_plane_schema_074_002(transaction: Transaction) -> None:
    for statement in PLANE_SCHEMA_074_002_STATEMENTS:
        transaction.execute(statement)


def _apply_plane_schema_074_003(transaction: Transaction) -> None:
    for statement in PLANE_SCHEMA_074_003_STATEMENTS:
        transaction.execute(statement)


def _apply_plane_schema_074_004(transaction: Transaction) -> None:
    for statement in PLANE_SCHEMA_074_004_STATEMENTS:
        transaction.execute(statement)


def _apply_plane_schema_075(transaction: Transaction) -> None:
    for statement in PLANE_SCHEMA_075_STATEMENTS:
        transaction.execute(statement)


def _apply_plane_schema_079(transaction: Transaction) -> None:
    _verify_predecessor_plane_schema(transaction, "075.001")
    for statement in ASSIGNMENT_SCHEMA_STATEMENTS:
        transaction.execute(statement)


def _apply_plane_schema_088(transaction: Transaction) -> None:
    _verify_predecessor_plane_schema(transaction, "079.001")
    for statement in OPERATION_SCHEMA_STATEMENTS:
        transaction.execute(statement)


PLANE_SCHEMA_088_002_STATEMENTS: Final = (
    "ALTER TABLE web_session ADD COLUMN incarnation_id UUID NOT NULL DEFAULT gen_random_uuid()",
    "ALTER TABLE web_session ADD CONSTRAINT web_session_incarnation_unique UNIQUE (incarnation_id)",
    """
ALTER TABLE web_session ADD CONSTRAINT web_session_incarnation_uuid4 CHECK (
    incarnation_id::text ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
)
""".strip(),
)


def _apply_plane_schema_088_002(transaction: Transaction) -> None:
    _verify_predecessor_plane_schema(transaction, "088.001")
    for statement in PLANE_SCHEMA_088_002_STATEMENTS:
        transaction.execute(statement)


PLANE_SCHEMA_088_003_STATEMENTS: Final = (
    "ALTER TABLE web_session ADD COLUMN issuing_issuer TEXT",
    "ALTER TABLE web_session ADD COLUMN issuing_client_id TEXT",
    r"""
ALTER TABLE web_session ADD CONSTRAINT web_session_issuing_metadata CHECK (
    (issuing_issuer IS NULL AND issuing_client_id IS NULL)
    OR (
        issuing_issuer IS NOT NULL AND issuing_client_id IS NOT NULL
        AND char_length(issuing_issuer) BETWEEN 1 AND 2048
        AND char_length(issuing_client_id) BETWEEN 1 AND 256
        AND issuing_issuer = btrim(issuing_issuer,
            U&'\0020\00A0\1680\2000\2001\2002\2003\2004\2005\2006'
            || U&'\2007\2008\2009\200A\2028\2029\202F\205F\3000')
        AND issuing_client_id = btrim(issuing_client_id,
            U&'\0020\00A0\1680\2000\2001\2002\2003\2004\2005\2006'
            || U&'\2007\2008\2009\200A\2028\2029\202F\205F\3000')
        AND issuing_issuer !~ U&'[\0001-\001F\007F-\009F]'
        AND issuing_client_id !~ U&'[\0001-\001F\007F-\009F]'
    )
)
""".strip(),
    "ALTER TABLE auth_revocation_queue ADD COLUMN issuing_issuer TEXT",
    r"""
ALTER TABLE auth_revocation_queue ADD CONSTRAINT auth_revocation_queue_issuing_metadata CHECK (
    issuing_issuer IS NULL
    OR (
        issuing_issuer IS NOT NULL AND client_id IS NOT NULL
        AND char_length(issuing_issuer) BETWEEN 1 AND 2048
        AND char_length(client_id) BETWEEN 1 AND 256
        AND issuing_issuer = btrim(issuing_issuer,
            U&'\0020\00A0\1680\2000\2001\2002\2003\2004\2005\2006'
            || U&'\2007\2008\2009\200A\2028\2029\202F\205F\3000')
        AND client_id = btrim(client_id,
            U&'\0020\00A0\1680\2000\2001\2002\2003\2004\2005\2006'
            || U&'\2007\2008\2009\200A\2028\2029\202F\205F\3000')
        AND issuing_issuer !~ U&'[\0001-\001F\007F-\009F]'
        AND client_id !~ U&'[\0001-\001F\007F-\009F]'
    )
)
""".strip(),
)


def _apply_plane_schema_088_003(transaction: Transaction) -> None:
    _verify_predecessor_plane_schema(transaction, "088.002")
    for statement in PLANE_SCHEMA_088_003_STATEMENTS:
        transaction.execute(statement)


def _apply_plane_schema_088_004(transaction: Transaction) -> None:
    _verify_predecessor_plane_schema(transaction, "088.003")
    for statement in DECLARATIVE_AGENT_SCHEMA_STATEMENTS:
        transaction.execute(statement)


def _apply_plane_schema_088_005(transaction: Transaction) -> None:
    _verify_predecessor_plane_schema(transaction, "088.004")
    for statement in GUIDANCE_SCHEMA_STATEMENTS:
        transaction.execute(statement)


def _apply_plane_schema_088_006(transaction: Transaction) -> None:
    _verify_predecessor_plane_schema(transaction, "088.005")
    for statement in SELECTED_INPUT_SCHEMA_STATEMENTS:
        transaction.execute(statement)


def _apply_plane_schema_088_007(transaction: Transaction) -> None:
    _verify_predecessor_plane_schema(transaction, "088.006")
    for statement in SCHEDULER_POLICY_SCHEMA_STATEMENTS:
        transaction.execute(statement)


def _apply_plane_schema_088_008(transaction: Transaction) -> None:
    _verify_predecessor_plane_schema(transaction, "088.007")
    for statement in FRAMEWORK_CREDENTIAL_SCHEMA_STATEMENTS:
        transaction.execute(statement)


def _apply_plane_schema_089_001(transaction: Transaction) -> None:
    _verify_predecessor_plane_schema(transaction, "088.008")
    for statement in TYPESAFE_CREDENTIAL_SCHEMA_STATEMENTS:
        transaction.execute(statement)


def _apply_plane_schema_089_002(transaction: Transaction) -> None:
    _verify_predecessor_plane_schema(transaction, "089.001")
    for statement in MESH_SCHEMA_STATEMENTS:
        transaction.execute(statement)


def _apply_plane_schema_089_003(transaction: Transaction) -> None:
    _verify_predecessor_plane_schema(transaction, "089.002")
    for statement in STOP_SCHEMA_STATEMENTS:
        transaction.execute(statement)


def _apply_plane_schema_089_004(transaction: Transaction) -> None:
    _verify_predecessor_plane_schema(transaction, "089.003")
    for statement in COMPLETION_WAKE_SCHEMA_STATEMENTS:
        transaction.execute(statement)


def _apply_plane_schema_089_005(transaction: Transaction) -> None:
    _verify_predecessor_plane_schema(transaction, "089.004")
    for statement in NOTIFICATION_SCHEMA_STATEMENTS:
        transaction.execute(statement)


def _apply_plane_schema_089_006(transaction: Transaction) -> None:
    _verify_predecessor_plane_schema(transaction, "089.005")
    for statement in ATLAS_PAGE_SCHEMA_STATEMENTS:
        transaction.execute(statement)


def _apply_plane_schema_089_007(transaction: Transaction) -> None:
    _verify_predecessor_plane_schema(transaction, "089.006")
    for statement in ATLAS_REVISION_SCHEMA_STATEMENTS:
        transaction.execute(statement)


def _apply_plane_schema_089_008(transaction: Transaction) -> None:
    _verify_predecessor_plane_schema(transaction, "089.007")
    for statement in ATLAS_LINK_SCHEMA_STATEMENTS:
        transaction.execute(statement)


def _apply_plane_schema_089_009(transaction: Transaction) -> None:
    _verify_predecessor_plane_schema(transaction, "089.008")
    for statement in CAPABILITY_PUBLICATION_SCHEMA_STATEMENTS:
        transaction.execute(statement)


def _apply_plane_schema_089_010(transaction: Transaction) -> None:
    _verify_predecessor_plane_schema(transaction, "089.009")
    for statement in HARNESS_BINDING_SCHEMA_STATEMENTS:
        transaction.execute(statement)


def _apply_plane_schema_089_011(transaction: Transaction) -> None:
    _verify_predecessor_plane_schema(transaction, "089.010")
    for statement in PUSH_DELIVERY_SCHEMA_STATEMENTS:
        transaction.execute(statement)


def _apply_plane_schema_089_012(transaction: Transaction) -> None:
    _verify_predecessor_plane_schema(transaction, "089.011")
    for statement in OBSERVATION_SCHEMA_STATEMENTS:
        transaction.execute(statement)


CURRENT_SCHEMA_VERIFICATION_STATEMENTS: Final = (
    PLANE_SCHEMA_067_STATEMENTS[-1],
    PLANE_SCHEMA_074_STATEMENTS[-1],
    PLANE_SCHEMA_074_002_STATEMENTS[-1],
    PLANE_SCHEMA_074_003_STATEMENTS[-1],
    PLANE_SCHEMA_074_004_STATEMENTS[-1],
    PLANE_SCHEMA_075_STATEMENTS[-1],
)


def _apply_plane_schema_089_013(transaction: Transaction) -> None:
    _verify_predecessor_plane_schema(transaction, "089.012")
    for statement in PROJECT_SCHEMA_STATEMENTS:
        transaction.execute(statement)


def _apply_plane_schema_089_014(transaction: Transaction) -> None:
    _verify_predecessor_plane_schema(transaction, "089.013")
    for statement in HARNESS_CAPTURE_SCHEMA_STATEMENTS:
        transaction.execute(statement)


def _apply_plane_schema_089_015(transaction: Transaction) -> None:
    _verify_predecessor_plane_schema(transaction, "089.014")
    for statement in AUDIT_CONVERSATION_SCHEMA_STATEMENTS:
        transaction.execute(statement)


def _apply_plane_schema_089_016(transaction: Transaction) -> None:
    _verify_predecessor_plane_schema(transaction, "089.015")
    for statement in CHAT_QUEUE_SCHEMA_STATEMENTS:
        transaction.execute(statement)


def _apply_plane_schema_089_017(transaction: Transaction) -> None:
    _verify_predecessor_plane_schema(transaction, "089.016")
    for statement in CONVERSATION_BRANCH_SCHEMA_STATEMENTS:
        transaction.execute(statement)


def _apply_plane_schema_089_018(transaction: Transaction) -> None:
    _verify_predecessor_plane_schema(transaction, "089.017")
    for statement in COMPUTER_USE_SCHEMA_STATEMENTS:
        transaction.execute(statement)


def _apply_plane_schema_089_019(transaction: Transaction) -> None:
    _verify_predecessor_plane_schema(transaction, "089.018")
    for statement in VERIFICATION_CREDENTIAL_SCHEMA_STATEMENTS:
        transaction.execute(statement)


def _apply_plane_schema_089_020(transaction: Transaction) -> None:
    _verify_predecessor_plane_schema(transaction, "089.019")
    for statement in CHAT_READ_RECEIPT_SCHEMA_STATEMENTS:
        transaction.execute(statement)


def _apply_plane_schema_089_021(transaction: Transaction) -> None:
    _verify_predecessor_plane_schema(transaction, "089.020")
    for statement in MESH_EVENT_SCHEMA_STATEMENTS:
        transaction.execute(statement)


def _apply_plane_schema_089_022(transaction: Transaction) -> None:
    _verify_predecessor_plane_schema(transaction, "089.021")
    for statement in RUN_LEDGER_SCHEMA_STATEMENTS:
        transaction.execute(statement)


CURRENT_SCHEMA_STRUCTURE_QUERY: Final = """
WITH owned_tables(table_name) AS (
    VALUES
        ('chat_project'),
        ('chat_project_membership'),
        ('tool_observation_page'),
        ('tool_observation'),
        ('notification_push_delivery'),
        ('notification_device_destination'),
        ('operation_harness_capture'),
        ('chat_queue_item'),
        ('chat_branch_head'),
        ('computer_session'),
        ('computer_task_grant'),
        ('computer_host_effect_intent'),
        ('computer_approval_fence'),
        ('verification_credential'),
        ('verification_challenge_consumption'),
        ('chat_read_receipt'),
        ('event_mesh'),
        ('event_feed'),
        ('event_entry'),
        ('event_fork_evidence'),
        ('event_body'),
        ('event_roster_entry'),
        ('event_checkpoint'),
        ('event_checkpoint_countersignature'),
        ('run_lease_entry'),
        ('run_goal_transition'),
        ('learning_ledger_entry'),
        ('operation_harness_binding'),
        ('capability_publication'),
        ('agent_host_session'),
        ('agent_ownership'),
        ('agent_runtime_instance'),
        ('agent_runtime_request'),
        ('agent_scopes'),
        ('agent_trust'),
        ('assignment_operation_receipt'),
        ('atlas_page'),
        ('atlas_page_command'),
        ('atlas_page_link'),
        ('atlas_page_link_command'),
        ('atlas_page_link_head'),
        ('atlas_page_revision'),
        ('assignment_guidance_reference'),
        ('assignment_guidance_selection'),
        ('assignment_selected_agent'),
        ('owner_skill_head'),
        ('owner_skill_revision'),
        ('owner_skill_catalog'),
        ('explicit_note_current'),
        ('astralplane_authority_binding'),
        ('astralplane_authority_lifecycle_operation'),
        ('astralplane_blob_owner_state'),
        ('astralplane_outbox'),
        ('astralplane_protected_effect_operation'),
        ('astralplane_purge_tombstone'),
        ('astralplane_receipt_claim'),
        ('astralplane_receipt_sequence_watermark'),
        ('astralplane_reconciliation_marker'),
        ('attachment_parser'),
        ('audit_entries'),
        ('audit_events'),
        ('audit_retention_anchor'),
        ('auth_revocation_queue'),
        ('background_task'),
        ('chat_files'),
        ('chat_steps'),
        ('chats'),
        ('component_feedback'),
        ('component_version'),
        ('consolidation_sweep'),
        ('conversation_commit'),
        ('draft_agents'),
        ('draft_artifact_publication'),
        ('draft_transition'),
        ('effect_ledger'),
        ('framework_credential'),
        ('interaction_log'),
        ('job_run'),
        ('knowledge_update_proposal'),
        ('latex_artifacts'),
        ('logs'),
        ('machine_credential'),
        ('maintenance_unit'),
        ('maintenance_unit_input'),
        ('memory_item'),
        ('memory_link'),
        ('mesh_enrollment_challenge'),
        ('mesh_enrollment_invitation'),
        ('mesh_member'),
        ('mesh_member_revocation'),
        ('mesh_public_identity'),
        ('mesh_record'),
        ('owner_stop_epoch'),
        ('owner_stop_operation_epoch'),
        ('peer_stop_acknowledgment'),
        ('message_attachment'),
        ('messages'),
        ('onboarding_state'),
        ('operation_admission_class'),
        ('operation_admission_slot'),
        ('operation_record'),
        ('operation_submission_result'),
        ('persistent_assignment'),
        ('persistent_assignment_event'),
        ('persistent_assignment_action'),
        ('persistent_assignment_activity'),
        ('quarantine_entry'),
        ('remote_machine'),
        ('remote_operation_proposal'),
        ('saved_components'),
        ('scheduled_job'),
        ('scheduled_job_policy'),
        ('scheduled_occurrence'),
        ('scheduled_occurrence_assignment'),
        ('schema_meta'),
        ('share_grant'),
        ('short_term_signal'),
        ('system_llm_config'),
        ('test_case_results'),
        ('test_evidence'),
        ('test_runs'),
        ('tool_overrides'),
        ('tool_permissions'),
        ('tool_quality_signal'),
        ('tracked_job'),
        ('tutorial_step'),
        ('tutorial_step_revision'),
        ('user_agent'),
        ('user_agent_revision'),
        ('user_agent_command_receipt'),
        ('user_attachments'),
        ('user_credentials'),
        ('user_data_sharing_acknowledgment'),
        ('user_llm_config'),
        ('user_offline_grant'),
        ('user_persona'),
        ('user_personalization'),
        ('user_preferences'),
        ('user_typesafe_credential'),
        ('users'),
        ('voice_session'),
        ('voice_turn'),
        ('web_session'),
        ('workspace_layout'),
        ('workspace_snapshot'),
        ('completion_subscription'),
        ('wake_receipt'),
        ('notification_preference'),
        ('notification_inbox_entry'),
        ('notification_delivery_identity')
),
owned_relations AS (
    SELECT
        owned_tables.table_name,
        table_record.oid AS relation_oid
    FROM owned_tables
    JOIN pg_namespace AS table_namespace
      ON table_namespace.nspname = current_schema()
    JOIN pg_class AS table_record
      ON table_record.relnamespace = table_namespace.oid
     AND table_record.relname = owned_tables.table_name
),
schema_shapes AS (
    SELECT
        'schema'::text AS object_kind,
        '<current>'::text AS object_identity,
        jsonb_build_object(
            'acl', ARRAY(
                SELECT jsonb_build_object(
                    'grantable', privilege.is_grantable,
                    'grantee', CASE
                        WHEN privilege.grantee = 0 THEN '<public>'
                        WHEN privilege.grantee = namespace_record.nspowner THEN '<owner>'
                        ELSE pg_get_userbyid(privilege.grantee)
                    END,
                    'grantor', CASE
                        WHEN privilege.grantor = namespace_record.nspowner THEN '<owner>'
                        ELSE pg_get_userbyid(privilege.grantor)
                    END,
                    'privilege', privilege.privilege_type
                )::text
                FROM aclexplode(
                    COALESCE(
                        namespace_record.nspacl,
                        acldefault('n', namespace_record.nspowner)
                    )
                ) AS privilege
                ORDER BY 1
            ),
            'ownerIsCurrentUser',
                pg_get_userbyid(namespace_record.nspowner) = current_user
        )::text AS object_definition
    FROM pg_namespace AS namespace_record
    WHERE namespace_record.nspname = current_schema()
),
catalog_dependencies AS NOT MATERIALIZED (
    SELECT
        dependency.classid AS object_class,
        dependency.objid AS object_id,
        dependency.refclassid AS reference_class,
        dependency.refobjid AS reference_id,
        CASE
            WHEN dependency_function.oid IS NOT NULL THEN
                'function:' || CASE
                    WHEN function_namespace.nspname = current_schema()
                    THEN '<current>'
                    ELSE function_namespace.nspname
                END || '.' || dependency_function.proname || '('
                || pg_get_function_identity_arguments(dependency_function.oid) || ')'
            WHEN dependency_type.oid IS NOT NULL THEN
                'type:' || CASE
                    WHEN type_namespace.nspname = current_schema() THEN '<current>'
                    ELSE type_namespace.nspname
                END || '.' || dependency_type.typname
            WHEN dependency_collation.oid IS NOT NULL THEN
                'collation:' || CASE
                    WHEN collation_namespace.nspname = current_schema() THEN '<current>'
                    ELSE collation_namespace.nspname
                END || '.' || dependency_collation.collname
            WHEN dependency_operator.oid IS NOT NULL THEN
                'operator:' || CASE
                    WHEN operator_namespace.nspname = current_schema() THEN '<current>'
                    ELSE operator_namespace.nspname
                END || '.' || dependency_operator.oprname || '('
                || COALESCE(
                    CASE
                        WHEN operator_left_namespace.nspname = current_schema()
                        THEN '<current>'
                        ELSE operator_left_namespace.nspname
                    END || '.' || operator_left_type.typname,
                    ''
                ) || ',' || COALESCE(
                    CASE
                        WHEN operator_right_namespace.nspname = current_schema()
                        THEN '<current>'
                        ELSE operator_right_namespace.nspname
                    END || '.' || operator_right_type.typname,
                    ''
                ) || ')'
            WHEN dependency_opclass.oid IS NOT NULL THEN
                'opclass:' || CASE
                    WHEN opclass_namespace.nspname = current_schema() THEN '<current>'
                    ELSE opclass_namespace.nspname
                END || '.' || dependency_opclass.opcname || '@'
                || opclass_access_method.amname
            WHEN dependency_relation.oid IS NOT NULL THEN
                'relation:' || CASE
                    WHEN relation_namespace.nspname = current_schema() THEN '<current>'
                    ELSE relation_namespace.nspname
                END || '.' || dependency_relation.relname || COALESCE(
                    '.' || dependency_attribute.attname,
                    ''
                )
            ELSE NULL
        END AS dependency_identity
    FROM pg_depend AS dependency
    LEFT JOIN pg_proc AS dependency_function
      ON dependency.refclassid = 'pg_proc'::regclass
     AND dependency_function.oid = dependency.refobjid
    LEFT JOIN pg_namespace AS function_namespace
      ON function_namespace.oid = dependency_function.pronamespace
    LEFT JOIN pg_type AS dependency_type
      ON dependency.refclassid = 'pg_type'::regclass
     AND dependency_type.oid = dependency.refobjid
    LEFT JOIN pg_namespace AS type_namespace
      ON type_namespace.oid = dependency_type.typnamespace
    LEFT JOIN pg_collation AS dependency_collation
      ON dependency.refclassid = 'pg_collation'::regclass
     AND dependency_collation.oid = dependency.refobjid
    LEFT JOIN pg_namespace AS collation_namespace
      ON collation_namespace.oid = dependency_collation.collnamespace
    LEFT JOIN pg_operator AS dependency_operator
      ON dependency.refclassid = 'pg_operator'::regclass
     AND dependency_operator.oid = dependency.refobjid
    LEFT JOIN pg_namespace AS operator_namespace
      ON operator_namespace.oid = dependency_operator.oprnamespace
    LEFT JOIN pg_type AS operator_left_type
      ON operator_left_type.oid = dependency_operator.oprleft
    LEFT JOIN pg_namespace AS operator_left_namespace
      ON operator_left_namespace.oid = operator_left_type.typnamespace
    LEFT JOIN pg_type AS operator_right_type
      ON operator_right_type.oid = dependency_operator.oprright
    LEFT JOIN pg_namespace AS operator_right_namespace
      ON operator_right_namespace.oid = operator_right_type.typnamespace
    LEFT JOIN pg_opclass AS dependency_opclass
      ON dependency.refclassid = 'pg_opclass'::regclass
     AND dependency_opclass.oid = dependency.refobjid
    LEFT JOIN pg_namespace AS opclass_namespace
      ON opclass_namespace.oid = dependency_opclass.opcnamespace
    LEFT JOIN pg_am AS opclass_access_method
      ON opclass_access_method.oid = dependency_opclass.opcmethod
    LEFT JOIN pg_class AS dependency_relation
      ON dependency.refclassid = 'pg_class'::regclass
     AND dependency_relation.oid = dependency.refobjid
    LEFT JOIN pg_namespace AS relation_namespace
      ON relation_namespace.oid = dependency_relation.relnamespace
    LEFT JOIN pg_attribute AS dependency_attribute
      ON dependency_attribute.attrelid = dependency_relation.oid
     AND dependency_attribute.attnum = dependency.refobjsubid
    WHERE dependency.classid IN (
        'pg_attrdef'::regclass,
        'pg_constraint'::regclass,
        'pg_class'::regclass,
        'pg_policy'::regclass
    )
),
table_shapes AS (
    SELECT
        'table'::text AS object_kind,
        table_record.relname::text AS object_identity,
        jsonb_build_object(
            'accessMethod', COALESCE(access_method.amname, ''),
            'acl', ARRAY(
                SELECT jsonb_build_object(
                    'grantable', privilege.is_grantable,
                    'grantee', CASE
                        WHEN privilege.grantee = 0 THEN '<public>'
                        WHEN privilege.grantee = table_record.relowner THEN '<owner>'
                        ELSE pg_get_userbyid(privilege.grantee)
                    END,
                    'grantor', CASE
                        WHEN privilege.grantor = table_record.relowner THEN '<owner>'
                        ELSE pg_get_userbyid(privilege.grantor)
                    END,
                    'privilege', privilege.privilege_type
                )::text
                FROM aclexplode(
                    COALESCE(
                        table_record.relacl,
                        acldefault('r', table_record.relowner)
                    )
                ) AS privilege
                ORDER BY 1
            ),
            'forceRowSecurity', table_record.relforcerowsecurity,
            'isPartition', table_record.relispartition,
            'kind', table_record.relkind,
            'ownerIsCurrentUser',
                pg_get_userbyid(table_record.relowner) = current_user,
            'partitionBound', COALESCE(
                pg_get_expr(
                    table_record.relpartbound,
                    table_record.oid,
                    TRUE
                ),
                ''
            ),
            'partitionKey', COALESCE(pg_get_partkeydef(table_record.oid), ''),
            'persistence', table_record.relpersistence,
            'replicaIdentity', table_record.relreplident,
            'rowSecurity', table_record.relrowsecurity
        )::text AS object_definition
    FROM owned_relations
    JOIN pg_class AS table_record
      ON table_record.oid = owned_relations.relation_oid
     AND table_record.relkind IN ('r', 'p')
    LEFT JOIN pg_am AS access_method
      ON access_method.oid = table_record.relam
),
sequence_shapes AS (
    SELECT
        'sequence'::text AS object_kind,
        sequence_record.relname::text AS object_identity,
        jsonb_build_object(
            'acl', ARRAY(
                SELECT jsonb_build_object(
                    'grantable', privilege.is_grantable,
                    'grantee', CASE
                        WHEN privilege.grantee = 0 THEN '<public>'
                        WHEN privilege.grantee = sequence_record.relowner THEN '<owner>'
                        ELSE pg_get_userbyid(privilege.grantee)
                    END,
                    'grantor', CASE
                        WHEN privilege.grantor = sequence_record.relowner THEN '<owner>'
                        ELSE pg_get_userbyid(privilege.grantor)
                    END,
                    'privilege', privilege.privilege_type
                )::text
                FROM aclexplode(
                    COALESCE(
                        sequence_record.relacl,
                        acldefault('S', sequence_record.relowner)
                    )
                ) AS privilege
                ORDER BY 1
            ),
            'cache', sequence_state.seqcache,
            'cycle', sequence_state.seqcycle,
            'dependencyType', ownership_dependency.deptype,
            'increment', sequence_state.seqincrement,
            'maximum', sequence_state.seqmax,
            'minimum', sequence_state.seqmin,
            'ownerColumn', owner_column.attname,
            'ownerIsCurrentUser',
                pg_get_userbyid(sequence_record.relowner) = current_user,
            'ownerTable', owner_table.relname,
            'persistence', sequence_record.relpersistence,
            'start', sequence_state.seqstart,
            'type', sequence_type.typname,
            'typeNamespace', CASE
                WHEN sequence_type_namespace.nspname = current_schema()
                THEN '<current>'
                ELSE sequence_type_namespace.nspname
            END
        )::text AS object_definition
    FROM owned_relations
    JOIN pg_class AS owner_table
      ON owner_table.oid = owned_relations.relation_oid
     AND owner_table.relkind IN ('r', 'p')
    JOIN pg_depend AS ownership_dependency
      ON ownership_dependency.refclassid = 'pg_class'::regclass
     AND ownership_dependency.refobjid = owner_table.oid
     AND ownership_dependency.refobjsubid > 0
     AND ownership_dependency.classid = 'pg_class'::regclass
     AND ownership_dependency.objsubid = 0
     AND ownership_dependency.deptype IN ('a', 'i')
    JOIN pg_class AS sequence_record
      ON sequence_record.oid = ownership_dependency.objid
     AND sequence_record.relkind = 'S'
     AND sequence_record.relnamespace = owner_table.relnamespace
    JOIN pg_sequence AS sequence_state
      ON sequence_state.seqrelid = sequence_record.oid
    JOIN pg_type AS sequence_type
      ON sequence_type.oid = sequence_state.seqtypid
    JOIN pg_namespace AS sequence_type_namespace
      ON sequence_type_namespace.oid = sequence_type.typnamespace
    JOIN pg_attribute AS owner_column
      ON owner_column.attrelid = owner_table.oid
     AND owner_column.attnum = ownership_dependency.refobjsubid
),
column_shapes AS (
    SELECT
        'column'::text AS object_kind,
        table_record.relname || '.' || attribute.attname AS object_identity,
        jsonb_build_object(
            'acl', ARRAY(
                SELECT jsonb_build_object(
                    'grantable', privilege.is_grantable,
                    'grantee', CASE
                        WHEN privilege.grantee = 0 THEN '<public>'
                        WHEN privilege.grantee = table_record.relowner THEN '<owner>'
                        ELSE pg_get_userbyid(privilege.grantee)
                    END,
                    'grantor', CASE
                        WHEN privilege.grantor = table_record.relowner THEN '<owner>'
                        ELSE pg_get_userbyid(privilege.grantor)
                    END,
                    'privilege', privilege.privilege_type
                )::text
                FROM aclexplode(attribute.attacl) AS privilege
                ORDER BY 1
            ),
            'collation', COALESCE(collation_record.collname, ''),
            'collationNamespace', COALESCE(
                CASE
                    WHEN collation_namespace.nspname = current_schema()
                    THEN '<current>'
                    ELSE collation_namespace.nspname
                END,
                ''
            ),
            'default', COALESCE(
                pg_get_expr(default_record.adbin, default_record.adrelid, TRUE),
                ''
            ),
            'expressionDependencies', ARRAY(
                SELECT DISTINCT dependency_identity
                FROM catalog_dependencies
                WHERE object_class = 'pg_attrdef'::regclass
                  AND object_id = default_record.oid
                  AND dependency_identity IS NOT NULL
                ORDER BY dependency_identity
            ),
            'generated', attribute.attgenerated,
            'identity', attribute.attidentity,
            'notNull', attribute.attnotnull,
            'type', format_type(attribute.atttypid, attribute.atttypmod),
            'typeBase', COALESCE(
                CASE
                    WHEN base_type_namespace.nspname = current_schema()
                    THEN '<current>'
                    ELSE base_type_namespace.nspname
                END || '.' || base_type.typname,
                ''
            ),
            'typeKind', type_record.typtype,
            'typeName', type_record.typname,
            'typeNamespace', CASE
                WHEN type_namespace.nspname = current_schema() THEN '<current>'
                ELSE type_namespace.nspname
            END
        )::text AS object_definition
    FROM owned_relations
    JOIN pg_class AS table_record
      ON table_record.oid = owned_relations.relation_oid
     AND table_record.relkind IN ('r', 'p')
    JOIN pg_attribute AS attribute
      ON attribute.attrelid = table_record.oid
     AND attribute.attnum > 0
     AND NOT attribute.attisdropped
    LEFT JOIN pg_attrdef AS default_record
      ON default_record.adrelid = table_record.oid
     AND default_record.adnum = attribute.attnum
    LEFT JOIN pg_collation AS collation_record
      ON collation_record.oid = attribute.attcollation
    LEFT JOIN pg_namespace AS collation_namespace
      ON collation_namespace.oid = collation_record.collnamespace
    JOIN pg_type AS type_record
      ON type_record.oid = attribute.atttypid
    JOIN pg_namespace AS type_namespace
      ON type_namespace.oid = type_record.typnamespace
    LEFT JOIN pg_type AS base_type
      ON base_type.oid = type_record.typbasetype
    LEFT JOIN pg_namespace AS base_type_namespace
      ON base_type_namespace.oid = base_type.typnamespace
),
constraint_shapes AS (
    SELECT
        'constraint'::text AS object_kind,
        table_record.relname || '.' || constraint_record.conname AS object_identity,
        jsonb_build_object(
            'check', COALESCE(
                pg_get_expr(
                    constraint_record.conbin,
                    constraint_record.conrelid,
                    TRUE
                ),
                ''
            ),
            'definition', pg_get_constraintdef(constraint_record.oid, TRUE),
            'deferrable', constraint_record.condeferrable,
            'deleteAction', constraint_record.confdeltype,
            'deleteSetColumns', ARRAY(
                SELECT attribute.attname
                FROM unnest(constraint_record.confdelsetcols)
                    WITH ORDINALITY AS delete_column(attnum, position)
                JOIN pg_attribute AS attribute
                  ON attribute.attrelid = constraint_record.conrelid
                 AND attribute.attnum = delete_column.attnum
                ORDER BY delete_column.position
            ),
            'deferred', constraint_record.condeferred,
            'dependencies', ARRAY(
                SELECT DISTINCT dependency_identity
                FROM catalog_dependencies
                WHERE object_class = 'pg_constraint'::regclass
                  AND object_id = constraint_record.oid
                  AND dependency_identity IS NOT NULL
                ORDER BY dependency_identity
            ),
            'exclusionOperators', ARRAY(
                SELECT dependency_identity
                FROM unnest(constraint_record.conexclop)
                    WITH ORDINALITY AS exclusion_operator(operator_oid, position)
                JOIN catalog_dependencies
                  ON object_class = 'pg_constraint'::regclass
                 AND object_id = constraint_record.oid
                 AND reference_class = 'pg_operator'::regclass
                 AND reference_id = exclusion_operator.operator_oid
                ORDER BY exclusion_operator.position
            ),
            'inheritanceCount', constraint_record.coninhcount,
            'isLocal', constraint_record.conislocal,
            'keys', ARRAY(
                SELECT attribute.attname
                FROM unnest(constraint_record.conkey) WITH ORDINALITY AS key(attnum, position)
                JOIN pg_attribute AS attribute
                  ON attribute.attrelid = constraint_record.conrelid
                 AND attribute.attnum = key.attnum
                ORDER BY key.position
            ),
            'matchType', constraint_record.confmatchtype,
            'noInherit', constraint_record.connoinherit,
            'parentConstraint', constraint_record.conparentid <> 0,
            'referenceKeys', ARRAY(
                SELECT attribute.attname
                FROM unnest(constraint_record.confkey) WITH ORDINALITY AS key(attnum, position)
                JOIN pg_attribute AS attribute
                  ON attribute.attrelid = constraint_record.confrelid
                 AND attribute.attnum = key.attnum
                ORDER BY key.position
            ),
            'referenceNamespace', CASE
                WHEN reference_namespace.nspname = current_schema() THEN '<current>'
                ELSE COALESCE(reference_namespace.nspname, '')
            END,
            'referenceTable', COALESCE(reference_table.relname, ''),
            'type', constraint_record.contype,
            'updateAction', constraint_record.confupdtype,
            'validated', constraint_record.convalidated
        )::text AS object_definition
    FROM owned_relations
    JOIN pg_class AS table_record
      ON table_record.oid = owned_relations.relation_oid
    JOIN pg_constraint AS constraint_record
      ON constraint_record.conrelid = table_record.oid
    LEFT JOIN pg_class AS reference_table
      ON reference_table.oid = constraint_record.confrelid
    LEFT JOIN pg_namespace AS reference_namespace
      ON reference_namespace.oid = reference_table.relnamespace
),
index_shapes AS (
    SELECT
        'index'::text AS object_kind,
        index_record.relname::text AS object_identity,
        jsonb_build_object(
            'accessMethod', access_method.amname,
            'clustered', index_state.indisclustered,
            'collations', ARRAY(
                SELECT COALESCE(
                    CASE
                        WHEN collation_namespace.nspname = current_schema()
                        THEN '<current>'
                        ELSE collation_namespace.nspname
                    END || '.' || collation_record.collname,
                    ''
                )
                FROM unnest(index_state.indcollation::oid[])
                    WITH ORDINALITY AS index_collation(collation_oid, position)
                LEFT JOIN pg_collation AS collation_record
                  ON collation_record.oid = index_collation.collation_oid
                LEFT JOIN pg_namespace AS collation_namespace
                  ON collation_namespace.oid = collation_record.collnamespace
                ORDER BY index_collation.position
            ),
            'columns', ARRAY(
                SELECT pg_get_indexdef(index_state.indexrelid, position, TRUE)
                FROM generate_series(1, index_state.indnatts) AS position
                ORDER BY position
            ),
            'dependencies', ARRAY(
                SELECT DISTINCT dependency_identity
                FROM catalog_dependencies
                WHERE object_class = 'pg_class'::regclass
                  AND object_id = index_state.indexrelid
                  AND dependency_identity IS NOT NULL
                ORDER BY dependency_identity
            ),
            'exclusion', index_state.indisexclusion,
            'immediate', index_state.indimmediate,
            'keyCount', index_state.indnkeyatts,
            'nullsNotDistinct', index_state.indnullsnotdistinct,
            'opclasses', ARRAY(
                SELECT CASE
                    WHEN opclass_namespace.nspname = current_schema()
                    THEN '<current>'
                    ELSE opclass_namespace.nspname
                END || '.' || opclass_record.opcname || '@'
                    || opclass_access_method.amname
                FROM unnest(index_state.indclass::oid[])
                    WITH ORDINALITY AS index_opclass(opclass_oid, position)
                JOIN pg_opclass AS opclass_record
                  ON opclass_record.oid = index_opclass.opclass_oid
                JOIN pg_namespace AS opclass_namespace
                  ON opclass_namespace.oid = opclass_record.opcnamespace
                JOIN pg_am AS opclass_access_method
                  ON opclass_access_method.oid = opclass_record.opcmethod
                ORDER BY index_opclass.position
            ),
            'options', index_state.indoption::smallint[],
            'predicate', COALESCE(
                pg_get_expr(index_state.indpred, index_state.indrelid, TRUE),
                ''
            ),
            'primary', index_state.indisprimary,
            'ready', index_state.indisready,
            'replicaIdentity', index_state.indisreplident,
            'table', table_record.relname,
            'unique', index_state.indisunique,
            'valid', index_state.indisvalid
        )::text AS object_definition
    FROM owned_relations
    JOIN pg_class AS table_record
      ON table_record.oid = owned_relations.relation_oid
     AND table_record.relkind IN ('r', 'p')
    JOIN pg_index AS index_state ON index_state.indrelid = table_record.oid
    JOIN pg_class AS index_record
      ON index_record.oid = index_state.indexrelid
     AND index_record.relkind = 'i'
    JOIN pg_am AS access_method ON access_method.oid = index_record.relam
),
trigger_shapes AS (
    SELECT
        'trigger'::text AS object_kind,
        table_record.relname || '.' || trigger_record.tgname AS object_identity,
        jsonb_build_object(
            'enabled', trigger_record.tgenabled,
            'function', function_record.proname,
            'functionArguments', pg_get_function_identity_arguments(
                function_record.oid
            ),
            'functionBody', function_record.prosrc,
            'functionConfiguration', ARRAY(
                SELECT replace(setting, current_schema(), '<current>')
                FROM unnest(
                    COALESCE(function_record.proconfig, ARRAY[]::text[])
                ) AS setting
                ORDER BY setting
            ),
            'functionLanguage', language_record.lanname,
            'functionLeakproof', function_record.proleakproof,
            'functionNamespace', CASE
                WHEN function_namespace.nspname = current_schema() THEN '<current>'
                ELSE function_namespace.nspname
            END,
            'functionParallel', function_record.proparallel,
            'functionResult', pg_get_function_result(function_record.oid),
            'functionSecurityDefiner', function_record.prosecdef,
            'functionStrict', function_record.proisstrict,
            'functionVolatility', function_record.provolatile,
            'functionOwnerIsCurrentUser',
                pg_get_userbyid(function_record.proowner) = current_user,
            'definition', pg_get_triggerdef(trigger_record.oid, TRUE),
            'argumentsHex', encode(trigger_record.tgargs, 'hex'),
            'columns', ARRAY(
                SELECT attribute.attname
                FROM unnest(trigger_record.tgattr::smallint[])
                    WITH ORDINALITY AS trigger_column(attnum, position)
                JOIN pg_attribute AS attribute
                  ON attribute.attrelid = trigger_record.tgrelid
                 AND attribute.attnum = trigger_column.attnum
                ORDER BY trigger_column.position
            ),
            'constraintDeferrable', trigger_record.tgdeferrable,
            'constraintInitiallyDeferred', trigger_record.tginitdeferred,
            'newTransitionTable', COALESCE(trigger_record.tgnewtable, ''),
            'oldTransitionTable', COALESCE(trigger_record.tgoldtable, ''),
            'type', trigger_record.tgtype,
            'when', COALESCE(
                pg_get_expr(trigger_record.tgqual, trigger_record.tgrelid, TRUE),
                ''
            )
        )::text AS object_definition
    FROM owned_relations
    JOIN pg_class AS table_record
      ON table_record.oid = owned_relations.relation_oid
     AND table_record.relkind IN ('r', 'p')
    JOIN pg_trigger AS trigger_record
      ON trigger_record.tgrelid = table_record.oid
     AND NOT trigger_record.tgisinternal
    JOIN pg_proc AS function_record ON function_record.oid = trigger_record.tgfoid
    JOIN pg_namespace AS function_namespace
      ON function_namespace.oid = function_record.pronamespace
    JOIN pg_language AS language_record
      ON language_record.oid = function_record.prolang
),
internal_trigger_records AS (
    SELECT
        table_record.relname::text AS table_name,
        jsonb_build_object(
            'argumentsHex', encode(trigger_record.tgargs, 'hex'),
            'columns', ARRAY(
                SELECT attribute.attname
                FROM unnest(trigger_record.tgattr::smallint[])
                    WITH ORDINALITY AS trigger_column(attnum, position)
                JOIN pg_attribute AS attribute
                  ON attribute.attrelid = trigger_record.tgrelid
                 AND attribute.attnum = trigger_column.attnum
                ORDER BY trigger_column.position
            ),
            'constraint', COALESCE(constraint_record.conname, ''),
            'constraintDeferrable', trigger_record.tgdeferrable,
            'constraintInitiallyDeferred', trigger_record.tginitdeferred,
            'enabled', trigger_record.tgenabled,
            'function', function_record.proname,
            'functionNamespace', CASE
                WHEN function_namespace.nspname = current_schema() THEN '<current>'
                ELSE function_namespace.nspname
            END,
            'newTransitionTable', COALESCE(trigger_record.tgnewtable, ''),
            'oldTransitionTable', COALESCE(trigger_record.tgoldtable, ''),
            'type', trigger_record.tgtype,
            'when', COALESCE(
                pg_get_expr(trigger_record.tgqual, trigger_record.tgrelid, TRUE),
                ''
            )
        ) AS trigger_definition,
        COALESCE(constraint_record.conname, '') AS constraint_name,
        function_record.proname AS function_name,
        trigger_record.tgtype AS trigger_type
    FROM owned_relations
    JOIN pg_class AS table_record
      ON table_record.oid = owned_relations.relation_oid
     AND table_record.relkind IN ('r', 'p')
    JOIN pg_trigger AS trigger_record
      ON trigger_record.tgrelid = table_record.oid
     AND trigger_record.tgisinternal
    LEFT JOIN pg_constraint AS constraint_record
      ON constraint_record.oid = trigger_record.tgconstraint
    JOIN pg_proc AS function_record
      ON function_record.oid = trigger_record.tgfoid
    JOIN pg_namespace AS function_namespace
      ON function_namespace.oid = function_record.pronamespace
),
internal_trigger_shapes AS (
    SELECT
        'internal-trigger-set'::text AS object_kind,
        table_name AS object_identity,
        jsonb_agg(
            trigger_definition
            ORDER BY constraint_name, function_name, trigger_type,
                     trigger_definition::text
        )::text AS object_definition
    FROM internal_trigger_records
    GROUP BY table_name
),
function_shapes AS (
    SELECT
        'function'::text AS object_kind,
        function_record.proname || '('
            || pg_get_function_identity_arguments(function_record.oid) || ')'
            AS object_identity,
        jsonb_build_object(
            'acl', ARRAY(
                SELECT jsonb_build_object(
                    'grantable', privilege.is_grantable,
                    'grantee', CASE
                        WHEN privilege.grantee = 0 THEN '<public>'
                        WHEN privilege.grantee = function_record.proowner THEN '<owner>'
                        ELSE pg_get_userbyid(privilege.grantee)
                    END,
                    'grantor', CASE
                        WHEN privilege.grantor = function_record.proowner THEN '<owner>'
                        ELSE pg_get_userbyid(privilege.grantor)
                    END,
                    'privilege', privilege.privilege_type
                )::text
                FROM aclexplode(
                    COALESCE(
                        function_record.proacl,
                        acldefault('f', function_record.proowner)
                    )
                ) AS privilege
                ORDER BY 1
            ),
            'arguments', pg_get_function_arguments(function_record.oid),
            'argumentModes', COALESCE(
                function_record.proargmodes::text[],
                ARRAY[]::text[]
            ),
            'argumentTypes', ARRAY(
                SELECT CASE
                    WHEN argument_type_namespace.nspname = current_schema()
                    THEN '<current>'
                    ELSE argument_type_namespace.nspname
                END || '.' || argument_type.typname
                FROM unnest(
                    COALESCE(
                        function_record.proallargtypes,
                        function_record.proargtypes::oid[]
                    )
                ) WITH ORDINALITY AS argument(type_oid, position)
                JOIN pg_type AS argument_type
                  ON argument_type.oid = argument.type_oid
                JOIN pg_namespace AS argument_type_namespace
                  ON argument_type_namespace.oid = argument_type.typnamespace
                ORDER BY argument.position
            ),
            'body', function_record.prosrc,
            'configuration', ARRAY(
                SELECT replace(setting, current_schema(), '<current>')
                FROM unnest(
                    COALESCE(function_record.proconfig, ARRAY[]::text[])
                ) AS setting
                ORDER BY setting
            ),
            'kind', function_record.prokind,
            'language', language_record.lanname,
            'leakproof', function_record.proleakproof,
            'namespace', '<current>',
            'ownerIsCurrentUser',
                pg_get_userbyid(function_record.proowner) = current_user,
            'parallel', function_record.proparallel,
            'result', pg_get_function_result(function_record.oid),
            'resultType', CASE
                WHEN result_type_namespace.nspname = current_schema()
                THEN '<current>'
                ELSE result_type_namespace.nspname
            END || '.' || result_type.typname,
            'resultTypeKind', result_type.typtype,
            'returnsSet', function_record.proretset,
            'securityDefiner', function_record.prosecdef,
            'strict', function_record.proisstrict,
            'support', COALESCE(
                CASE
                    WHEN support_namespace.nspname = current_schema()
                    THEN '<current>'
                    ELSE support_namespace.nspname
                END || '.' || support_function.proname || '('
                    || pg_get_function_identity_arguments(support_function.oid)
                    || ')',
                ''
            ),
            'volatility', function_record.provolatile
        )::text AS object_definition
    FROM pg_proc AS function_record
    JOIN pg_namespace AS namespace_record
      ON namespace_record.oid = function_record.pronamespace
     AND namespace_record.nspname = current_schema()
    JOIN pg_language AS language_record
      ON language_record.oid = function_record.prolang
    JOIN pg_type AS result_type
      ON result_type.oid = function_record.prorettype
    JOIN pg_namespace AS result_type_namespace
      ON result_type_namespace.oid = result_type.typnamespace
    LEFT JOIN pg_proc AS support_function
      ON support_function.oid = function_record.prosupport
    LEFT JOIN pg_namespace AS support_namespace
      ON support_namespace.oid = support_function.pronamespace
),
policy_shapes AS (
    SELECT
        'policy'::text AS object_kind,
        table_record.relname || '.' || policy_record.polname AS object_identity,
        jsonb_build_object(
            'command', policy_record.polcmd,
            'dependencies', ARRAY(
                SELECT DISTINCT dependency_identity
                FROM catalog_dependencies
                WHERE object_class = 'pg_policy'::regclass
                  AND object_id = policy_record.oid
                  AND dependency_identity IS NOT NULL
                ORDER BY dependency_identity
            ),
            'permissive', policy_record.polpermissive,
            'roles', ARRAY(
                SELECT CASE
                    WHEN role_oid = 0 THEN '<public>'
                    WHEN role_oid = table_record.relowner THEN '<owner>'
                    ELSE pg_get_userbyid(role_oid)
                END
                FROM unnest(policy_record.polroles) AS role_oid
                ORDER BY 1
            ),
            'using', COALESCE(
                pg_get_expr(policy_record.polqual, policy_record.polrelid, TRUE),
                ''
            ),
            'withCheck', COALESCE(
                pg_get_expr(
                    policy_record.polwithcheck,
                    policy_record.polrelid,
                    TRUE
                ),
                ''
            )
        )::text AS object_definition
    FROM owned_relations
    JOIN pg_class AS table_record
      ON table_record.oid = owned_relations.relation_oid
     AND table_record.relkind IN ('r', 'p')
    JOIN pg_policy AS policy_record
      ON policy_record.polrelid = table_record.oid
),
rule_shapes AS (
    SELECT
        'rule'::text AS object_kind,
        table_record.relname || '.' || rule_record.rulename AS object_identity,
        jsonb_build_object(
            'definition', pg_get_ruledef(rule_record.oid, TRUE),
            'enabled', rule_record.ev_enabled,
            'event', rule_record.ev_type,
            'instead', rule_record.is_instead
        )::text AS object_definition
    FROM owned_relations
    JOIN pg_class AS table_record
      ON table_record.oid = owned_relations.relation_oid
     AND table_record.relkind IN ('r', 'p')
    JOIN pg_rewrite AS rule_record
      ON rule_record.ev_class = table_record.oid
     AND rule_record.rulename <> '_RETURN'
),
inheritance_shapes AS (
    SELECT
        'inheritance'::text AS object_kind,
        CASE
            WHEN child_namespace.nspname = current_schema() THEN '<current>'
            ELSE child_namespace.nspname
        END || '.' || child_table.relname || '->' || CASE
            WHEN parent_namespace.nspname = current_schema() THEN '<current>'
            ELSE parent_namespace.nspname
        END || '.' || parent_table.relname AS object_identity,
        jsonb_build_object(
            'ownedRole', CASE
                WHEN child_table.oid IN (
                    SELECT relation_oid FROM owned_relations
                ) AND parent_table.oid IN (
                    SELECT relation_oid FROM owned_relations
                ) THEN 'both'
                WHEN child_table.oid IN (
                    SELECT relation_oid FROM owned_relations
                ) THEN 'child'
                ELSE 'parent'
            END,
            'sequence', inheritance.inhseqno
        )::text AS object_definition
    FROM pg_inherits AS inheritance
    JOIN pg_class AS child_table
      ON child_table.oid = inheritance.inhrelid
    JOIN pg_namespace AS child_namespace
      ON child_namespace.oid = child_table.relnamespace
    JOIN pg_class AS parent_table
      ON parent_table.oid = inheritance.inhparent
    JOIN pg_namespace AS parent_namespace
      ON parent_namespace.oid = parent_table.relnamespace
    WHERE child_table.oid IN (
        SELECT relation_oid FROM owned_relations
    ) OR parent_table.oid IN (
        SELECT relation_oid FROM owned_relations
    )
)
SELECT object_kind, object_identity, object_definition
FROM (
    SELECT * FROM schema_shapes
    UNION ALL SELECT * FROM table_shapes
    UNION ALL SELECT * FROM sequence_shapes
    UNION ALL SELECT * FROM column_shapes
    UNION ALL SELECT * FROM constraint_shapes
    UNION ALL SELECT * FROM index_shapes
    UNION ALL SELECT * FROM trigger_shapes
    UNION ALL SELECT * FROM internal_trigger_shapes
    UNION ALL SELECT * FROM function_shapes
    UNION ALL SELECT * FROM policy_shapes
    UNION ALL SELECT * FROM rule_shapes
    UNION ALL SELECT * FROM inheritance_shapes
) AS structures
ORDER BY object_kind, object_identity
""".strip()

CURRENT_SCHEMA_STRUCTURE_DIGEST: Final = (
    "cb564e2d95f3b58fe9861227bde05e16d56e9852f89a2abdcc825e156b08cbc9"
)
CURRENT_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS: Final = (CURRENT_SCHEMA_STRUCTURE_DIGEST,)

_CURRENT_SCHEMA_NAME_QUERY: Final = """
SELECT
    namespace_record.nspname AS schema_name,
    pg_get_userbyid(namespace_record.nspowner) AS schema_owner
FROM pg_namespace AS namespace_record
WHERE namespace_record.oid = current_schema()::regnamespace
""".strip()
_DEFAULT_PUBLIC_SCHEMA_DEFINITION: Final = (
    '{"acl": ["{\\"grantee\\": \\"<owner>\\", \\"grantor\\": \\"<owner>\\", '
    '\\"grantable\\": false, \\"privilege\\": \\"CREATE\\"}", '
    '"{\\"grantee\\": \\"<owner>\\", \\"grantor\\": \\"<owner>\\", '
    '\\"grantable\\": false, \\"privilege\\": \\"USAGE\\"}", '
    '"{\\"grantee\\": \\"<public>\\", \\"grantor\\": \\"<owner>\\", '
    '\\"grantable\\": false, \\"privilege\\": \\"USAGE\\"}"], '
    '"ownerIsCurrentUser": false}'
)
_CANONICAL_OWNED_SCHEMA_DEFINITION: Final = (
    '{"acl": ["{\\"grantee\\": \\"<owner>\\", \\"grantor\\": \\"<owner>\\", '
    '\\"grantable\\": false, \\"privilege\\": \\"CREATE\\"}", '
    '"{\\"grantee\\": \\"<owner>\\", \\"grantor\\": \\"<owner>\\", '
    '\\"grantable\\": false, \\"privilege\\": \\"USAGE\\"}"], '
    '"ownerIsCurrentUser": true}'
)


def _schema_structure_digest(records: tuple[Mapping[str, object], ...]) -> str:
    canonical = json.dumps(
        [
            {
                "definition": str(record["object_definition"]),
                "identity": str(record["object_identity"]),
                "kind": str(record["object_kind"]),
            }
            for record in records
        ],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


PREDECESSOR_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS: Final = (
    (
        "066.001",
        (
            "4389632042f990aaf6afec57b5bdfd44b2e2711b1c414a6c71591d147546b6c4",
            "84cc9f0af555517013af26ada3920aebb8cc10e0d05fed75d424f960c810aa5f",
        ),
    ),
    (
        "067.001",
        (
            "8d46db8b3a788a77185b21d149819f6394e14d96cdd6a2e393c105e2adb2b587",
            "14b1a1369359114c439143a3bdd75f8a51f6b153361e2360759fea96016b8a42",
        ),
    ),
    (
        "074.001",
        (
            "4f312524a3b22eea04a5ef0566200b03aa74a57c3e4a891612dd3f1d8fc978b5",
            "a02a688814c62c1a8cb864161853bd2b84202fa691626e19d3c588eb7c705a12",
        ),
    ),
    (
        "074.002",
        (
            "6d5b8c61ae3a5e55c26b3abb3c81344c11fbe7c93401cdabcb700eb6dccdf67d",
            "cb09fe20fd9e1a1e1fc6f92f828d48ed80b2bc2e136ed7df34c7a0835edd5dde",
        ),
    ),
    (
        "074.003",
        (
            "5676e8159ed581433daf24a7d6cad2d3d4ab0cdf94875d1dd870aae382a26bfe",
            "cf88e094b28ee09a228b9c2d5cf36c95200ae002d5045b6061086ab775fdb1b4",
        ),
    ),
    (
        "074.004",
        ("2e95879e293dbac3018a5c3fea92662b2939390bb12195f5a9f8d1eca03d6ec3",),
    ),
    (
        "075.001",
        ("0b623484495b64cb2557473f6e9d9c1d9f41a6798090641f2ffe65f8c7076b15",),
    ),
    (
        "079.001",
        ("ea985cd52e622f9febaed5783b312ca7177cc088ad9804d71891647087d99eeb",),
    ),
    (
        "088.001",
        ("674a7463c1075a43be94155cb5eb44734a1adea5a1cffe2a3d8ac30899208975",),
    ),
    (
        "088.002",
        ("e04e5d0b8984e89208cefb0321636dde3b85a25679849ba00fe95f99577d0a71",),
    ),
    ("088.003", ("aedb36a4c2ee25925158b5a3f04303b46c71b391a0fe6a4d25dbb3fd395c127b",)),
    ("088.004", ("5a46bdb17bcead3af6d1bb6a95c97aac04cf35a716ffd3f63b239836f0c30b72",)),
    ("088.005", ("b278966bf3a42458c72c1c859014c523a8da00d8acd0ee18a4e8ecb9f783835a",)),
    ("088.006", ("aa8dd06daf08fb11b92dc528c57a2e68072698e437a5b2f83933fdaecebe9f8b",)),
    ("088.007", ("eeb9ed13a85e58ce84be7f9d31324e815946b72a8168c67068257c0791e332db",)),
    ("088.008", ("c99faec61a4a8b4b362550cb12074aefb7e610e3775fa276daf9d2df1d7cbbe1",)),
    ("089.001", ("4123d3bae2d73e369c65ca715ccaf47bdf3560e5ae09a3dc967fe26abd2517f7",)),
    ("089.002", ("dc9d7ec5b8c5be483c16f89ce7d723410c402fb6f5dc8962663c53a41e7609b5",)),
    ("089.003", ("3441e750601f9b78fa7057d36e0b0c51bed6a912450baca6a30db7aa33fcae22",)),
    ("089.004", ("4be9c1e2ab9fe95472bd1a1a986bcfc578984071cd4c7de41397b39af6851c22",)),
    ("089.005", ("2aae4fa6281bda3ca690ce9eb174a5e43b454f0862b0544129bd537eb67bb798",)),
    ("089.006", ("88896cd0498562ecd0b4d610c4453f7ed01f527e0845cacf7300d88374578e48",)),
    ("089.007", ("939f224d8a11622964a53b37a63dde6ec37095636145b018486883afe70e9dff",)),
    ("089.008", ("880dedb270bb3b151685136edbae32f9b4ee42f35d1286af7a395d5532f02d70",)),
    ("089.009", ("50c157e6451628840bb6b2a7ae245dfbd281a9ef1e3a5248831de4ce776ad3a5",)),
    ("089.010", ("1e1fb4ca7f6ec63e790a65c3ebbaf028702f0ddefb084152fba6cb19cb161c68",)),
    ("089.011", ("f986f26b756d0611d52007f73888672b191b5426a8404ec5bd7da3152639599d",)),
    ("089.012", ("e506006358926677d9dba3261e0a17b4c56815132f747953048f6fcca1e1e38d",)),
    ("089.013", ("8b8717e86457edcff6c17661f3e74c61f227022ecfb399bb8d3b3484ad411233",)),
    ("089.014", ("5879c71ec6a7ebb414126087034e00663503174b10701f538b32cb22c28ef8e1",)),
    ("089.015", ("5a2943f2bc607fa8a9e88d4123abc866348cfe95164f22b87a44fca82c1986af",)),
    ("089.016", ("075f10522138154d770bc77898a9b43e6fddbdfca3ff0549c0ef77b983b7ea7e",)),
    ("089.017", ("86af1f46f4909106c0ab736203acfdbca86f3ed105559a8972db4899faa8c03f",)),
    ("089.018", ("ea9ef03c7818484211071ae4182b12ba9b4eb0f6e3b0863aa6792e9dcc1ec625",)),
    ("089.019", ("78774d997f0f9403dd19b1649ef48e230de62bc6c4954416a647e259d11a0203",)),
    ("089.020", ("07bc6c360942f7890b2c77fbf6a36a4e26f4bf74ee45005f1bee003371d220ef",)),
    ("089.021", ("6d5601534ee90bbd5d45916b248cffe1da44f64768a09ce6efd084cb5d199365",)),
)


def _verify_predecessor_plane_schema(
    transaction: Transaction,
    revision: str | None,
) -> None:
    accepted = dict(PREDECESSOR_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS).get(revision)
    if accepted is None:
        raise SchemaRevisionError(
            "migration source has no qualified predecessor schema evidence",
            metadata={"revision": revision or "<empty>"},
        )
    records = tuple(transaction.fetch_all(CURRENT_SCHEMA_STRUCTURE_QUERY))
    observed = _schema_structure_digest(records)
    if observed in accepted:
        return

    schema_record = transaction.fetch_one(_CURRENT_SCHEMA_NAME_QUERY)
    schema_name = None if schema_record is None else schema_record.get("schema_name")
    schema_owner = None if schema_record is None else schema_record.get("schema_owner")
    schema_shapes = tuple(
        record
        for record in records
        if record.get("object_kind") == "schema" and record.get("object_identity") == "<current>"
    )
    if (
        schema_name == "public"
        and schema_owner == "pg_database_owner"
        and len(schema_shapes) == 1
        and schema_shapes[0].get("object_definition") == _DEFAULT_PUBLIC_SCHEMA_DEFINITION
    ):
        normalized = tuple(
            {
                "object_definition": _CANONICAL_OWNED_SCHEMA_DEFINITION,
                "object_identity": record["object_identity"],
                "object_kind": record["object_kind"],
            }
            if record is schema_shapes[0]
            else record
            for record in records
        )
        if _schema_structure_digest(normalized) in accepted:
            return

    if observed not in accepted:
        raise SchemaRevisionError(
            "predecessor schema canonical structure does not match revision evidence",
            metadata={
                "expected": ",".join(accepted),
                "observed": observed,
                "revision": revision or "<empty>",
            },
        )


def _verify_current_plane_schema(transaction: Transaction) -> None:
    for statement in CURRENT_SCHEMA_VERIFICATION_STATEMENTS:
        transaction.execute(statement)
    observed_digest = _schema_structure_digest(
        transaction.fetch_all(CURRENT_SCHEMA_STRUCTURE_QUERY)
    )
    if observed_digest not in CURRENT_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS:
        raise SchemaRevisionError(
            "current schema canonical structure does not match revision evidence",
            metadata={
                "expected": ",".join(CURRENT_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS),
                "observed": observed_digest,
            },
        )


def _statements_checksum(statements: tuple[str, ...]) -> str:
    canonical = json.dumps(
        statements,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


CURRENT_SCHEMA_VERIFIER_CHECKSUM: Final = _statements_checksum(
    (
        *CURRENT_SCHEMA_VERIFICATION_STATEMENTS,
        CURRENT_SCHEMA_STRUCTURE_QUERY,
        *CURRENT_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS,
    )
)
PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM: Final = _statements_checksum(
    (
        CURRENT_SCHEMA_STRUCTURE_QUERY,
        _CURRENT_SCHEMA_NAME_QUERY,
        _DEFAULT_PUBLIC_SCHEMA_DEFINITION,
        _CANONICAL_OWNED_SCHEMA_DEFINITION,
        json.dumps(
            PREDECESSOR_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS,
            ensure_ascii=True,
            separators=(",", ":"),
        ),
    )
)


PLANE_SCHEMA_067_MIGRATION: Final = Migration(
    name="astralplane-067-transactional-recovery",
    source_revisions=("066.001",),
    target_revision="067.001",
    checksum=_statements_checksum(PLANE_SCHEMA_067_STATEMENTS),
    operation=_apply_plane_schema_067,
)
PLANE_SCHEMA_067_REGISTRY_DIGEST: Final = (
    "ae2285e6764cf084eeaf6099443d85fb9b27ae930fcb0684e4e0f458d17bb4f9"
)
if MigrationRegistry((PLANE_SCHEMA_067_MIGRATION,)).digest != PLANE_SCHEMA_067_REGISTRY_DIGEST:
    raise MigrationDefinitionError(
        "historical 067 migration registry no longer matches its pinned digest"
    )
PLANE_SCHEMA_074_MIGRATION: Final = Migration(
    name="astralplane-074-lets-authority",
    source_revisions=("067.001",),
    target_revision="074.001",
    checksum=_statements_checksum(PLANE_SCHEMA_074_STATEMENTS),
    operation=_apply_plane_schema_074,
)
PLANE_SCHEMA_074_001_REGISTRY_DIGEST: Final = (
    "02ee01830e51c97edbeb384eb05f25b5101efa6c0f564383bab5b7b90a7e80cf"
)
if (
    MigrationRegistry((PLANE_SCHEMA_067_MIGRATION, PLANE_SCHEMA_074_MIGRATION)).digest
    != PLANE_SCHEMA_074_001_REGISTRY_DIGEST
):
    raise MigrationDefinitionError(
        "historical 074.001 migration registry no longer matches its pinned digest"
    )
PLANE_SCHEMA_074_002_MIGRATION: Final = Migration(
    name="astralplane-074-quality-audit-ownership",
    source_revisions=("074.001",),
    target_revision="074.002",
    checksum=_statements_checksum(PLANE_SCHEMA_074_002_STATEMENTS),
    operation=_apply_plane_schema_074_002,
)
PLANE_SCHEMA_074_002_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "4ab0716557288b8721ea97b5ecea3b760d58b3539918b2e948341e90ff2d3fd1"
)
PLANE_SCHEMA_074_002_REGISTRY_DIGEST: Final = (
    "1bb948074ec378d2a74e2b74eff29e72a6f9a6be03d3ae24ec6439fcf70f1e02"
)
if (
    MigrationRegistry(
        (
            PLANE_SCHEMA_067_MIGRATION,
            PLANE_SCHEMA_074_MIGRATION,
            PLANE_SCHEMA_074_002_MIGRATION,
        ),
        current_schema_verifier=_verify_current_plane_schema,
        current_schema_verifier_checksum=(PLANE_SCHEMA_074_002_SCHEMA_VERIFIER_CHECKSUM),
    ).digest
    != PLANE_SCHEMA_074_002_REGISTRY_DIGEST
):
    raise MigrationDefinitionError(
        "historical 074.002 migration registry no longer matches its pinned digest"
    )
PLANE_SCHEMA_074_003_MIGRATION: Final = Migration(
    name="astralplane-074-current-runtime-contract",
    source_revisions=("074.002",),
    target_revision="074.003",
    checksum=_statements_checksum(PLANE_SCHEMA_074_003_STATEMENTS),
    operation=_apply_plane_schema_074_003,
)
PLANE_SCHEMA_074_003_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "36638e8a87aa5a516429fa4b4998ae3e72bd8d86d5b25cac4db5fb8ef2d2380e"
)
PLANE_SCHEMA_074_003_REGISTRY_DIGEST: Final = (
    "ae0a3152c11ce711cbc8fbf8c3447d38abfe32db9d9d0fd5149e7e2c1c623296"
)
if (
    MigrationRegistry(
        (
            PLANE_SCHEMA_067_MIGRATION,
            PLANE_SCHEMA_074_MIGRATION,
            PLANE_SCHEMA_074_002_MIGRATION,
            PLANE_SCHEMA_074_003_MIGRATION,
        ),
        current_schema_verifier=_verify_current_plane_schema,
        current_schema_verifier_checksum=(PLANE_SCHEMA_074_003_SCHEMA_VERIFIER_CHECKSUM),
    ).digest
    != PLANE_SCHEMA_074_003_REGISTRY_DIGEST
):
    raise MigrationDefinitionError(
        "historical 074.003 migration registry no longer matches its pinned digest"
    )
PLANE_SCHEMA_074_004_MIGRATION: Final = Migration(
    name="astralplane-074-pending-attachment-materialization",
    source_revisions=("074.003",),
    target_revision="074.004",
    checksum=_statements_checksum(PLANE_SCHEMA_074_004_STATEMENTS),
    operation=_apply_plane_schema_074_004,
)
PLANE_SCHEMA_074_004_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "bd5ff43f781e08fe127a6e28ae9bd9b57a796190360215ee485941bc56870e69"
)
PLANE_SCHEMA_074_004_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "e8eecf5c403b73f2d6f0678de6bbf0e9cfc219c10e89d59a961b8c6d63fa9c7e"
)
PLANE_SCHEMA_074_004_REGISTRY_DIGEST: Final = (
    "31495e9b916301e5d9d5011f256224e62e0a0822e25fdf3b9c339beb695eff50"
)
if (
    MigrationRegistry(
        (
            PLANE_SCHEMA_067_MIGRATION,
            PLANE_SCHEMA_074_MIGRATION,
            PLANE_SCHEMA_074_002_MIGRATION,
            PLANE_SCHEMA_074_003_MIGRATION,
            PLANE_SCHEMA_074_004_MIGRATION,
        ),
        current_schema_verifier=_verify_current_plane_schema,
        current_schema_verifier_checksum=(PLANE_SCHEMA_074_004_SCHEMA_VERIFIER_CHECKSUM),
        predecessor_schema_verifier=_verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=(
            PLANE_SCHEMA_074_004_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM
        ),
    ).digest
    != PLANE_SCHEMA_074_004_REGISTRY_DIGEST
):
    raise MigrationDefinitionError(
        "historical 074.004 migration registry no longer matches its pinned digest"
    )
PLANE_SCHEMA_075_MIGRATION: Final = Migration(
    name="astralplane-075-client-local-speech",
    source_revisions=("074.004",),
    target_revision="075.001",
    checksum=_statements_checksum(PLANE_SCHEMA_075_STATEMENTS),
    operation=_apply_plane_schema_075,
)
PLANE_SCHEMA_075_REGISTRY_DIGEST: Final = (
    "755faecd45a7d8ca9956f25a239bed476802b885efdce29a36dc3b66981f94df"
)
if (
    MigrationRegistry(
        (
            PLANE_SCHEMA_067_MIGRATION,
            PLANE_SCHEMA_074_MIGRATION,
            PLANE_SCHEMA_074_002_MIGRATION,
            PLANE_SCHEMA_074_003_MIGRATION,
            PLANE_SCHEMA_074_004_MIGRATION,
            PLANE_SCHEMA_075_MIGRATION,
        ),
        current_schema_verifier=_verify_current_plane_schema,
        current_schema_verifier_checksum="bc32928ec26f75eec92c632a536cb9853d3e6db6e3fc45c271ea69abde5510fe",
        predecessor_schema_verifier=_verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum="81c7e111ab56966bf4c4bf2b6f1be7e96bfd12a5f988ce86e6d5df1dc605d1c4",
    ).digest
    != PLANE_SCHEMA_075_REGISTRY_DIGEST
):
    raise MigrationDefinitionError(
        "historical 075.001 registry no longer matches its pinned digest"
    )
PLANE_SCHEMA_079_STATEMENTS: Final = ASSIGNMENT_SCHEMA_STATEMENTS
PLANE_SCHEMA_079_MIGRATION: Final = Migration(
    name="astralplane-079-persistent-assignments",
    source_revisions=("075.001",),
    target_revision="079.001",
    checksum=_statements_checksum(ASSIGNMENT_SCHEMA_STATEMENTS),
    operation=_apply_plane_schema_079,
)
PLANE_SCHEMA_079_REGISTRY_DIGEST: Final = (
    "2353261227ed72d030ab2426b1a7229c8a1302c669a241dc6b84e3e77e003cad"
)
if (
    MigrationRegistry(
        (
            PLANE_SCHEMA_067_MIGRATION,
            PLANE_SCHEMA_074_MIGRATION,
            PLANE_SCHEMA_074_002_MIGRATION,
            PLANE_SCHEMA_074_003_MIGRATION,
            PLANE_SCHEMA_074_004_MIGRATION,
            PLANE_SCHEMA_075_MIGRATION,
            PLANE_SCHEMA_079_MIGRATION,
        ),
        current_schema_verifier=_verify_current_plane_schema,
        current_schema_verifier_checksum="1987a3e7b27787ef5c4dcc4552e2713b1627b82aaf0760d8ccb881e5a4f30017",
        predecessor_schema_verifier=_verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum="7a881bf3c3753eee9ec320444f7f0d029da03bbf77ef74c9160921435ef54e25",
    ).digest
    != PLANE_SCHEMA_079_REGISTRY_DIGEST
):
    raise MigrationDefinitionError(
        "historical 079.001 registry no longer matches its pinned digest"
    )
PLANE_SCHEMA_088_MIGRATION: Final = Migration(
    name="astralplane-088-one-shot-operations",
    source_revisions=("079.001",),
    target_revision="088.001",
    checksum=_statements_checksum(OPERATION_SCHEMA_STATEMENTS),
    operation=_apply_plane_schema_088,
)
PLANE_SCHEMA_088_REGISTRY_DIGEST: Final = (
    "b6eaa819e9bd471350e48e431686c1ed6922e206608f1b673e0544c14014552d"
)
if (
    MigrationRegistry(
        (
            PLANE_SCHEMA_067_MIGRATION,
            PLANE_SCHEMA_074_MIGRATION,
            PLANE_SCHEMA_074_002_MIGRATION,
            PLANE_SCHEMA_074_003_MIGRATION,
            PLANE_SCHEMA_074_004_MIGRATION,
            PLANE_SCHEMA_075_MIGRATION,
            PLANE_SCHEMA_079_MIGRATION,
            PLANE_SCHEMA_088_MIGRATION,
        ),
        current_schema_verifier=_verify_current_plane_schema,
        current_schema_verifier_checksum="35bd630d2be86b48988d2fdbe16da54faea293aca68e80d6363db8fb41ded1de",
        predecessor_schema_verifier=_verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum="f87fa1fba779b03a64feec13821d5426c28159117e6c0aafb47756847706f885",
    ).digest
    != PLANE_SCHEMA_088_REGISTRY_DIGEST
):
    raise MigrationDefinitionError(
        "historical 088.001 registry no longer matches its pinned digest"
    )
PLANE_SCHEMA_088_002_MIGRATION: Final = Migration(
    name="astralplane-088-session-incarnation",
    source_revisions=("088.001",),
    target_revision="088.002",
    checksum=_statements_checksum(PLANE_SCHEMA_088_002_STATEMENTS),
    operation=_apply_plane_schema_088_002,
)
PLANE_SCHEMA_088_002_REGISTRY_DIGEST: Final = (
    "22d086ef60c5e73f124f3569268c4b48138871b1614ac538ab7f685a1b313afc"
)
if (
    MigrationRegistry(
        (
            PLANE_SCHEMA_067_MIGRATION,
            PLANE_SCHEMA_074_MIGRATION,
            PLANE_SCHEMA_074_002_MIGRATION,
            PLANE_SCHEMA_074_003_MIGRATION,
            PLANE_SCHEMA_074_004_MIGRATION,
            PLANE_SCHEMA_075_MIGRATION,
            PLANE_SCHEMA_079_MIGRATION,
            PLANE_SCHEMA_088_MIGRATION,
            PLANE_SCHEMA_088_002_MIGRATION,
        ),
        current_schema_verifier=_verify_current_plane_schema,
        current_schema_verifier_checksum="c9321dc01e0196b626c68c7be653f7c89329cd779f90cbd86776c9843730aae0",
        predecessor_schema_verifier=_verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum="ff709a4f975d23dcf10cf27ebd4d7e66d97f213b9f028409cb54cefa0d5fc291",
    ).digest
    != PLANE_SCHEMA_088_002_REGISTRY_DIGEST
):
    raise MigrationDefinitionError(
        "historical 088.002 registry no longer matches its pinned digest"
    )
PLANE_SCHEMA_088_003_MIGRATION: Final = Migration(
    name="astralplane-088-session-issuer",
    source_revisions=("088.002",),
    target_revision="088.003",
    checksum=_statements_checksum(PLANE_SCHEMA_088_003_STATEMENTS),
    operation=_apply_plane_schema_088_003,
)
PLANE_SCHEMA_088_003_REGISTRY_DIGEST: Final = (
    "a3d3ac43bee48b0ca6832cca1e4a347db0a3f838af908b8545edb11e7e94272a"
)
PLANE_SCHEMA_088_004_MIGRATION: Final = Migration(
    name="astralplane-088-declarative-agents",
    source_revisions=("088.003",),
    target_revision="088.004",
    checksum=_statements_checksum(DECLARATIVE_AGENT_SCHEMA_STATEMENTS),
    operation=_apply_plane_schema_088_004,
)
PLANE_SCHEMA_088_004_REGISTRY_DIGEST: Final = (
    "99e50e8f0aa6d4086bfe3e56701b951ebbb5bed9628631253f8dce2d935464e3"
)
PLANE_SCHEMA_088_005_MIGRATION: Final = Migration(
    name="astralplane-088-owner-guidance",
    source_revisions=("088.004",),
    target_revision="088.005",
    checksum=_statements_checksum(GUIDANCE_SCHEMA_STATEMENTS),
    operation=_apply_plane_schema_088_005,
)
PLANE_SCHEMA_088_005_REGISTRY_DIGEST: Final = (
    "8b1bd520cf0ecc249ef39d35930d71a591f49d5d04f254eabe1e3aefac729ed2"
)
PLANE_SCHEMA_088_006_MIGRATION: Final = Migration(
    name="astralplane-088-selected-input",
    source_revisions=("088.005",),
    target_revision="088.006",
    checksum=_statements_checksum(SELECTED_INPUT_SCHEMA_STATEMENTS),
    operation=_apply_plane_schema_088_006,
)
PLANE_SCHEMA_088_006_REGISTRY_DIGEST: Final = (
    "4f5783e676a12a4009959da2056c2c769418f8c101d67f533223a608158ed73a"
)
PLANE_SCHEMA_088_007_MIGRATION: Final = Migration(
    name="astralplane-088-scheduler-policy",
    source_revisions=("088.006",),
    target_revision="088.007",
    checksum=_statements_checksum(SCHEDULER_POLICY_SCHEMA_STATEMENTS),
    operation=_apply_plane_schema_088_007,
)
PLANE_SCHEMA_088_007_REGISTRY_DIGEST: Final = (
    "844d9f6629bc422013f84488b10ba6d2862f4805475c63b217639184146fca4d"
)
PLANE_SCHEMA_088_008_MIGRATION: Final = Migration(
    name="astralplane-088-framework-credentials",
    source_revisions=("088.007",),
    target_revision="088.008",
    checksum=_statements_checksum(FRAMEWORK_CREDENTIAL_SCHEMA_STATEMENTS),
    operation=_apply_plane_schema_088_008,
)
# Pinned historical value; recomputing would hide drift
PLANE_SCHEMA_088_008_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "caed57601561aec9313d260a6efd4c731e2d6c3499d17423b830a0ee6f82bfdf"
)
PLANE_SCHEMA_088_008_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "a46d356acf50c9e05ede126bb91c382422c8bc11844bc2e803c3fa3e491523f2"
)
PLANE_SCHEMA_088_008_REGISTRY_DIGEST: Final = (
    "4cddbddbc3eed66232f35bc24452451f92aa2b3e90968eb6fd6754f9a64358d8"
)
if (
    MigrationRegistry(
        (
            PLANE_SCHEMA_067_MIGRATION,
            PLANE_SCHEMA_074_MIGRATION,
            PLANE_SCHEMA_074_002_MIGRATION,
            PLANE_SCHEMA_074_003_MIGRATION,
            PLANE_SCHEMA_074_004_MIGRATION,
            PLANE_SCHEMA_075_MIGRATION,
            PLANE_SCHEMA_079_MIGRATION,
            PLANE_SCHEMA_088_MIGRATION,
            PLANE_SCHEMA_088_002_MIGRATION,
            PLANE_SCHEMA_088_003_MIGRATION,
            PLANE_SCHEMA_088_004_MIGRATION,
            PLANE_SCHEMA_088_005_MIGRATION,
            PLANE_SCHEMA_088_006_MIGRATION,
            PLANE_SCHEMA_088_007_MIGRATION,
            PLANE_SCHEMA_088_008_MIGRATION,
        ),
        current_schema_verifier=_verify_current_plane_schema,
        current_schema_verifier_checksum=(PLANE_SCHEMA_088_008_SCHEMA_VERIFIER_CHECKSUM),
        predecessor_schema_verifier=_verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=(
            PLANE_SCHEMA_088_008_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM
        ),
    ).digest
    != PLANE_SCHEMA_088_008_REGISTRY_DIGEST
):
    raise MigrationDefinitionError(
        "historical 088.008 migration registry no longer matches its pinned digest"
    )
PLANE_SCHEMA_089_001_MIGRATION: Final = Migration(
    name="astralplane-089-typesafe-credentials",
    source_revisions=("088.008",),
    target_revision="089.001",
    checksum=_statements_checksum(TYPESAFE_CREDENTIAL_SCHEMA_STATEMENTS),
    operation=_apply_plane_schema_089_001,
)
# Pinned historical value; recomputing would hide drift
PLANE_SCHEMA_089_001_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "7123aabb64906d6bb6875921f597f88cbcba5df9393c57786afe830044280016"
)
PLANE_SCHEMA_089_001_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "bff0d85f218b82544c953134e59524552745032890cf5de0ad3814951aeef4f8"
)
PLANE_SCHEMA_089_001_REGISTRY_DIGEST: Final = (
    "35741bd0de148f836cd8b75b160531013836a61bd46b9e17e7790641412979d8"
)
if (
    MigrationRegistry(
        (
            PLANE_SCHEMA_067_MIGRATION,
            PLANE_SCHEMA_074_MIGRATION,
            PLANE_SCHEMA_074_002_MIGRATION,
            PLANE_SCHEMA_074_003_MIGRATION,
            PLANE_SCHEMA_074_004_MIGRATION,
            PLANE_SCHEMA_075_MIGRATION,
            PLANE_SCHEMA_079_MIGRATION,
            PLANE_SCHEMA_088_MIGRATION,
            PLANE_SCHEMA_088_002_MIGRATION,
            PLANE_SCHEMA_088_003_MIGRATION,
            PLANE_SCHEMA_088_004_MIGRATION,
            PLANE_SCHEMA_088_005_MIGRATION,
            PLANE_SCHEMA_088_006_MIGRATION,
            PLANE_SCHEMA_088_007_MIGRATION,
            PLANE_SCHEMA_088_008_MIGRATION,
            PLANE_SCHEMA_089_001_MIGRATION,
        ),
        current_schema_verifier=_verify_current_plane_schema,
        current_schema_verifier_checksum=(PLANE_SCHEMA_089_001_SCHEMA_VERIFIER_CHECKSUM),
        predecessor_schema_verifier=_verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=(
            PLANE_SCHEMA_089_001_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM
        ),
    ).digest
    != PLANE_SCHEMA_089_001_REGISTRY_DIGEST
):
    raise MigrationDefinitionError(
        "historical 089.001 migration registry no longer matches its pinned digest"
    )
PLANE_SCHEMA_089_002_MIGRATION: Final = Migration(
    name="astralplane-089-mesh-enrollment-records",
    source_revisions=("089.001",),
    target_revision="089.002",
    checksum=_statements_checksum(MESH_SCHEMA_STATEMENTS),
    operation=_apply_plane_schema_089_002,
)
PLANE_SCHEMA_089_002_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "d4ab9dc75093637ad6dbcbd42d4806799b2285bd3a2191d3d057cab9281b4173"
)
PLANE_SCHEMA_089_002_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "aa15cde43236661c4b63656910cda66dce17a222aaf64a3be61df5360faa3388"
)
PLANE_SCHEMA_089_002_REGISTRY_DIGEST: Final = (
    "8570c65182f5c90f12319435f9b2614d6ebfff0d1404e4650df6f2bd34962565"
)
PLANE_SCHEMA_089_003_MIGRATION: Final = Migration(
    name="astralplane-owner-stop-epochs",
    source_revisions=("089.002",),
    target_revision="089.003",
    checksum=_statements_checksum(STOP_SCHEMA_STATEMENTS),
    operation=_apply_plane_schema_089_003,
)
PLANE_SCHEMA_089_004_MIGRATION: Final = Migration(
    name="astralplane-089-completion-wake",
    source_revisions=("089.003",),
    target_revision="089.004",
    checksum=_statements_checksum(COMPLETION_WAKE_SCHEMA_STATEMENTS),
    operation=_apply_plane_schema_089_004,
)
PLANE_SCHEMA_089_004_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "48d93ab8d6372404c557a816e6d1b57bde5e8c2a3ae64c91d0c1e67ae27337a5"
)
PLANE_SCHEMA_089_004_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "e3f2988fea4b6e8cf7edd9064cec67b3d0665f0863de40bbd8332ff9e7f0a9f3"
)
PLANE_SCHEMA_089_004_REGISTRY_DIGEST: Final = (
    "08b7d824a62284351d1ce178708bb185d7c81f77c96ab14e890f7e5675a008fc"
)
PLANE_SCHEMA_089_005_MIGRATION: Final = Migration(
    name="astralplane-089-notification-records",
    source_revisions=("089.004",),
    target_revision="089.005",
    checksum=_statements_checksum(NOTIFICATION_SCHEMA_STATEMENTS),
    operation=_apply_plane_schema_089_005,
)
PLANE_SCHEMA_089_005_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "e6fe3fdb7a7c87498589b5d9eb11ce7a9c98e6a42619127f585e6c23ca76e1b3"
)
PLANE_SCHEMA_089_005_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "0cf15dc0f990fc103169bf0d1c2137b434cfa8ecf63922aed6741092778918f4"
)
PLANE_SCHEMA_089_005_REGISTRY_DIGEST: Final = (
    "98a9082bb3317192cd6e48df3b63c76dbb9404814cb4a0180eb997f8737e37b0"
)
PLANE_SCHEMA_089_006_MIGRATION: Final = Migration(
    name="astralplane-089-atlas-pages",
    source_revisions=("089.005",),
    target_revision="089.006",
    checksum=_statements_checksum(ATLAS_PAGE_SCHEMA_STATEMENTS),
    operation=_apply_plane_schema_089_006,
)
PLANE_SCHEMA_089_006_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "6911bade08880589f3a7de5a5654412ce14baa207b451d611fa41a1c27aaabed"
)
PLANE_SCHEMA_089_006_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "6415846f481719dc158cde3940668df747b7f3b80a49474e0d8ce3cbca640d48"
)
PLANE_SCHEMA_089_006_REGISTRY_DIGEST: Final = (
    "1176ee5a65f74e0447abe2e63bb90808acc40289acd1a6a09ec6a10ae1a967b1"
)
PLANE_SCHEMA_089_007_MIGRATION: Final = Migration(
    name="astralplane-089-atlas-revisions",
    source_revisions=("089.006",),
    target_revision="089.007",
    checksum=_statements_checksum(ATLAS_REVISION_SCHEMA_STATEMENTS),
    operation=_apply_plane_schema_089_007,
)
PLANE_SCHEMA_089_007_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "d9260f27198ba19c1c9d79d25b5c5aed04c4c2dcd5dc845823043928ce8c3a7a"
)
PLANE_SCHEMA_089_007_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "b97a96083497b737bf9051ff132353edd6bf036f43f29e4af76b4c17d6178fd8"
)
PLANE_SCHEMA_089_007_REGISTRY_DIGEST: Final = (
    "fe60420679846e7ea06ca5da3cef250cde5ad75a4ca47b6eb15739893a8ebd09"
)
PLANE_SCHEMA_089_008_MIGRATION: Final = Migration(
    name="astralplane-089-atlas-links",
    source_revisions=("089.007",),
    target_revision="089.008",
    checksum=_statements_checksum(ATLAS_LINK_SCHEMA_STATEMENTS),
    operation=_apply_plane_schema_089_008,
)
PLANE_SCHEMA_089_008_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "a18e07c3dcb6bc449a4b182549e53aa6dce1ff02cce9dcf8e48fe1a7af965388"
)
PLANE_SCHEMA_089_008_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "9a19be2c191583eb71180aaae46da1e66fa4e4190cd8d5d8f8859a99da3fb899"
)
PLANE_SCHEMA_089_008_REGISTRY_DIGEST: Final = (
    "3845856d7280e5ebd5e516135344b660cde8e3cff2a86f2ab50b0d4e0cf7999d"
)
PLANE_SCHEMA_089_009_MIGRATION: Final = Migration(
    name="astralplane-089-capability-publications",
    source_revisions=("089.008",),
    target_revision="089.009",
    checksum=_statements_checksum(CAPABILITY_PUBLICATION_SCHEMA_STATEMENTS),
    operation=_apply_plane_schema_089_009,
)
PLANE_SCHEMA_089_009_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "b90a854e8d71b8193c352910d49b774b3c3c51bf74c17f50ef750f4e95782498"
)
PLANE_SCHEMA_089_009_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "004f82523616cb70da25657815e55f04c31520fc57ed5527403771c99ae438bf"
)
PLANE_SCHEMA_089_009_REGISTRY_DIGEST: Final = (
    "c3e2b3d2d2a2f88a7b995eb6525f6f01df463bad5129a989f7ed70be5e95ed4b"
)
PLANE_SCHEMA_089_010_MIGRATION: Final = Migration(
    name="astralplane-089-operation-harness-bindings",
    source_revisions=("089.009",),
    target_revision="089.010",
    checksum=_statements_checksum(HARNESS_BINDING_SCHEMA_STATEMENTS),
    operation=_apply_plane_schema_089_010,
)
PLANE_SCHEMA_089_010_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "91cc90c81950761611eca76678a45c76969a5fbcb52e25b119b32939666ad4c6"
)
PLANE_SCHEMA_089_010_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "d9cce4785093bfc4fd308af655d5809f14cb8bf5044bbb39ae827a4b397d129d"
)
PLANE_SCHEMA_089_010_REGISTRY_DIGEST: Final = (
    "facf476fbde30deb1952b352e2f80d56742956c368b9f97e3e04478934cc2d9b"
)
PLANE_SCHEMA_089_011_MIGRATION: Final = Migration(
    name="astralplane-089-push-deliveries",
    source_revisions=("089.010",),
    target_revision="089.011",
    checksum=_statements_checksum(PUSH_DELIVERY_SCHEMA_STATEMENTS),
    operation=_apply_plane_schema_089_011,
)
PLANE_SCHEMA_089_011_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "a2f00c314f47ff36e9f522db4e332cad4ac7af3d1a82622909a390eed22f0b7a"
)
PLANE_SCHEMA_089_011_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "136f65bce0a5c757ebc3a035da0d5b7426c750b13d40023dc8999df880bd58d1"
)
PLANE_SCHEMA_089_011_REGISTRY_DIGEST: Final = (
    "8677b4e37ea98a7471ca97a82bc0182aed0f68002cace976ecfaacf8487d04fc"
)
PLANE_SCHEMA_089_012_MIGRATION: Final = Migration(
    name="astralplane-089-observation-records",
    source_revisions=("089.011",),
    target_revision="089.012",
    checksum=_statements_checksum(OBSERVATION_SCHEMA_STATEMENTS),
    operation=_apply_plane_schema_089_012,
)
PLANE_SCHEMA_089_012_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "e2bb080e5dc95c3c90cd46b4e3dde7c366c9b2d68d6c7c337f794e6920462e1e"
)
PLANE_SCHEMA_089_012_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "bbd0d0cededef7d894ef3fb43a840e3d104e06e55c813137ee6a6a73d5ad7398"
)
PLANE_SCHEMA_089_012_REGISTRY_DIGEST: Final = (
    "58ba65b80bf3c1019c6c110d59cc618813668f7916f1621d851e58199738dc9e"
)
PLANE_SCHEMA_089_013_MIGRATION: Final = Migration(
    name="astralplane-089-projects",
    source_revisions=("089.012",),
    target_revision="089.013",
    checksum=_statements_checksum(PROJECT_SCHEMA_STATEMENTS),
    operation=_apply_plane_schema_089_013,
)
PLANE_SCHEMA_089_013_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "1aa4102bf3d982e67cb26bec2d15762e75c4d3ad639534f17d1dcd0ef25a789c"
)
PLANE_SCHEMA_089_013_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "d61f5668c8ba83976187943cace6822a5a7441faceb4d7ddeb09b22368b71428"
)
PLANE_SCHEMA_089_013_REGISTRY_DIGEST: Final = (
    "4a346c04dfb71e87afcf0444e196f9771977005e0f925c1336d43b1ed1bd3869"
)
PLANE_SCHEMA_089_014_MIGRATION: Final = Migration(
    name="astralplane-089-harness-captures",
    source_revisions=("089.013",),
    target_revision="089.014",
    checksum=_statements_checksum(HARNESS_CAPTURE_SCHEMA_STATEMENTS),
    operation=_apply_plane_schema_089_014,
)
PLANE_SCHEMA_089_014_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "0292ecd07549047e50ed79896a632e10b241b23a5ca0e688dc1daaeda9a9004d"
)
PLANE_SCHEMA_089_014_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "132a98eda33d5b9166f74d388c61b750cce4b1069cb15f756ff85f2b2335e9e1"
)
PLANE_SCHEMA_089_014_REGISTRY_DIGEST: Final = (
    "5a03a9f11a22530ae80a11036d0ddaf72d084797546285a71ebdbc3fd741bb06"
)
PLANE_SCHEMA_089_015_MIGRATION: Final = Migration(
    name="astralplane-089-audit-conversation-index",
    source_revisions=("089.014",),
    target_revision="089.015",
    checksum=_statements_checksum(AUDIT_CONVERSATION_SCHEMA_STATEMENTS),
    operation=_apply_plane_schema_089_015,
)
PLANE_SCHEMA_089_015_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "1f9ec8b14a9896084d983e2b67753fdf7515097d74e916c2315c365c3292a45f"
)
PLANE_SCHEMA_089_015_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "cd117c845e11e3ee4f7c9bb6f858a2b228e6afa104c5923441e0fabb22c304db"
)
PLANE_SCHEMA_089_015_REGISTRY_DIGEST: Final = (
    "d609f1cf1d02c744ff8b35115f8c9be2ebb873a7fa987931c08154d2ecfcb857"
)
PLANE_SCHEMA_089_016_MIGRATION: Final = Migration(
    name="astralplane-089-chat-queue",
    source_revisions=("089.015",),
    target_revision="089.016",
    checksum=_statements_checksum(CHAT_QUEUE_SCHEMA_STATEMENTS),
    operation=_apply_plane_schema_089_016,
)
PLANE_SCHEMA_089_016_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "059c16e9ba8c3544e6671e165472ccdb99f425ddc315717f4b3b4650e9dbce64"
)
PLANE_SCHEMA_089_016_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "b3e0f3867df38951b63923df5f6259a18de963e3984fee0be8802ed03966db33"
)
PLANE_SCHEMA_089_016_REGISTRY_DIGEST: Final = (
    "fd43a7ce895389d9ca951723e7be6f0eb97551d7b7d5e1b0f4897a99ede4ed6c"
)
PLANE_SCHEMA_089_017_MIGRATION: Final = Migration(
    name="astralplane-089-conversation-branches",
    source_revisions=("089.016",),
    target_revision="089.017",
    checksum=_statements_checksum(CONVERSATION_BRANCH_SCHEMA_STATEMENTS),
    operation=_apply_plane_schema_089_017,
)
PLANE_SCHEMA_089_017_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "89ecc73b7e9b5f6f0a829a28abc93f3a0d85a3e7afa4f2f158b20e5fee3c7308"
)
PLANE_SCHEMA_089_017_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "59e8ffa8ccd1745741925058e15d7335a4a539b870b3677afde3195806773af5"
)
PLANE_SCHEMA_089_017_REGISTRY_DIGEST: Final = (
    "99862c91fe45769d9b037987e80dbd3b685be5f7afbf0bafb2c895d15592c0f5"
)
PLANE_SCHEMA_089_018_MIGRATION: Final = Migration(
    name="astralplane-089-computer-use",
    source_revisions=("089.017",),
    target_revision="089.018",
    checksum=_statements_checksum(COMPUTER_USE_SCHEMA_STATEMENTS),
    operation=_apply_plane_schema_089_018,
)
PLANE_SCHEMA_089_018_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "d49c20e41432617a68a969ce89dc5a66bcaf8c48734b6318a6271afe52b55efc"
)
PLANE_SCHEMA_089_018_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "7219a65ae24832f0bbf1a29fce458a46c65990eb78cce837b43ba5c17154f581"
)
PLANE_SCHEMA_089_018_REGISTRY_DIGEST: Final = (
    "30a2982f3e2ffa8bd96e917b432e8f63ba77587d32d06c362457bb8cf9195492"
)
PLANE_SCHEMA_089_019_MIGRATION: Final = Migration(
    name="astralplane-089-verification-credentials",
    source_revisions=("089.018",),
    target_revision="089.019",
    checksum=_statements_checksum(VERIFICATION_CREDENTIAL_SCHEMA_STATEMENTS),
    operation=_apply_plane_schema_089_019,
)
PLANE_SCHEMA_089_019_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "908c445c4722c9dc9c1bc9e30a5beb855701d25e5accbbe78ada05cb11447ea8"
)
PLANE_SCHEMA_089_019_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "5ca27137697062b307834d4c41ee970dad04ef5058f8c971acf021c99548e7e6"
)
PLANE_SCHEMA_089_019_REGISTRY_DIGEST: Final = (
    "5c81191286b6cad6ef12d3e1005af7577b908f0a1b5ecbe63606c7e34a553746"
)
PLANE_SCHEMA_089_020_MIGRATION: Final = Migration(
    name="astralplane-089-chat-read-receipts",
    source_revisions=("089.019",),
    target_revision="089.020",
    checksum=_statements_checksum(CHAT_READ_RECEIPT_SCHEMA_STATEMENTS),
    operation=_apply_plane_schema_089_020,
)
PLANE_SCHEMA_089_020_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "d75843ce5c5dbb94710db9686ea044ff187d2db2182ec474c203fceeed08e58f"
)
PLANE_SCHEMA_089_020_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "3af8aea2946271a51338c96c6db3dc944332d9044168e02665b2fc6d1bcffb60"
)
PLANE_SCHEMA_089_020_REGISTRY_DIGEST: Final = (
    "82e6c45424227803221853d05c7fed7496de11b1c7025124f1c745a7736a793f"
)
PLANE_SCHEMA_089_021_MIGRATION: Final = Migration(
    name="astralplane-089-mesh-event-store",
    source_revisions=("089.020",),
    target_revision="089.021",
    checksum=_statements_checksum(MESH_EVENT_SCHEMA_STATEMENTS),
    operation=_apply_plane_schema_089_021,
)
PLANE_SCHEMA_089_021_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "3dd7fe6a7dbdaa793a34ee6bafdca64cdd9fcf3a66e6985175adf4d06b18b53b"
)
PLANE_SCHEMA_089_021_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM: Final = (
    "5eff29adc4c4e8ae9d620c1e49b3bdf049ee6520848a211731f55f2d0ec2b749"
)
PLANE_SCHEMA_089_021_REGISTRY_DIGEST: Final = (
    "c7bdb010951e3d27b13b9f6fdc7e3d8a4e70ac34478807df12c2727b6b3739e6"
)
PLANE_SCHEMA_089_022_MIGRATION: Final = Migration(
    name="astralplane-089-run-and-learning-ledger",
    source_revisions=("089.021",),
    target_revision="089.022",
    checksum=_statements_checksum(RUN_LEDGER_SCHEMA_STATEMENTS),
    operation=_apply_plane_schema_089_022,
)
MIGRATION_REGISTRY: Final = MigrationRegistry(
    (
        PLANE_SCHEMA_067_MIGRATION,
        PLANE_SCHEMA_074_MIGRATION,
        PLANE_SCHEMA_074_002_MIGRATION,
        PLANE_SCHEMA_074_003_MIGRATION,
        PLANE_SCHEMA_074_004_MIGRATION,
        PLANE_SCHEMA_075_MIGRATION,
        PLANE_SCHEMA_079_MIGRATION,
        PLANE_SCHEMA_088_MIGRATION,
        PLANE_SCHEMA_088_002_MIGRATION,
        PLANE_SCHEMA_088_003_MIGRATION,
        PLANE_SCHEMA_088_004_MIGRATION,
        PLANE_SCHEMA_088_005_MIGRATION,
        PLANE_SCHEMA_088_006_MIGRATION,
        PLANE_SCHEMA_088_007_MIGRATION,
        PLANE_SCHEMA_088_008_MIGRATION,
        PLANE_SCHEMA_089_001_MIGRATION,
        PLANE_SCHEMA_089_002_MIGRATION,
        PLANE_SCHEMA_089_003_MIGRATION,
        PLANE_SCHEMA_089_004_MIGRATION,
        PLANE_SCHEMA_089_005_MIGRATION,
        PLANE_SCHEMA_089_006_MIGRATION,
        PLANE_SCHEMA_089_007_MIGRATION,
        PLANE_SCHEMA_089_008_MIGRATION,
        PLANE_SCHEMA_089_009_MIGRATION,
        PLANE_SCHEMA_089_010_MIGRATION,
        PLANE_SCHEMA_089_011_MIGRATION,
        PLANE_SCHEMA_089_012_MIGRATION,
        PLANE_SCHEMA_089_013_MIGRATION,
        PLANE_SCHEMA_089_014_MIGRATION,
        PLANE_SCHEMA_089_015_MIGRATION,
        PLANE_SCHEMA_089_016_MIGRATION,
        PLANE_SCHEMA_089_017_MIGRATION,
        PLANE_SCHEMA_089_018_MIGRATION,
        PLANE_SCHEMA_089_019_MIGRATION,
        PLANE_SCHEMA_089_020_MIGRATION,
        PLANE_SCHEMA_089_021_MIGRATION,
        PLANE_SCHEMA_089_022_MIGRATION,
    ),
    current_schema_verifier=_verify_current_plane_schema,
    current_schema_verifier_checksum=CURRENT_SCHEMA_VERIFIER_CHECKSUM,
    predecessor_schema_verifier=_verify_predecessor_plane_schema,
    predecessor_schema_verifier_checksum=PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM,
)
if (
    MigrationRegistry(
        tuple(edge for edge in MIGRATION_REGISTRY.migrations if edge.target_revision <= "089.002"),
        current_schema_verifier=_verify_current_plane_schema,
        current_schema_verifier_checksum=PLANE_SCHEMA_089_002_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=_verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=PLANE_SCHEMA_089_002_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM,
    ).digest
    != PLANE_SCHEMA_089_002_REGISTRY_DIGEST
):
    raise MigrationDefinitionError(
        "historical mesh migration registry no longer matches its digest"
    )
PLANE_SCHEMA_089_003_REGISTRY_DIGEST: Final = (
    "773f574efc9657de7692d6ae27352edbb07ef93ed967f4d5ca121ab5c0e1ac7e"
)
if (
    MigrationRegistry(
        tuple(edge for edge in MIGRATION_REGISTRY.migrations if edge.target_revision <= "089.004"),
        current_schema_verifier=_verify_current_plane_schema,
        current_schema_verifier_checksum=PLANE_SCHEMA_089_004_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=_verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=(
            PLANE_SCHEMA_089_004_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM
        ),
    ).digest
    != PLANE_SCHEMA_089_004_REGISTRY_DIGEST
):
    raise MigrationDefinitionError(
        "historical completion-wake migration registry no longer matches its digest"
    )
if (
    MigrationRegistry(
        tuple(edge for edge in MIGRATION_REGISTRY.migrations if edge.target_revision <= "089.005"),
        current_schema_verifier=_verify_current_plane_schema,
        current_schema_verifier_checksum=PLANE_SCHEMA_089_005_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=_verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=PLANE_SCHEMA_089_005_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM,
    ).digest
    != PLANE_SCHEMA_089_005_REGISTRY_DIGEST
):
    raise MigrationDefinitionError(
        "historical notification migration registry no longer matches its digest"
    )
if (
    MigrationRegistry(
        tuple(edge for edge in MIGRATION_REGISTRY.migrations if edge.target_revision <= "089.006"),
        current_schema_verifier=_verify_current_plane_schema,
        current_schema_verifier_checksum=PLANE_SCHEMA_089_006_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=_verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=(
            PLANE_SCHEMA_089_006_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM
        ),
    ).digest
    != PLANE_SCHEMA_089_006_REGISTRY_DIGEST
):
    raise MigrationDefinitionError(
        "historical atlas-page migration registry no longer matches its digest"
    )
if (
    MigrationRegistry(
        tuple(edge for edge in MIGRATION_REGISTRY.migrations if edge.target_revision <= "089.007"),
        current_schema_verifier=_verify_current_plane_schema,
        current_schema_verifier_checksum=PLANE_SCHEMA_089_007_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=_verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=(
            PLANE_SCHEMA_089_007_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM
        ),
    ).digest
    != PLANE_SCHEMA_089_007_REGISTRY_DIGEST
):
    raise MigrationDefinitionError(
        "historical atlas-revision migration registry no longer matches its digest"
    )
if (
    MigrationRegistry(
        tuple(edge for edge in MIGRATION_REGISTRY.migrations if edge.target_revision <= "089.008"),
        current_schema_verifier=_verify_current_plane_schema,
        current_schema_verifier_checksum=PLANE_SCHEMA_089_008_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=_verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=PLANE_SCHEMA_089_008_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM,
    ).digest
    != PLANE_SCHEMA_089_008_REGISTRY_DIGEST
):
    raise MigrationDefinitionError("historical migration registry no longer matches its digest")
if (
    MigrationRegistry(
        tuple(edge for edge in MIGRATION_REGISTRY.migrations if edge.target_revision <= "089.009"),
        current_schema_verifier=_verify_current_plane_schema,
        current_schema_verifier_checksum=PLANE_SCHEMA_089_009_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=_verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=PLANE_SCHEMA_089_009_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM,
    ).digest
    != PLANE_SCHEMA_089_009_REGISTRY_DIGEST
):
    raise MigrationDefinitionError("historical migration registry no longer matches its digest")
if (
    MigrationRegistry(
        tuple(edge for edge in MIGRATION_REGISTRY.migrations if edge.target_revision <= "089.010"),
        current_schema_verifier=_verify_current_plane_schema,
        current_schema_verifier_checksum=PLANE_SCHEMA_089_010_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=_verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=PLANE_SCHEMA_089_010_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM,
    ).digest
    != PLANE_SCHEMA_089_010_REGISTRY_DIGEST
):
    raise MigrationDefinitionError("historical migration registry no longer matches its digest")
if (
    MigrationRegistry(
        tuple(edge for edge in MIGRATION_REGISTRY.migrations if edge.target_revision <= "089.011"),
        current_schema_verifier=_verify_current_plane_schema,
        current_schema_verifier_checksum=PLANE_SCHEMA_089_011_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=_verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=PLANE_SCHEMA_089_011_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM,
    ).digest
    != PLANE_SCHEMA_089_011_REGISTRY_DIGEST
):
    raise MigrationDefinitionError("historical migration registry no longer matches its digest")
if (
    MigrationRegistry(
        tuple(edge for edge in MIGRATION_REGISTRY.migrations if edge.target_revision <= "089.012"),
        current_schema_verifier=_verify_current_plane_schema,
        current_schema_verifier_checksum=PLANE_SCHEMA_089_012_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=_verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=PLANE_SCHEMA_089_012_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM,
    ).digest
    != PLANE_SCHEMA_089_012_REGISTRY_DIGEST
):
    raise MigrationDefinitionError("historical migration registry no longer matches its digest")
if (
    MigrationRegistry(
        tuple(edge for edge in MIGRATION_REGISTRY.migrations if edge.target_revision <= "089.013"),
        current_schema_verifier=_verify_current_plane_schema,
        current_schema_verifier_checksum=PLANE_SCHEMA_089_013_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=_verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=PLANE_SCHEMA_089_013_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM,
    ).digest != PLANE_SCHEMA_089_013_REGISTRY_DIGEST
):
    raise MigrationDefinitionError("historical migration registry no longer matches its digest")
if (
    MigrationRegistry(
        tuple(edge for edge in MIGRATION_REGISTRY.migrations if edge.target_revision <= "089.014"),
        current_schema_verifier=_verify_current_plane_schema,
        current_schema_verifier_checksum=PLANE_SCHEMA_089_014_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=_verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=PLANE_SCHEMA_089_014_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM,
    ).digest != PLANE_SCHEMA_089_014_REGISTRY_DIGEST
):
    raise MigrationDefinitionError("historical migration registry no longer matches its digest")
if (
    MigrationRegistry(
        tuple(edge for edge in MIGRATION_REGISTRY.migrations if edge.target_revision <= "089.015"),
        current_schema_verifier=_verify_current_plane_schema,
        current_schema_verifier_checksum=PLANE_SCHEMA_089_015_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=_verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=(
            PLANE_SCHEMA_089_015_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM
        ),
    ).digest != PLANE_SCHEMA_089_015_REGISTRY_DIGEST
):
    raise MigrationDefinitionError("historical migration registry no longer matches its digest")
if (
    MigrationRegistry(
        tuple(edge for edge in MIGRATION_REGISTRY.migrations if edge.target_revision <= "089.016"),
        current_schema_verifier=_verify_current_plane_schema,
        current_schema_verifier_checksum=PLANE_SCHEMA_089_016_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=_verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=(
            PLANE_SCHEMA_089_016_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM
        ),
    ).digest != PLANE_SCHEMA_089_016_REGISTRY_DIGEST
):
    raise MigrationDefinitionError("historical migration registry no longer matches its digest")
if (
    MigrationRegistry(
        tuple(edge for edge in MIGRATION_REGISTRY.migrations if edge.target_revision <= "089.017"),
        current_schema_verifier=_verify_current_plane_schema,
        current_schema_verifier_checksum=PLANE_SCHEMA_089_017_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=_verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=(
            PLANE_SCHEMA_089_017_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM
        ),
    ).digest != PLANE_SCHEMA_089_017_REGISTRY_DIGEST
):
    raise MigrationDefinitionError("historical migration registry no longer matches its digest")
if (
    MigrationRegistry(
        tuple(edge for edge in MIGRATION_REGISTRY.migrations if edge.target_revision <= "089.018"),
        current_schema_verifier=_verify_current_plane_schema,
        current_schema_verifier_checksum=PLANE_SCHEMA_089_018_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=_verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=(
            PLANE_SCHEMA_089_018_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM
        ),
    ).digest != PLANE_SCHEMA_089_018_REGISTRY_DIGEST
):
    raise MigrationDefinitionError("historical migration registry no longer matches its digest")
if (
    MigrationRegistry(
        tuple(edge for edge in MIGRATION_REGISTRY.migrations if edge.target_revision <= "089.019"),
        current_schema_verifier=_verify_current_plane_schema,
        current_schema_verifier_checksum=PLANE_SCHEMA_089_019_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=_verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=(
            PLANE_SCHEMA_089_019_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM
        ),
    ).digest != PLANE_SCHEMA_089_019_REGISTRY_DIGEST
):
    raise MigrationDefinitionError("historical migration registry no longer matches its digest")
if (
    MigrationRegistry(
        tuple(edge for edge in MIGRATION_REGISTRY.migrations if edge.target_revision <= "089.020"),
        current_schema_verifier=_verify_current_plane_schema,
        current_schema_verifier_checksum=PLANE_SCHEMA_089_020_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=_verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=(
            PLANE_SCHEMA_089_020_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM
        ),
    ).digest != PLANE_SCHEMA_089_020_REGISTRY_DIGEST
):
    raise MigrationDefinitionError("historical migration registry no longer matches its digest")
if (
    MigrationRegistry(
        tuple(edge for edge in MIGRATION_REGISTRY.migrations if edge.target_revision <= "089.021"),
        current_schema_verifier=_verify_current_plane_schema,
        current_schema_verifier_checksum=PLANE_SCHEMA_089_021_SCHEMA_VERIFIER_CHECKSUM,
        predecessor_schema_verifier=_verify_predecessor_plane_schema,
        predecessor_schema_verifier_checksum=(
            PLANE_SCHEMA_089_021_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM
        ),
    ).digest != PLANE_SCHEMA_089_021_REGISTRY_DIGEST
):
    raise MigrationDefinitionError("historical migration registry no longer matches its digest")
MIGRATION_DIGEST: Final = MIGRATION_REGISTRY.digest
CURRENT_DATA_PLANE_REVISION: Final = DataPlaneRevision(
    schema_revision="089.022",
    read_compatible_from=(
        "066.001",
        "067.001",
        "074.001",
        "074.002",
        "074.003",
        "074.004",
        "075.001",
        "079.001",
        "088.001",
        "088.002",
        "088.003",
        "088.004",
        "088.005",
        "088.006",
        "088.007",
        "088.008",
        "089.001",
        "089.002",
        "089.003",
        "089.004",
        "089.005",
        "089.006",
        "089.007",
        "089.008",
        "089.009",
        "089.010",
        "089.011",
        "089.012",
        "089.013",
        "089.014",
        "089.015",
        "089.016",
        "089.017",
        "089.018",
        "089.019",
        "089.020",
        "089.021",
    ),
    migration_digest=MIGRATION_DIGEST,
    accepted_predecessor_digests=(
        ("067.001", PLANE_SCHEMA_067_REGISTRY_DIGEST),
        ("074.001", PLANE_SCHEMA_074_001_REGISTRY_DIGEST),
        ("074.002", PLANE_SCHEMA_074_002_REGISTRY_DIGEST),
        ("074.003", PLANE_SCHEMA_074_003_REGISTRY_DIGEST),
        ("074.004", PLANE_SCHEMA_074_004_REGISTRY_DIGEST),
        ("075.001", PLANE_SCHEMA_075_REGISTRY_DIGEST),
        ("079.001", PLANE_SCHEMA_079_REGISTRY_DIGEST),
        ("088.001", PLANE_SCHEMA_088_REGISTRY_DIGEST),
        ("088.002", PLANE_SCHEMA_088_002_REGISTRY_DIGEST),
        ("088.003", PLANE_SCHEMA_088_003_REGISTRY_DIGEST),
        ("088.004", PLANE_SCHEMA_088_004_REGISTRY_DIGEST),
        ("088.005", PLANE_SCHEMA_088_005_REGISTRY_DIGEST),
        ("088.006", PLANE_SCHEMA_088_006_REGISTRY_DIGEST),
        ("088.007", PLANE_SCHEMA_088_007_REGISTRY_DIGEST),
        ("088.008", PLANE_SCHEMA_088_008_REGISTRY_DIGEST),
        ("089.001", PLANE_SCHEMA_089_001_REGISTRY_DIGEST),
        ("089.002", PLANE_SCHEMA_089_002_REGISTRY_DIGEST),
        ("089.003", PLANE_SCHEMA_089_003_REGISTRY_DIGEST),
        ("089.004", PLANE_SCHEMA_089_004_REGISTRY_DIGEST),
        ("089.005", PLANE_SCHEMA_089_005_REGISTRY_DIGEST),
        ("089.006", PLANE_SCHEMA_089_006_REGISTRY_DIGEST),
        ("089.007", PLANE_SCHEMA_089_007_REGISTRY_DIGEST),
        ("089.008", PLANE_SCHEMA_089_008_REGISTRY_DIGEST),
        ("089.009", PLANE_SCHEMA_089_009_REGISTRY_DIGEST),
        ("089.010", PLANE_SCHEMA_089_010_REGISTRY_DIGEST),
        ("089.011", PLANE_SCHEMA_089_011_REGISTRY_DIGEST),
        ("089.012", PLANE_SCHEMA_089_012_REGISTRY_DIGEST),
        ("089.013", PLANE_SCHEMA_089_013_REGISTRY_DIGEST),
        ("089.014", PLANE_SCHEMA_089_014_REGISTRY_DIGEST),
        ("089.015", PLANE_SCHEMA_089_015_REGISTRY_DIGEST),
        ("089.016", PLANE_SCHEMA_089_016_REGISTRY_DIGEST),
        ("089.017", PLANE_SCHEMA_089_017_REGISTRY_DIGEST),
        ("089.018", PLANE_SCHEMA_089_018_REGISTRY_DIGEST),
        ("089.019", PLANE_SCHEMA_089_019_REGISTRY_DIGEST),
        ("089.020", PLANE_SCHEMA_089_020_REGISTRY_DIGEST),
        ("089.021", PLANE_SCHEMA_089_021_REGISTRY_DIGEST),
    ),
)


__all__ = (
    "ATLAS_LINK_SCHEMA_STATEMENTS",
    "ATLAS_PAGE_SCHEMA_STATEMENTS",
    "ATLAS_REVISION_SCHEMA_STATEMENTS",
    "CAPABILITY_PUBLICATION_SCHEMA_STATEMENTS",
    "CHAT_READ_RECEIPT_SCHEMA_STATEMENTS",
    "COMPUTER_USE_SCHEMA_STATEMENTS",
    "CONVERSATION_BRANCH_SCHEMA_STATEMENTS",
    "CURRENT_DATA_PLANE_REVISION",
    "CURRENT_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS",
    "CURRENT_SCHEMA_STRUCTURE_DIGEST",
    "CURRENT_SCHEMA_STRUCTURE_QUERY",
    "CURRENT_SCHEMA_VERIFICATION_STATEMENTS",
    "CURRENT_SCHEMA_VERIFIER_CHECKSUM",
    "DECLARATIVE_AGENT_SCHEMA_STATEMENTS",
    "FRAMEWORK_CREDENTIAL_SCHEMA_STATEMENTS",
    "GUIDANCE_SCHEMA_STATEMENTS",
    "HARNESS_BINDING_SCHEMA_STATEMENTS",
    "MESH_EVENT_SCHEMA_STATEMENTS",
    "MESH_SCHEMA_STATEMENTS",
    "MIGRATION_DIGEST",
    "MIGRATION_REGISTRY",
    "NOTIFICATION_SCHEMA_STATEMENTS",
    "OBSERVATION_SCHEMA_STATEMENTS",
    "PLANE_SCHEMA_067_MIGRATION",
    "PLANE_SCHEMA_067_STATEMENTS",
    "PLANE_SCHEMA_074_001_REGISTRY_DIGEST",
    "PLANE_SCHEMA_074_002_MIGRATION",
    "PLANE_SCHEMA_074_002_REGISTRY_DIGEST",
    "PLANE_SCHEMA_074_002_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_074_002_STATEMENTS",
    "PLANE_SCHEMA_074_003_MIGRATION",
    "PLANE_SCHEMA_074_003_STATEMENTS",
    "PLANE_SCHEMA_074_004_MIGRATION",
    "PLANE_SCHEMA_074_004_REGISTRY_DIGEST",
    "PLANE_SCHEMA_074_004_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_074_004_STATEMENTS",
    "PLANE_SCHEMA_075_MIGRATION",
    "PLANE_SCHEMA_075_REGISTRY_DIGEST",
    "PLANE_SCHEMA_075_STATEMENTS",
    "PLANE_SCHEMA_079_MIGRATION",
    "PLANE_SCHEMA_079_REGISTRY_DIGEST",
    "PLANE_SCHEMA_079_STATEMENTS",
    "PLANE_SCHEMA_088_002_MIGRATION",
    "PLANE_SCHEMA_088_002_REGISTRY_DIGEST",
    "PLANE_SCHEMA_088_002_STATEMENTS",
    "PLANE_SCHEMA_088_003_MIGRATION",
    "PLANE_SCHEMA_088_003_REGISTRY_DIGEST",
    "PLANE_SCHEMA_088_003_STATEMENTS",
    "PLANE_SCHEMA_088_004_MIGRATION",
    "PLANE_SCHEMA_088_004_REGISTRY_DIGEST",
    "PLANE_SCHEMA_088_005_MIGRATION",
    "PLANE_SCHEMA_088_005_REGISTRY_DIGEST",
    "PLANE_SCHEMA_088_006_MIGRATION",
    "PLANE_SCHEMA_088_006_REGISTRY_DIGEST",
    "PLANE_SCHEMA_088_007_MIGRATION",
    "PLANE_SCHEMA_088_007_REGISTRY_DIGEST",
    "PLANE_SCHEMA_088_008_MIGRATION",
    "PLANE_SCHEMA_088_008_REGISTRY_DIGEST",
    "PLANE_SCHEMA_088_MIGRATION",
    "PLANE_SCHEMA_088_REGISTRY_DIGEST",
    "PLANE_SCHEMA_089_001_MIGRATION",
    "PLANE_SCHEMA_089_001_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_001_REGISTRY_DIGEST",
    "PLANE_SCHEMA_089_001_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_002_MIGRATION",
    "PLANE_SCHEMA_089_002_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_002_REGISTRY_DIGEST",
    "PLANE_SCHEMA_089_002_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_003_MIGRATION",
    "PLANE_SCHEMA_089_004_MIGRATION",
    "PLANE_SCHEMA_089_004_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_004_REGISTRY_DIGEST",
    "PLANE_SCHEMA_089_004_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_005_MIGRATION",
    "PLANE_SCHEMA_089_005_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_005_REGISTRY_DIGEST",
    "PLANE_SCHEMA_089_005_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_006_MIGRATION",
    "PLANE_SCHEMA_089_006_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_006_REGISTRY_DIGEST",
    "PLANE_SCHEMA_089_006_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_007_MIGRATION",
    "PLANE_SCHEMA_089_008_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_008_REGISTRY_DIGEST",
    "PLANE_SCHEMA_089_008_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_009_MIGRATION",
    "PLANE_SCHEMA_089_009_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_009_REGISTRY_DIGEST",
    "PLANE_SCHEMA_089_009_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_010_MIGRATION",
    "PLANE_SCHEMA_089_010_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_010_REGISTRY_DIGEST",
    "PLANE_SCHEMA_089_010_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_011_MIGRATION",
    "PLANE_SCHEMA_089_011_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_011_REGISTRY_DIGEST",
    "PLANE_SCHEMA_089_011_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_012_MIGRATION",
    "PLANE_SCHEMA_089_012_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_012_REGISTRY_DIGEST",
    "PLANE_SCHEMA_089_012_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_013_MIGRATION",
    "PLANE_SCHEMA_089_014_MIGRATION",
    "PLANE_SCHEMA_089_014_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_014_REGISTRY_DIGEST",
    "PLANE_SCHEMA_089_014_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_015_MIGRATION",
    "PLANE_SCHEMA_089_015_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_015_REGISTRY_DIGEST",
    "PLANE_SCHEMA_089_015_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_016_MIGRATION",
    "PLANE_SCHEMA_089_016_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_016_REGISTRY_DIGEST",
    "PLANE_SCHEMA_089_016_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_017_MIGRATION",
    "PLANE_SCHEMA_089_017_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_017_REGISTRY_DIGEST",
    "PLANE_SCHEMA_089_017_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_018_MIGRATION",
    "PLANE_SCHEMA_089_018_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_018_REGISTRY_DIGEST",
    "PLANE_SCHEMA_089_018_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_019_MIGRATION",
    "PLANE_SCHEMA_089_019_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_019_REGISTRY_DIGEST",
    "PLANE_SCHEMA_089_019_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_020_MIGRATION",
    "PLANE_SCHEMA_089_020_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_020_REGISTRY_DIGEST",
    "PLANE_SCHEMA_089_020_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_021_MIGRATION",
    "PLANE_SCHEMA_089_021_PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_021_REGISTRY_DIGEST",
    "PLANE_SCHEMA_089_021_SCHEMA_VERIFIER_CHECKSUM",
    "PLANE_SCHEMA_089_022_MIGRATION",
    "PREDECESSOR_SCHEMA_COMPATIBLE_STRUCTURE_DIGESTS",
    "PREDECESSOR_SCHEMA_VERIFIER_CHECKSUM",
    "PUSH_DELIVERY_SCHEMA_STATEMENTS",
    "RUN_LEDGER_SCHEMA_STATEMENTS",
    "SCHEDULER_POLICY_SCHEMA_STATEMENTS",
    "SELECTED_INPUT_SCHEMA_STATEMENTS",
    "STOP_SCHEMA_STATEMENTS",
    "TYPESAFE_CREDENTIAL_SCHEMA_STATEMENTS",
    "VERIFICATION_CREDENTIAL_SCHEMA_STATEMENTS",
    "Migration",
    "MigrationRegistry",
    "MigrationReport",
    "MigrationRunner",
)
