-- Synthetic, non-PHI representative data layered onto the canonical Plane 066.001 baseline.
-- The loader executes the provenance-bound LegacyBaseline066Builder before these statements.

-- astralplane-fixture-statement
CREATE TABLE schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- astralplane-fixture-statement
INSERT INTO schema_meta (key, value)
VALUES ('revision', '066.001');

-- astralplane-fixture-statement
CREATE TABLE test_runs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    system_state TEXT NOT NULL,
    categories TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running'
);

-- astralplane-fixture-statement
CREATE TABLE test_case_results (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES test_runs(id),
    suite TEXT NOT NULL,
    test_name TEXT NOT NULL,
    outcome TEXT NOT NULL,
    duration_ms DOUBLE PRECISION DEFAULT 0.0,
    metrics TEXT,
    qualitative TEXT DEFAULT '',
    evidence_hash TEXT DEFAULT '',
    verification_status TEXT NOT NULL DEFAULT 'pending'
);

-- astralplane-fixture-statement
CREATE TABLE test_evidence (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES test_case_results(id),
    evidence_type TEXT NOT NULL,
    data TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    captured_at TEXT NOT NULL
);

-- astralplane-fixture-statement
CREATE TABLE audit_entries (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES test_case_results(id),
    action TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    rationale TEXT DEFAULT '',
    timestamp TEXT NOT NULL,
    previous_hash TEXT NOT NULL
);

-- astralplane-fixture-statement
CREATE TABLE latex_artifacts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES test_runs(id),
    filename TEXT NOT NULL,
    generated_from TEXT NOT NULL,
    verification_complete BOOLEAN NOT NULL DEFAULT FALSE,
    generated_at TEXT NOT NULL
);

-- astralplane-fixture-statement
INSERT INTO chats (id, user_id, title, created_at, updated_at)
VALUES
    ('conversation-1', 'fixture-owner-a', 'Synthetic conversation A', 1000, 1001),
    ('conversation-2', 'fixture-owner-b', 'Synthetic conversation B', 1002, 1003);

-- astralplane-fixture-statement
INSERT INTO messages (chat_id, user_id, role, content, timestamp)
VALUES
    ('conversation-1', 'fixture-owner-a', 'user', 'Synthetic message A', 1010),
    ('conversation-2', 'fixture-owner-b', 'assistant', 'Synthetic message B', 1011);

-- astralplane-fixture-statement
INSERT INTO user_preferences (user_id, preferences, updated_at)
VALUES
    ('fixture-owner-a', '{"theme":"synthetic-dark"}', 1020),
    ('fixture-owner-b', '{"theme":"synthetic-light"}', 1021);

-- astralplane-fixture-statement
INSERT INTO user_attachments (
    attachment_id,
    user_id,
    filename,
    content_type,
    category,
    extension,
    size_bytes,
    sha256,
    storage_path,
    created_at,
    deleted_at
)
VALUES
    (
        'artifact-1',
        'fixture-owner-a',
        'artifact.txt',
        'text/plain',
        'document',
        '.txt',
        49,
        'b70ef33cdab65179930b27076c17c861f3f6b00b7025833378ac39728cec4be4',
        'fixture-owner-a/artifact-1/artifact.txt',
        1030,
        NULL
    ),
    (
        'artifact-2',
        'fixture-owner-b',
        'summary.json',
        'application/json',
        'data',
        '.json',
        41,
        'bf54f7326432f19faee001e9dd8885c8798b70e8173fa1e4252698b3dc1ced9b',
        'fixture-owner-b/artifact-2/summary.json',
        1031,
        1040
    );

-- astralplane-fixture-statement
INSERT INTO test_runs (
    id, started_at, finished_at, system_state, categories, status
)
VALUES (
    'qualification-run-1',
    '2026-01-01T00:00:00+00:00',
    NULL,
    '{"revision":"synthetic-pre-split"}',
    '["unit"]',
    'running'
);

-- astralplane-fixture-statement
INSERT INTO test_case_results (
    id, run_id, suite, test_name, outcome, duration_ms, metrics,
    qualitative, evidence_hash, verification_status
)
VALUES (
    'qualification-case-1',
    'qualification-run-1',
    'plane',
    'test_synthetic_owner_backfill',
    'passed',
    1.5,
    '{"assertions":1}',
    'synthetic qualification case',
    '',
    'pending'
);

-- astralplane-fixture-statement
INSERT INTO test_evidence (
    id, case_id, evidence_type, data, sha256, captured_at
)
VALUES (
    'qualification-evidence-1',
    'qualification-case-1',
    'synthetic-report',
    '{"passed":true}',
    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    '2026-01-01T00:00:01+00:00'
);

-- astralplane-fixture-statement
INSERT INTO audit_entries (
    id, case_id, action, reviewer, rationale, timestamp, previous_hash
)
VALUES (
    'qualification-audit-1',
    'qualification-case-1',
    'verified',
    'synthetic-reviewer',
    'synthetic evidence matches',
    '2026-01-01T00:00:02+00:00',
    ''
);

-- astralplane-fixture-statement
INSERT INTO latex_artifacts (
    id, run_id, filename, generated_from, verification_complete, generated_at
)
VALUES (
    'qualification-artifact-1',
    'qualification-run-1',
    'reports/qualification-run-1.tex',
    '["qualification-case-1"]',
    FALSE,
    '2026-01-01T00:00:03+00:00'
);

-- astralplane-fixture-statement
INSERT INTO workspace_layout (
    chat_id, user_id, layout_key, position, layout, created_at, updated_at
)
VALUES (
    'conversation-1',
    'fixture-owner-a',
    'synthetic-layout',
    3,
    '{"type":"stack","children":[]}',
    1040,
    1041
);

