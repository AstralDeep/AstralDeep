# GaiaKeep agent handoff

The implementation is local on `codex/092-gaiakeep-agent`; it has not been pushed or deployed. [Local verification](verification.md) records checks and remaining qualification. No Cresco agent, tunnel service or independent Gaia instance was provisioned.

## Local checks

Use Python 3.11 with AstralDeep's normal dependencies and exact composed component wheels installed. Install `tooling/requirements-gaiakeep-tests.txt` in an isolated environment. Deterministic tests require neither the private Gaia SDK nor live services. From the root with `backend` on `PYTHONPATH`:

```text
python -m pytest -c backend/pytest.ini backend/agents/gaiakeep/tests backend/tests/test_gaiakeep_confirmation.py backend/tests/test_gaiakeep_tunnel.py backend/tests/test_remote_confirmation_063.py backend/tests/test_remote_coverage_misc_063.py backend/tests/test_remote_taint_063.py backend/tests/test_remote_orchestrator_wiring_063.py backend/tests/test_start_wait.py -q
ruff check .
git diff --check
```

The modules runner discovers `backend/agents/gaiakeep/tests` automatically. Optional SDK qualification is separate: install `tooling/requirements-gaiakeep-sdk-tests.txt` using existing authorized GitHub access, then run `python -m pytest -c backend/pytest.ini tooling/gaiakeep -q`. Installed SDK `direct_url.json` must record the exact pinned commit; unqualified index packages/wheels without that provenance are refused. Private implementation stays outside the product tree.

## Configure the existing deployment when ready

Build an agent-capable candidate image with `backend/requirements-gaiakeep.txt` installed before enabling the flags. This optional manifest declares the exact private SDK pin, `websockets>=15,<17` and `jsonschema>=4.21,<5`. Supply configuration through normal deployment secret/configuration paths, never committed values:

| Variable | Required value |
|---|---|
| `FF_GAIAKEEP`, `FF_CRESCO` | Both true for opt-in registration, default false. Cresco flag controls access to the existing native gateway only. |
| `GAIAKEEP_CORE_ADDRESS` | Existing core region:agent:plugin triple. |
| `GAIAKEEP_ALLOWED_PEERS` | Comma-separated admitted triples, up to 16, including the core. |
| `GAIAKEEP_GATEWAY_PORT` | Existing remote loopback gateway port, default 8282. |
| `GAIAKEEP_GATEWAY_TLS_NAME` | Verified hostname from the gateway certificate. |
| `GAIAKEEP_GATEWAY_CA_FILE` | Readable trusted PEM CA/certificate file. |
| `GAIAKEEP_CORE_PUBLIC_KEY` | Out-of-band verified base64url SPKI core identity key. |
| `CRESCO_SERVICE_KEY` | Runtime-only existing gateway service key. |
| `GAIAKEEP_ALLOW_LEGACY` | Optional true, default false; legacy system tools also need explicit permission. |
| `GAIAKEEP_RESTORE_ROOT` | For legacy restore: administrator-controlled absolute POSIX directory other than `/`. User destination remains relative beneath it. |

Register a caller-owned DGX SSH machine in Remote machines and probe/pin its existing host key. The endpoint must be reachable from the backend's network namespace and pass existing SSH/HTTP egress gates. Discovery verified sandbox `128.163.202.61` → `localhost:40000` → login node; it did not establish public or container reachability of port 40000. The sandbox host's localhost is not the backend container's localhost. Existing SSH policy rejects loopback targets; this feature adds no bypass. Qualify the existing endpoint mapping before connection.

In agent credentials, each user supplies their enrolled `GAIAKEEP_PRINCIPAL` and `GAIAKEEP_PRIVATE_KEY`. For the password-style field, use single-line base64 of an unencrypted ECDSA P-384 PEM key; raw PEM is also accepted by the agent. Optional `GAIAKEEP_RECONSTRUCTION_TOKEN` is for applicable legacy restore. SSH auth and Gaia identity/roles are separate. Grant tools explicitly; Gaia is excluded from automatic safe-agent seeding. Models cannot supply identity, signatures, stream selectors or tokens.

## Semantics and limits

The 111 declarations have 107 control surfaces plus two complete bounded file tools. Four encrypted transfer primitives belong to SDK upload/read workflows. Upload creates one new version and moves the branch head; `base_vid` preserves other files and `expected_head` guards concurrency. Both strategies support a head guard; have does not support `base_vid`. Files: 8 MiB decoded, 120-second flow. RPCs: 1 MiB; aggregate gzip expansion: 4 MiB. Ordinary ingest/control request IDs survive uncertain results for reconciliation. Do not blindly retry unconfirmed writes.

Legacy `fetch` is explicitly unsupported until its bounded authenticated receiver is implemented/qualified (T025); the pinned SDK contains no supported receiver. Legal-order shortening is unsupported because of the upstream action-field collision. Extraction verifies the signed certificate, not Merkle leaf inclusion. Redaction-changed certificates are omitted and lose the verified claim.

## Pending qualification and disablement

When the owner reports the interface ready, record trust configuration and qualify an exact candidate SHA/image with representative nonsecret collection data. Test actual whoami/status, permitted reads, insufficient-role denial, cross-user refusal, approval decline/expiry/replay, an approved mutation and job reconciliation, an encrypted file round trip on Linux, failed host/TLS/core pins, and disconnect cleanup. Exercise credentials, approval and results on every affected client. Complete merge/release gates remain required; local tests do not waive them. SDK `pread`/`pwrite` transfers still need Linux verification.

Disable by setting `FF_GAIAKEEP=false` and recreating the candidate container. Restart alone does not install ordinary source changes or reread Compose environment. Neither placement registers Gaia with flags off, and invocation checks again. Existing encrypted credentials and audit retain their normal lifecycle.
