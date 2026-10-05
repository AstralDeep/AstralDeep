# GaiaKeep operator guide

Current archival workflow and SDK update instructions are in the [2026-10-04 continuation](#upstream-and-local-dataset-continuation-2026-10-04). Earlier qualification observations remain historical.

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

Successful Gaia results and issued approval cards are published through the normal owner-scoped conversation workspace and survive ordinary conversation hydration. Approval cards keep a stable identity so approve/decline replaces the issued card. Successful approved results use a separate owner-scoped conversation commit before optional model continuation; a publication failure never repeats the completed Gaia mutation. Permission refusals, failed tool results and unrelated agents retain their existing delivery behavior. SDK commit timeouts remain unconfirmed and never trigger another commit; reconcile using the retained request/upload/version identity before deciding on any new operation. Read peer fallback and authoritative permission/TLS/protocol denials retain their existing behavior.
Control and data connections may make at most two opening attempts for a recognized transient network failure before sending any frame. Each attempt retains the existing ten-second opening cap and the original operation deadline. Cleanup must finish successfully before another opening attempt; TLS, authorization, protocol, unknown close-code and cleanup failures stop immediately. Exhausted control openings do not gain an additional SDK reconnect or commit attempt. Requests, stream registrations and activation are never resent by this recovery path.

## Qualification and rollout

Use Python 3.11, AstralDeep's exact composition and tooling/requirements-gaiakeep-tests.txt. Deterministic Gaia tests need no private SDK or live third-party network:

```text
python -m pytest -c backend/pytest.ini backend/agents/gaiakeep/tests backend/tests/test_gaiakeep_confirmation.py backend/tests/test_gaiakeep_tunnel.py backend/tests/test_remote_confirmation_063.py backend/tests/test_remote_coverage_misc_063.py backend/tests/test_remote_taint_063.py backend/tests/test_remote_orchestrator_wiring_063.py backend/tests/test_start_wait.py -q
ruff check .
git diff --check
```

Before production activation, stage the exact clean candidate image using real Keycloak, Plane-imported representative data, normal migrations, workers and isolated mounts/networks. Use the supported backend-web initializer and qualification driver. Verify authenticated dispatch, permission/cross-owner denial, exact approval/decline/replay, approved bounded publication/readback, job reconciliation, failed SSH/TLS/core pins and disconnected-write uncertainty. Exercise credential/approval/results on every affected client; retain candidate-bound evidence. Local/source smoke alone is not staging qualification.

After qualified merge and green CI, deploy the immutable sha-<merge SHA> image and recreate only the backend with reviewed flags/trust mounts. Preserve companion services, data, IAM/audit secrets and prior image/config backups. Disable with FF_GAIAKEEP=false and backend recreation; restart alone does not reread Compose environment or copy source.

The agent automatically appends failures to `ISSUES.md` in its configured operator directory and emits the same safe diagnostic labels in normal logs. Entries exclude identities, arguments, paths, payloads and exception text. When available, `native_phase`, `failure_kind` and `native_status` identify the failure boundary using fixed labels and a numeric status or `none`. Missing or malformed optional detail leaves the original entry intact. Bounded rotation keeps three backups. A sink failure is visible in normal logs and does not change the primary Gaia outcome or retry a request.

The sandbox rollout will precreate the dedicated host directory `/home/sam/gaia-integration/agent-issues` as Sam UID/GID 1003, mode 2750. Its setgid bit makes root backend-created 0640 files inherit Sam's group, so Sam can read `ISSUES.md`; verify actual ownership/mode and readability after recreation. Mount only this directory. The earlier `/home/sam/gaia-integration/ISSUES.md` contains historical operator qualification notes. Durable product records and authorization/audit provenance remain in AstralPlane. Local log directories are excluded from Git and image build contexts.

SSH account discovery uses gaiakeep_connection_info for the enrolled principal/tenant/default collection and gaiakeep_list_collections for SDK-known collections and heads. Neither accepts profile paths or credentials. These are distinct from mutating core.profile policy operations; native compatibility refuses the local discovery tools.
## Upstream and local dataset continuation, 2026-10-04

The current integration targets GaiaKeep/gfs `f86ec85d5526d3273150725d79eee1a389237e37`, docs `51d41bc6af8b6e57f714448ff58baa155ab48a23` and dashboard `6101b5526bb7c59fefb6308fc4f4de60d4e1a17d`. The gfs client still identifies itself as 0.2.0, so version alone cannot qualify an installation. The exact pure wheel SHA256 is `89a4565b61a42dd7adcef578958027d60eae85a28bceb2ac65f52c63b6958065`; `sdk-artifact.json` also binds all 24 source/resource files. The catalog retains 115 native declarations and exposes 118 tools: 111 control tools, two bounded inline file workflows, two account-discovery reads and three local dataset workflows.

Update the existing enrolled login account before changing the backend SDK lock. Retain its previous exact wheel and environment inventory for rollback, copy the reviewed wheel to an account-owned directory, independently check its SHA256, and run the existing interpreter's supported pip operation:

```sh
"$HOME/.gaiakeep/venv/bin/python" -m pip install --no-deps --force-reinstall /operator/verified/gaiakeep-0.2.0-py3-none-any.whl
```

The wheel path above is operator supplied; it is never an agent argument. Keep the existing Gaia profile, private signing key, service key, gateway certificate and Astral machine registration. Verify `direct_url.json` contains the admitted archive SHA256 and verify every installed source digest before switching the backend image. The remote adapter independently repeats these checks before importing snapshotted source. Rollback requires the prior image and its matching prior wheel together. Installing or updating the account client is an operator deployment action; the agent does not self-install packages or provision Cresco services.

An operator or existing job runner can place completed run outputs under `~/.gaiakeep/astral-workspace/<dataset_ref>` in the enrolled account. `.gaiakeep` and `astral-workspace` must be owned by that account with mode 0700; dataset directories/files must be account owned and protected from group/other writers. Dataset references contain at most 64 ASCII letters, digits, underscores or hyphens and begin with a letter or digit. Symlinks, hard links, special files, absolute paths and traversal are refused. No tool accepts an upstream local path or lets the model choose a different workspace root.

For example, after a job creates `run42/metrics.json` and `run42/model.bin`, use the ordinary agent tools in this order:

1. Read `gaiakeep_connection_info` and one page of `gaiakeep_list_collections`. Tenant collection discovery calls native `core.collections`; pass its `next` cursor back as `after`. `core.branches` identifies the intended branch head.
2. Call `gaiakeep_inspect_dataset` with the owned machine ID and `dataset_ref="run42"`. Its sorted manifest contains relative file keys, byte counts and SHA256 fingerprints. The manifest digest binds every key, size and file digest.
3. Propose `gaiakeep_upload_dataset` with the same machine/reference, returned `manifest_sha256`, collection, selected branch and `expected_head`. Use `prefix="runs/42"` and a short `note` to describe results. If `base_vid` is omitted, it defaults to `expected_head`, retaining existing dataset files and adding the completed run in one native version. A server-generated native request ID is bound before approval. Approval is exact argument, owner bound and single use.
4. At execution, the adapter reopens confined descriptors, rehashes every source, compares the approved digest and creates private unlinked snapshots before native ingest. A file changed since approval is refused before upload. Keep the returned request/upload/version/job IDs for reconciliation.
5. To retrieve a fixed version locally, propose `gaiakeep_download_dataset` with an immutable `vid` and a fresh dataset reference. This writes local files and therefore requires the same mutation approval. It never replaces an existing reference. Each whole-file SDK transfer and additional signed digest proof must pass; a complete manifest is checked before and after atomic no-replace directory publication. Failed transfers clean up unpublished staging.

Local dataset workflows require Linux SSH mode and are bounded to 256 files, 1 GiB total, 1,024 directory entries, 16 nested directories and the existing 120-second operation deadline. Larger datasets need a separate qualified transfer scope. Inline file workflows retain their 8 MiB decoded limit. Version notes are at most 512 UTF-8 bytes and contain no control characters. These bounds apply to remote version listings before any download directory or stream is created.

Native publication may return `commit_job` while tape copies are still being verified. That result is `pending`, with the job/version identity preserved, and does not claim durable completion. Poll `gaiakeep_core_job` using that job ID; status 16 reads remain pending and must not trigger another publication. `gaiakeep_core_jobs` provides bounded principal-scoped job discovery. Both SSH and native compatibility transports retain canonical publication metadata even though the upstream ingest result currently drops it. An invalid job token yields an unconfirmed result instead of false success.

Results render as existing Astral cards, status badges, key/value fields and typed tables. Encoded JSON is decoded for display; identifiers, dataset paths and notes remain literal. Boolean and numeric table cells retain their types. Tables preview at most 50 returned rows and eight columns, with explicit notices for shortened or omitted detail. Credentials, file bytes and opaque proof artifacts are excluded from this view; the canonical result and reconciliation remain available unchanged.

The inherited legacy `getcapabilities` surface remains conservatively approval gated and requires operator legacy opt-in; a confirmation card is an outstanding approval, not a native outage. Use the pinned tool catalog and `core.status` for supported modern discovery. Semantic cross-agent relations, panAtlas operational data and UofL resource provisioning remain future work. This continuation implements archival/retrieval and versioned result augmentation.

The final parsed-presentation and approval snapshot passed 1,360 tests with zero skips in a disposable production Python 3.11.17 container on an internal network containing only throwaway PostgreSQL 17. It includes actual SDK raw/keyed proof, atomic destination, owner-scoped approval/publication and real primitive-renderer fixtures. Reproduce its scoped invocation from the candidate root with the owning repository's declared application dependencies, coverage tooling and exact current SDK wheel installed:

```sh
PYTHONPATH="$PWD/backend" python -m pytest -c backend/pytest.ini backend/agents/gaiakeep/tests backend/tests/test_gaiakeep_confirmation.py backend/tests/test_gaiakeep_tunnel.py backend/tests/test_gaiakeep_outer_dispatch.py backend/tests/test_remote_confirmation_063.py backend/tests/test_gaiakeep_ui_publication.py backend/tests/test_connection_publication_owner.py backend/tests/test_tool_feedback.py backend/tests/test_authorize_and_prepare.py backend/tests/test_computer_use_076.py -q
```

When candidate files are mounted read only, direct `COVERAGE_FILE` and report destinations to a separate evidence mount and use Ruff `--no-cache`. See the dated verification record for exact source/evidence digests and changed-line coverage. This deterministic run does not substitute for ordinary authenticated live transfer and approval acceptance after the matched account SDK/backend update.

S1 now runs the qualified backend with the matched account SDK. Ordinary signed-in browser inspection renders the actual manifest as primitive cards and tables; an upload proposal survives reload, and declining it persists across another reload with an owner-bound audit record. Full approved upload/download and durability-job acceptance remain blocked by the upstream core's non-serving state in [gfs issue 13](https://github.com/GaiaKeep/gfs/issues/13). This sandbox activation does not qualify a release or complete production acceptance.
