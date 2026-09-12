# Explicit restored-session retirement

This is the operator interface for the recovery requirement in
[session-incarnation.md](session-incarnation.md). It does not restore a database,
verify a backup, stop services, clear their caches, or reopen admission. It is not
part of normal startup. Its local receipt supplies no protected release authority.

Before invocation, the operator independently closes every admission path and
stops all writers, verifies the restored PostgreSQL database and every paired
durable root, and retains that recovery record outside the repository. After
retirement, discard all application processes and their session caches before
restarting the qualified composition. Require new institutional sign-in and
consent; retain old grant and operation references unchanged.

Use a POSIX host, the exact initialized Deep checkout, and its qualified installed
component wheels with the original wheel lock. The tool verifies component source
and Gitlink declarations, installed wheel provenance, file integrity and import
origins before importing the Plane recovery entry. It never installs or fetches
dependencies. A source checkout substituted for installed package data refuses.

Store the explicit PostgreSQL URL in an owner-only regular file with mode `0600`.
The tool never reads application environment variables for a connection. Provide
the expected database and schema separately; Plane verifies both against the
connected target before retirement. The following paths are placeholders:

```sh
python -m scripts.retire_restored_sessions \
  --root /operator/qualified/AstralDeep \
  --wheel-lock /operator/qualified/astral-component-wheels.lock.json \
  --database-url-file /operator/private/restored-database-url \
  --database restored_database \
  --schema public \
  --recovery-record /operator/records/verified-joint-recovery \
  --recovery-record-sha256 REPLACE_WITH_VERIFIED_RECORD_SHA256 \
  --output /operator/records/session-retirement-attempt.json \
  --execute
```

The recovery-record hash only binds the independently retained operator record.
The command does not certify its contents or infer closed writers from a flag.
The output must not exist, including as a symlink. Before touching the database,
the tool creates a private pending receipt and flushes the file and parent
directory. It calls only Plane's public `retire_restored_sessions` API, which owns
the pool, exact current schema and catalog validation, migration coordination,
bounded transaction, complete session deletion and final empty-table check.
There is no owner inventory that could omit an unknown or inactive owner.

Only a returned typed postcommit result permits a `commit_confirmed` receipt with
the retired count. No session identity, credential, URL, payload or private
exception text is copied into the receipt. The command exits zero only after
flushing that receipt. A pending or torn receipt, nonzero exit, interruption or
missing response is unconfirmed; it does not prove rollback. A final receipt
flush failure may follow a confirmed database commit. Keep admission closed and
investigate or repeat explicitly with a new output path. Never retry automatically
or restore a grant's authority from the absence of a confirmation.

Repeating the operation while writers remain stopped returns zero after a
previous committed retirement. Restoring the snapshot again requires retirement
again; neither an old receipt nor a restored reconciliation marker skips the
operation. Retirement preserves grants, assignments, issued permits, uncertain
liabilities, accounting, audit and blobs. It does not assert those liabilities
have been reconciled or that the wider restore is ready to reopen.

Local CLI tests use a synthetic public API and do not qualify PostgreSQL, IAM,
operator maintenance controls, paired backup verification or protected staging.
Those qualifications remain separate and must use the exact final Plane and
Deep artifacts before this procedure is treated as release-ready.
