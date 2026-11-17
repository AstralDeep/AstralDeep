"""Applies generated-publication transitions through explicit SQLite statements under one savepoint.
The owning repository validates authority first; these predicates preserve its PostgreSQL fences.
"""

from astralplane.repositories import RepositoryConflictError

_REBIND_RECOVERY_ATTEMPT_ELIGIBLE = """
SELECT publication.publication_id, draft.id AS draft_id
FROM draft_artifact_publication AS publication
JOIN draft_agents AS draft
  ON draft.draft_uuid = publication.draft_uuid
 AND draft.user_id = publication.owner_user_id
WHERE publication.publication_id = %s
  AND publication.owner_user_id = %s
  AND publication.state_revision = %s
  AND publication.state = %s
  AND publication.draft_uuid = %s
  AND publication.source_state_revision = %s
  AND publication.generation_claim_id = %s
  AND publication.target_agent_id = %s
  AND publication.target_revision_id = %s
  AND publication.staging_relative_path = %s
  AND publication.revision_relative_path = %s
  AND publication.operation_id IS NOT DISTINCT FROM %s
  AND publication.operation_execution_generation IS NOT DISTINCT FROM %s
  AND publication.artifact_digest IS NOT DISTINCT FROM %s
  AND publication.manifest_digest IS NOT DISTINCT FROM %s
  AND draft.state_revision = publication.source_state_revision
  AND draft.generation_claim_id = publication.generation_claim_id
  AND draft.target_agent_id = publication.target_agent_id
  AND draft.status = 'generating'
  AND draft.published_revision_id IS NULL
FOR UPDATE OF publication, draft
""".strip()

_MARK_VALIDATED_ELIGIBLE = """
SELECT publication.publication_id, draft.id AS draft_id
FROM draft_artifact_publication AS publication
JOIN draft_agents AS draft
  ON draft.draft_uuid = publication.draft_uuid
 AND draft.user_id = publication.owner_user_id
JOIN user_agent_revision AS revision
  ON revision.revision_id = publication.target_revision_id
 AND revision.agent_id = publication.target_agent_id
 AND revision.owner_user_id = publication.owner_user_id
WHERE publication.publication_id = %s
  AND publication.owner_user_id = %s
  AND publication.state_revision = %s
  AND publication.state = 'staged'
  AND publication.operation_id = %s
  AND publication.operation_execution_generation = %s
  AND draft.state_revision = publication.source_state_revision
  AND draft.generation_claim_id = publication.generation_claim_id
  AND draft.generation_claim_expires_at > clock_timestamp()
  AND draft.target_agent_id = publication.target_agent_id
  AND draft.status = 'generating'
  AND draft.published_revision_id IS NULL
  AND revision.state = 'prepared'
  AND revision.artifact_digest = %s
  AND revision.artifact_relative_path = publication.revision_relative_path
FOR UPDATE OF publication, draft, revision
""".strip()

_FAIL_ELIGIBLE = """
SELECT publication.publication_id, draft.id AS draft_id,
       revision.revision_id
FROM draft_artifact_publication AS publication
JOIN draft_agents AS draft
  ON draft.draft_uuid = publication.draft_uuid
 AND draft.user_id = publication.owner_user_id
JOIN user_agent_revision AS revision
  ON revision.revision_id = publication.target_revision_id
 AND revision.agent_id = publication.target_agent_id
 AND revision.owner_user_id = publication.owner_user_id
WHERE publication.publication_id = %s
  AND publication.owner_user_id = %s
  AND publication.state_revision = %s
  AND publication.state = %s
  AND publication.operation_id = %s
  AND publication.operation_execution_generation = %s
  AND draft.state_revision = publication.source_state_revision
  AND draft.generation_claim_id = publication.generation_claim_id
  AND draft.generation_claim_expires_at > clock_timestamp()
  AND draft.target_agent_id = publication.target_agent_id
  AND draft.status = 'generating'
  AND draft.published_revision_id IS NULL
  AND revision.state = 'prepared'
  AND revision.artifact_relative_path = publication.revision_relative_path
FOR UPDATE OF publication, draft, revision
""".strip()

_COMMIT_PUBLISHED_ELIGIBLE = """
SELECT publication.publication_id, draft.id AS draft_id
FROM draft_artifact_publication AS publication
JOIN draft_agents AS draft
  ON draft.draft_uuid = publication.draft_uuid
 AND draft.user_id = publication.owner_user_id
JOIN user_agent_revision AS revision
  ON revision.revision_id = publication.target_revision_id
 AND revision.agent_id = publication.target_agent_id
 AND revision.owner_user_id = publication.owner_user_id
WHERE publication.publication_id = %s
  AND publication.owner_user_id = %s
  AND publication.state_revision = %s
  AND publication.state = 'validated'
  AND publication.operation_id = %s
  AND publication.operation_execution_generation = %s
  AND publication.artifact_digest = %s
  AND publication.manifest_digest = %s
  AND draft.state_revision = publication.source_state_revision
  AND draft.generation_claim_id = publication.generation_claim_id
  AND draft.generation_claim_expires_at > clock_timestamp()
  AND draft.target_agent_id = publication.target_agent_id
  AND draft.status = 'generating'
  AND draft.published_revision_id IS NULL
  AND draft.error_message IS NOT DISTINCT FROM %s
  AND draft.security_report IS NOT DISTINCT FROM %s
  AND draft.validation_report IS NOT DISTINCT FROM %s
  AND draft.required_credentials IS NOT DISTINCT FROM %s
  AND revision.state = 'prepared'
  AND revision.artifact_digest = publication.artifact_digest
  AND revision.artifact_relative_path = publication.revision_relative_path
FOR UPDATE OF publication, draft, revision
""".strip()


