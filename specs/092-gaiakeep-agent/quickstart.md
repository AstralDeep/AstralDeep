# GaiaKeep operator guide

PR #226 merged the default-disabled agent at a13c977e6b22c0ce8f1f4980de5e483d15dd9447. The production continuation is codex/092-gaiakeep-production; verification.md records exact-candidate staging and affected-client qualification separately. No Cresco service or Gaia instance is provisioned.

## Account and trust configuration

Run the deployment owner's Gaia client setup under each enrolled DGX login account. The qualified installation is ~/.gaiakeep/venv/bin/python with ~/.gaiakeep/gaiakeep-profile.json and its account-owned P-384 key. Profile/key files must be owner-only regular files. The remote SDK is the exact 0.2.0 wheel and source/resource digests in backend/agents/gaiakeep/sdk-artifact.json. The ephemeral adapter snapshots verified SDK bytes, ignores mutable bytecode and native speedups, and uses supported pure-Python chunking. It does not install a service on the login node.

Register your DGX account in Astral's Remote machines surface using the existing SSH endpoint. Probe and pin its host key. SSH credentials remain encrypted in Astral; Gaia's private signing key and gateway service key remain on the DGX. The default SSH mode needs no Gaia key entry in agent credentials. Grant individual tools explicitly; Gaia remains excluded from automatic safe-agent permission seeding.

Sandbox's existing 128.163.202.61:40000 listener reaches the DGX login node. The backend must reach that public address from its container. Its narrow bridge/source-IP/firewall allowance must follow a container address or network replacement. The container's localhost does not reach this listener; SSH egress policy remains enforced.

Supply runtime configuration through private files and certificate mounts, never committed values:

| Variable | Requirement |
|---|---|
| FF_GAIAKEEP, FF_CRESCO | Both true for opt-in registration; both default false. Cresco flag gates the existing native gateway seam. |
| GAIAKEEP_CONNECTION_MODE | Default ssh: run the reviewed adapter in the caller's pinned login account. |
| GAIAKEEP_CORE_ADDRESS | Existing core region:agent:plugin triple. |
| GAIAKEEP_ALLOWED_PEERS | Up to 16 admitted triples including core and qualified read peers. |
| GAIAKEEP_GATEWAY_PORT | Existing login-node loopback gateway port; SSH mode default 28282. |
| GAIAKEEP_GATEWAY_TLS_NAME | Out-of-band verified certificate hostname. |
| GAIAKEEP_GATEWAY_CA_FILE | Backend-readable mounted PEM trust anchor; only public bytes go to the adapter. |
| GAIAKEEP_CORE_PUBLIC_KEY | Out-of-band verified base64url P-384 SPKI. |
| GAIAKEEP_ALLOW_LEGACY | Optional true, default false; requires separately qualified legacy deployment and system permission. |
| GAIAKEEP_ISSUE_LOG_DIRECTORY | Operator-owned absolute directory for automatic redacted Markdown failures; Compose mounts `/app/gaia-issue-log`. Never expose a directory containing credentials or user uploads. |
| GAIAKEEP_ISSUE_LOG_HOST_DIRECTORY | Compose-only host directory for the dedicated log mount; default `./backend/logs/gaiakeep`. |
| GAIAKEEP_RESTORE_ROOT | Legacy restore only: operator-controlled non-root absolute POSIX directory. |

The adapter dials only login-node loopback at the operator port. It compares profile core identity, port and peers against deployment trust and overrides the stock development transport. Certificate/hostname checks apply to both control and encrypted data channels. Profile verify_ssl=false and unverified_dataplane=true cannot disable verification. Model arguments cannot select identity, signing keys, endpoints or profile paths.

The production image already contains Astral's declared Paramiko, cryptography, jsonschema and websockets dependencies. Remote SDK installation is separate. Optional native compatibility mode retains the exact private SDK in backend/requirements-gaiakeep.txt, encrypted GAIAKEEP_PRINCIPAL/GAIAKEEP_PRIVATE_KEY, runtime CRESCO_SERVICE_KEY and all trust fields; its default gateway port is 8282. It is not the sandbox production mode. Do not install an SDK into a running container as a deployment substitute.

## Bounds and outcomes