-- astralplane-fixture-statement
INSERT INTO scheduled_job (
    id,
    user_id,
    name,
    instruction,
    schedule_kind,
    schedule_expr,
    timezone,
    consented_scopes,
    delivery,
    status,
    created_at,
    updated_at
)
VALUES (
    '00000000-0000-4000-8000-000000000201',
    'fixture-owner-a',
    'Synthetic scheduled operation',
    'Emit a synthetic maintenance marker',
    'interval',
    '3600',
    'UTC',
    '["read"]'::jsonb,
    'in_app',
    'active',
    1050,
    1051
);

-- astralplane-fixture-statement
INSERT INTO remote_machine (
    machine_id,
    owner_user_id,
    label,
    address,
    port,
    username,
    os_family,
    role,
    created_at,
    updated_at
)
VALUES (
    'machine-1',
    'fixture-owner-a',
    'Synthetic documentation host',
    '192.0.2.10',
    22,
    'fixture-user',
    'linux',
    'plain',
    1060,
    1061
);

-- astralplane-fixture-statement
INSERT INTO voice_session (
    session_id,
    user_id,
    activation_id,
    device_id,
    device_kind,
    transport,
    room_name,
    participant_identity,
    visible_chat_id,
    state,
    generation,
    owner_connection_generation,
    control_binding_id,
    control_binding_expires_at,
    lease_expires_at,
    started_at,
    updated_at,
    ended_at,
    end_reason,
    media_grant_nonce_hash,
    media_grant_expires_at,
    media_grant_issued_at
)
VALUES (
    '00000000-0000-4000-8000-000000000301',
    'fixture-owner-a',
    '00000000-0000-4000-8000-000000000311',
    '00000000-0000-4000-8000-000000000312',
    'web',
    'livekit',
    'fixture-voice-room',
    'fixture-voice-participant',
    'conversation-1',
    'ended',
    1,
    '00000000-0000-4000-8000-000000000313',
    '00000000-0000-4000-8000-000000000314',
    '2026-01-01T01:00:00Z',
    '2026-01-01T01:00:00Z',
    '2026-01-01T00:00:00Z',
    '2026-01-01T00:01:00Z',
    '2026-01-01T00:01:00Z',
    'user',
    decode(repeat('44', 32), 'hex'),
    '2026-01-01T00:30:00Z',
    '2026-01-01T00:00:00Z'
);

-- astralplane-fixture-statement
INSERT INTO voice_turn (
    turn_id,
    client_turn_id,
    session_id,
    session_generation,
    media_grant_revision,
    user_id,
    chat_id,
    chat_context_revision,
    execution_base_render_revision,
    submission_id,
    request_generation,
    state,
    terminal_kind,
    terminal_at,
    created_at,
    updated_at
)
VALUES (
    '00000000-0000-4000-8000-000000000302',
    '00000000-0000-4000-8000-000000000321',
    '00000000-0000-4000-8000-000000000301',
    1,
    1,
    'fixture-owner-a',
    'conversation-1',
    1,
    0,
    '00000000-0000-4000-8000-000000000322',
    '00000000-0000-4000-8000-000000000323',
    'succeeded',
    'succeeded',
    '2026-01-01T00:00:30Z',
    '2026-01-01T00:00:00Z',
    '2026-01-01T00:00:30Z'
);

-- astralplane-fixture-statement
INSERT INTO audit_events (
    event_id,
    actor_user_id,
    auth_principal,
    agent_id,
    event_class,
    action_type,
    description,
    conversation_id,
    correlation_id,
    outcome,
    started_at,
    completed_at,
    recorded_at,
    prev_hash,
    entry_hash,
    key_id,
    schema_version
)
VALUES
    (
        '00000000-0000-4000-8000-000000000401',
        'fixture-owner-a',
        'fixture-principal-a',
        'fixture-agent',
        'agent_tool_call',
        'synthetic.read',
        'Synthetic audit event one',
        'conversation-1',
        '00000000-0000-4000-8000-000000000411',
        'success',
        '2026-01-01T00:00:00Z',
        '2026-01-01T00:00:01Z',
        '2026-01-01T00:00:02Z',
        decode(repeat('00', 32), 'hex'),
        decode(repeat('11', 32), 'hex'),
        'fixture-key',
        1
    ),
    (
        '00000000-0000-4000-8000-000000000402',
        'fixture-owner-a',
        'fixture-principal-a',
        'fixture-agent',
        'agent_tool_call',
        'synthetic.write',
        'Synthetic audit event two',
        'conversation-1',
        '00000000-0000-4000-8000-000000000412',
        'success',
        '2026-01-01T00:00:03Z',
        '2026-01-01T00:00:04Z',
        '2026-01-01T00:00:05Z',
        decode(repeat('11', 32), 'hex'),
        decode(repeat('22', 32), 'hex'),
        'fixture-key',
        1
    ),
    (
        '00000000-0000-4000-8000-000000000403',
        'fixture-owner-b',
        'fixture-principal-b',
        NULL,
        'auth',
        'synthetic.open',
        'Synthetic audit event three',
        'conversation-2',
        '00000000-0000-4000-8000-000000000413',
        'success',
        '2026-01-01T00:00:06Z',
        '2026-01-01T00:00:07Z',
        '2026-01-01T00:00:08Z',
        decode(repeat('00', 32), 'hex'),
        decode(repeat('33', 32), 'hex'),
        'fixture-key',
        1
    );
