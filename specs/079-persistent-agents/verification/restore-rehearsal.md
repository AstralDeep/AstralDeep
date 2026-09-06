# Paired backup restore rehearsal

Completed locally on 2026-09-05. This verifies the original pre-cutover pair, independently of application deployment or owner live079 evidence.

- Original backup: `Y:/WORK/MCP/AstralDeep-079-precutover-20260905`, unchanged. Paired manifest SHA256 `58862c5fbcf7178329eb6b46c4ec324f0285e83273267d670360eebccd826100`.
- Private restored roots and operator record: `Y:/WORK/MCP/AstralDeep-079-restore-rehearsal-20260905T222045Z-174fb8f5`. The output directory was atomically created with a protected current-user-only NTFS ACL before any private bytes were written; descendants inherit only that access.
- PostgreSQL container `astraldeep-079-restore-20260905t222045z-174fb8f5`; private Docker data volume `astraldeep-079-restore-20260905t222045z-174fb8f5-pgdata`. Retained stopped. Network `none`, no published ports, original dump mounted read-only. No production credentials copied and no network egress.
- Exact locally available PostgreSQL image `sha256:1bea307dfb3ee30541a7acf7de14b58bcd6948da98e5d31a04c627c4d35ec64b` matches the paired backup image identity. No image was pulled or published.

All five root archives and the database dump matched their original manifest digests before restoration. Nonfollowing two-pass archive validation rejected unsafe paths, links, special files and Windows aliases before extraction. Full restored root manifests match: **1,335 files, 7,669,473 bytes** across agents, data, knowledge, personal-agent-artifacts and tmp.

Full native `pg_restore --exit-on-error` succeeded into the newly initialized isolated database, preserving original object ownership and ACLs. Offline COPY-data comparison verifies exact row multisets for **83 tables / 2,570 rows**, including duplicates, and all **16 sequence values**. All **467 catalog object/owner entries** match. Full schema SQL is equivalent: after removing dump comments/blank lines/generated restriction nonces, only the exact redundant `(A AND B) AND C` prefix grouping in existing `voice_session_identity_075_check` and `voice_turn_identity_065_check` required normalization for PostgreSQL17 deparsing; every remaining statement, owner and ACL is identical. Raw schema exports/difference remain private.

Restored attachment metadata matches the restored tmp manifest and original parity: zero ready/live attachments, zero pending, two deleted records preserved, 181 unreferenced snapshot files preserved. No user rows, filenames or raw database exports are included in this public diagnostic record.

Restored schema remains **075.001**, migration digest `755faecd45a7d8ca9956f25a239bed476802b885efdce29a36dc3b66981f94df`. Read-only isolated catalog checks confirmed zero large objects. The full original backup and the restored database/root pair remain available; nothing was deleted or overwritten.

Limits: this proves offline paired PostgreSQL and blob-root restoration. It does not claim application startup, IAM/provider credentials, network integrations, native clients, or post-restore079 guarded migration recovery. No production application/container/database was changed by this rehearsal. The optional079 migration was not attempted.

Rehearsal scripts are ignored local operator artifacts: `rehearse_paired_backup.py` and `verify_restored_schema.py`. Exact private aggregate result: `operator-recovery-record.json` in the restored directory.