The catalog retains 111 declarations, 107 control tools, two bounded file workflows and two SSH account-discovery tools. Files are at most 8 MiB decoded; encryption/key exchange remain SDK-owned. Upload publishes one relative file and moves the selected branch head. Use base_vid to retain other files and expected_head for concurrency. Ingest accepts optional request_id matching 16–64 ASCII letters/digits/underscore/hyphen; its ID survives success or uncertainty for explicit reconciliation. Have-upload accepts neither base_vid nor request_id.

Every mutation requires normal owner-bound, exact-argument, single-use human approval. Uncertain writes are never replayed. Native jobs/IDs are retained without claiming completion. Ordinary RPC/results are bounded to 1 MiB, aggregate gzip expansion to 4 MiB, diagnostics to 16 KiB and the whole SSH operation to 120 seconds. File results use a separate 13 MiB wire limit. Request bytes use stdin rather than process arguments; raw remote diagnostics are never returned.

Legacy fetch remains unsupported until T025's authenticated receiver is qualified. Legal-order shortening remains unsupported due to the upstream signing/routing collision. Extraction verifies exact certificate bytes/signature and extract ID against the pinned key, including the current SDK public-key field; Merkle leaf inclusion is not claimed. The prototype may wipe data and must not receive PHI or regulated content.

Approved mutation results are published directly through the normal owner-scoped workspace path before optional model continuation. SDK commit timeouts remain unconfirmed and never trigger another commit; reconcile using the retained request/upload/version identity before deciding on any new operation. Read peer fallback and authoritative permission/TLS/protocol denials retain their existing behavior.
Eligible reads may reconnect once through the pinned SDK when a verified RPC connection times out before sending the request. This stays inside the original deadline; a receive/native timeout or an expired deadline does not gain that reconnect. Mutations and data streams retain their existing behavior.

## Qualification and rollout

Use Python 3.11, AstralDeep's exact composition and tooling/requirements-gaiakeep-tests.txt. Deterministic Gaia tests need no private SDK or live third-party network:

```text
python -m pytest -c backend/pytest.ini backend/agents/gaiakeep/tests backend/tests/test_gaiakeep_confirmation.py backend/tests/test_gaiakeep_tunnel.py backend/tests/test_remote_confirmation_063.py backend/tests/test_remote_coverage_misc_063.py backend/tests/test_remote_taint_063.py backend/tests/test_remote_orchestrator_wiring_063.py backend/tests/test_start_wait.py -q
ruff check .
git diff --check
```

Before production activation, stage the exact clean candidate image using real Keycloak, Plane-imported representative data, normal migrations, workers and isolated mounts/networks. Use the supported backend-web initializer and qualification driver. Verify authenticated dispatch, permission/cross-owner denial, exact approval/decline/replay, approved bounded publication/readback, job reconciliation, failed SSH/TLS/core pins and disconnected-write uncertainty. Exercise credential/approval/results on every affected client; retain candidate-bound evidence. Local/source smoke alone is not staging qualification.

After qualified merge and green CI, deploy the immutable sha-<merge SHA> image and recreate only the backend with reviewed flags/trust mounts. Preserve companion services, data, IAM/audit secrets and prior image/config backups. Disable with FF_GAIAKEEP=false and backend recreation; restart alone does not reread Compose environment or copy source.

The agent automatically appends failures to `ISSUES.md` in its configured operator directory and emits the same safe diagnostic labels in normal logs. Entries exclude identities, arguments, paths, payloads and exception text. Bounded rotation keeps three backups. A sink failure is visible in normal logs and does not change the primary Gaia outcome or retry a request.

The sandbox rollout will precreate the dedicated host directory `/home/sam/gaia-integration/agent-issues` as Sam UID/GID 1003, mode 2750. Its setgid bit makes root backend-created 0640 files inherit Sam's group, so Sam can read `ISSUES.md`; verify actual ownership/mode and readability after recreation. Mount only this directory. The earlier `/home/sam/gaia-integration/ISSUES.md` contains historical operator qualification notes. Durable product records and authorization/audit provenance remain in AstralPlane. Local log directories are excluded from Git and image build contexts.

SSH account discovery uses gaiakeep_connection_info for the enrolled principal/tenant/default collection and gaiakeep_list_collections for SDK-known collections and heads. Neither accepts profile paths or credentials. These are distinct from mutating core.profile policy operations; native compatibility refuses the local discovery tools.