def _required_row(tx, statement, parameters, reason):
    row = tx.fetch_one(statement, parameters)
    if row is None:
        raise RepositoryConflictError(reason)
    return row


def _write(tx, statement, parameters, reason):
    if tx.execute(statement, parameters).rowcount != 1:
        raise RepositoryConflictError(reason)


def _publish_row(tx, expected, changes, parameters, reason):
    return _required_row(
        tx,
        "UPDATE draft_artifact_publication SET " + changes + ",state_revision=state_revision+1 "
        "WHERE publication_id=%s AND owner_user_id=%s AND state_revision=%s AND state=%s "
        "RETURNING *",
        (
            *parameters,
            expected.publication_id,
            expected.owner_id,
            expected.state_revision,
            expected.state,
        ),
        reason,
    )


def rebind(tx, expected, attempt, lease_seconds):
    reason = "publication recovery fence is stale"
    with tx.savepoint("sqlite_generated_publication"):
        eligible = _required_row(
            tx,
            _REBIND_RECOVERY_ATTEMPT_ELIGIBLE,
            (
                expected.publication_id,
                expected.owner_id,
                expected.state_revision,
                expected.state,
                expected.draft_uuid,
                expected.source_state_revision,
                expected.generation_claim_id,
                expected.target_agent_id,
                expected.target_revision_id,
                expected.staging_relative_path,
                expected.revision_relative_path,
                expected.operation_id,
                expected.operation_execution_generation,
                expected.artifact_digest,
                expected.manifest_digest,
            ),
            reason,
        )
        _write(
            tx,
            "UPDATE draft_agents SET generation_claim_expires_at=GREATEST("
            "generation_claim_expires_at,clock_timestamp()+(%s*interval '1 second')),"
            "updated_at=(extract(epoch from clock_timestamp())*1000)::bigint "
            "WHERE id=%s AND user_id=%s",
            (lease_seconds, eligible["draft_id"], expected.owner_id),
            reason,
        )
        return _publish_row(
            tx,
            expected,
            "operation_id=%s,operation_execution_generation=%s",
            (str(attempt.operation_id), attempt.execution_generation),
            reason,
        )


def validate(tx, expected, attempt, artifact_digest, manifest_digest, result):
    reason = "publication validation fence is stale"
    with tx.savepoint("sqlite_generated_publication"):
        eligible = _required_row(
            tx,
            _MARK_VALIDATED_ELIGIBLE,
            (
                expected.publication_id,
                expected.owner_id,
                expected.state_revision,
                str(attempt.operation_id),
                attempt.execution_generation,
                artifact_digest,
            ),
            reason,
        )
        _write(
            tx,
            "UPDATE draft_agents SET error_message=%s,security_report=%s,"
            "validation_report=%s,required_credentials=%s,"
            "updated_at=(extract(epoch from clock_timestamp())*1000)::bigint "
            "WHERE id=%s AND user_id=%s",
            (
                result.error_message,
                result.security_report,
                result.validation_report,
                result.required_credentials,
                eligible["draft_id"],
                expected.owner_id,
            ),
            reason,
        )
        return _publish_row(
            tx,
            expected,
            "state='validated',artifact_digest=%s,manifest_digest=%s",
            (artifact_digest, manifest_digest),
            reason,
        )


def fail(tx, expected, attempt, failure_code, safe_error_message):
    reason = "publication failure fence is stale"
    with tx.savepoint("sqlite_generated_publication"):
        eligible = _required_row(
            tx,
            _FAIL_ELIGIBLE,
            (
                expected.publication_id,
                expected.owner_id,
                expected.state_revision,
                expected.state,
                str(attempt.operation_id),
                attempt.execution_generation,
            ),
            reason,
        )
        _write(
            tx,
            "UPDATE draft_agents SET generation_claim_id=NULL,generation_claim_expires_at=NULL,"
            "status='error',error_message=%s,state_revision=state_revision+1,"
            "updated_at=(extract(epoch from clock_timestamp())*1000)::bigint "
            "WHERE id=%s AND user_id=%s",
            (safe_error_message, eligible["draft_id"], expected.owner_id),
            reason,
        )
        _write(
            tx,
            "UPDATE user_agent_revision SET state='failed',failed_at=clock_timestamp(),"
            "failure_code=%s,state_revision=state_revision+1 "
            "WHERE revision_id=%s AND agent_id=%s AND owner_user_id=%s AND state='prepared'",
            (failure_code, eligible["revision_id"], expected.target_agent_id, expected.owner_id),
            reason,
        )
        return _publish_row(
            tx,
            expected,
            "state='failed',failed_at=clock_timestamp(),failure_code=%s",
            (failure_code,),
            reason,
        )


def publish(tx, expected, attempt, result):
    reason = "publication commit fence is stale"
    with tx.savepoint("sqlite_generated_publication"):
        eligible = _required_row(
            tx,
            _COMMIT_PUBLISHED_ELIGIBLE,
            (
                expected.publication_id,
                expected.owner_id,
                expected.state_revision,
                str(attempt.operation_id),
                attempt.execution_generation,
                expected.artifact_digest,
                expected.manifest_digest,
                result.error_message,
                result.security_report,
                result.validation_report,
                result.required_credentials,
            ),
            reason,
        )
        _write(
            tx,
            "UPDATE draft_agents SET generation_claim_id=NULL,generation_claim_expires_at=NULL,"
            "status='generated',published_revision_id=%s,state_revision=state_revision+1,"
            "updated_at=(extract(epoch from clock_timestamp())*1000)::bigint "
            "WHERE id=%s AND user_id=%s",
            (expected.target_revision_id, eligible["draft_id"], expected.owner_id),
            reason,
        )
        return _publish_row(
            tx, expected, "state='published',published_at=clock_timestamp()", (), reason
        )
