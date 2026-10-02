# Data model

No new persistent tables, migrations, schema revisions or composition pins.

- Connection: existing RemoteMachineRecord and encrypted MachineCredentialRecord, selected by verified user_id and machine_id. Existing host_key_fingerprint is required before unattended connection. Operator gateway port is1..65535; allowed core peers are nonempty region:agent:plugin triples.
- Identity: existing encrypted agent credential entries GAIAKEEP_PRINCIPAL (nonempty, <=256 characters) and GAIAKEEP_PRIVATE_KEY (P-384 PEM or its single-line base64 encoding, <=16KiB input). An optional reconstruction token remains in encrypted credentials. Operator pinned core public key and TLS CA are distinct from user credentials.
- Catalog operation: immutable source revision, action, explicit public params and required names, conservative scope, read/mutation classification, tool name. Additional properties and auth/routing overrides are refused. RPC request/reply <=1MiB; decompressed field <=4MiB.
- Proposal: existing RemoteOperationProposalRecord, 900-second expiry, caller/agent/tool/machine/exact-argument fingerprint, single-use atomic consumption. State transitions and audit are owned by existing confirmation service.
- Native job: bounded Gaia reply with native job identifier/state. Native request_id is retained in structured reconciliation data and write error messages when present; it is not invented as a completed job identifier. Astral does not duplicate the Gaia job database; job/status/cancel tools operate on actual native identifiers.
- Transfer: one normalized relative collection path; <=8MiB decoded content, <=120-second flow; only agent-owned temporary paths. Cleanup follows success and every failure. No arbitrary local filesystem path is exposed.
