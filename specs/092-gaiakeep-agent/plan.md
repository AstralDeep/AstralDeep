# Implementation Plan: GaiaKeep agent

**Branch**: `codex/092-gaiakeep-agent` | **Date**: 2026-10-02 | **Spec**: [spec.md](spec.md)

## Summary

A first-party Python agent exposes the pinned public GaiaKeep catalog and bounded file conveniences through normal Astral dispatch. It opens a per-call SSH tunnel using the caller's existing registered machine, then uses a TLS-verified native WebSocket transport and the pinned optional GaiaKeep SDK. No fabric, gateway, or Gaia instance is provisioned.

## Technical Context

- Language/version: Python 3.11.
- Dependencies: existing Paramiko, cryptography and astralprims; optional jsonschema >=4.21,<5 and websockets >=15,<17; private optional gaiakeep client at exact gfs revision 02f7d83cc92db50968d4076da8afad2cc7a811b7. Declare optional installation in backend/requirements-gaiakeep.txt; deterministic tests use tooling/requirements-gaiakeep-tests.txt and require no private SDK. Separate SDK qualification uses tooling/requirements-gaiakeep-sdk-tests.txt. Do not vendor private implementation.
- Storage: existing AstralPlane remote machines, encrypted agent credentials, and durable operation proposals; Gaia owns storage and native jobs. No schema/pin changes.
- Testing: Python pytest with coverage, deterministic TLS/WebSocket and injected SSH/SDK fixtures; root Ruff configuration.
- Target: Linux production backend. Test protocol/authorization on Python 3.11 locally; Linux SDK transfer qualification and full staging remain pending.
- Bounds: 8 MiB files; 1 MiB RPC wire payload/reply; 4 MiB decompressed parameter; 20-second control RPC; 10-second connection; 120-second file flow bound; no mutation replay. Values are deployment-independent safety bounds, not CI wall-clock performance assertions.
- Scope: 111 declarations (90 core, 21 prototype/common), 107 named control surfaces and two complete bounded file tools, for 109 tools. core.put/get/haveopen/have belong to the file workflows. Legacy fetch is explicitly unsupported because no qualified receiver exists; legal-order shortening is unsupported because of the upstream action-field collision. Other legacy operations require additional operator opt-in because their native auth differs from core signing.

## Constitution Check

- I/II/VI/XII: Python backend, approved Card/CodeBlock/Alert primitives, existing surfaces, short file headers; no component/client edits.
- III/IV/XI: meaningful deterministic tests, changed-code >=90%, root Ruff. Existing complete merge gates still apply; no network-dependent required test.
- V/XIV: declare optional exact upstream SDK pin; no private implementation vendoring, no component source/pin changes.
- VII: Keycloak/RFC8693 dispatch and audit preserved; FF_GAIAKEEP and FF_CRESCO both default off; native wsapi seam only. Runtime CRESCO_SERVICE_KEY stays in environment, user Gaia key is decrypted inside agent. Existing SSH owner isolation and host pins, verified TLS for control AND dataplane, bounded egress, conservative scopes and confirmation for all mutations.
- IX: all durable mechanics use existing Plane facades; no SQL or migrations.
- X: existing owner-operated DGX Gaia deployment plus sandbox is the intended real-auth staging topology. Owner says deployment pending; actual Gaia role/denial/file flows and every affected client must be verified against a candidate image before merge. Record exact candidate SHA/image, representative collection, host/gateway/core pins and nonsecret observations. No missing-check waiver is requested and no publication is authorized.
- XIII: protocol/source facts cite exact revisions and date; specified, locally test-passing, staging-verified, and deployed remain separate states.

## Project Structure

- backend/agents/gaiakeep/: gaiakeep_agent.py, mcp_server.py, catalog.py, capabilities.json, client.py, transport.py, tests/.
- backend/orchestrator/: local_agents.py registration and remote_confirmation.py policy extension; reuse remote_machines.py and remote_transport.py host checks through a public tunnel context.
- backend/shared/: feature_flags.py. backend/orchestrator/taint.py classifies Gaia output and mutation sinks; backend/start.py preserves both runtime placements. Gaia remains outside automatic safe-agent permission seeding, and standalone transport binds loopback.
- backend/requirements-gaiakeep.txt and tooling/requirements-gaiakeep-tests.txt.
- specs/092-gaiakeep-agent/: design, contracts, tasks and qualification handoff.

## Design

1. Reviewed capability metadata becomes a checked-in factual catalog. Each explicit named tool has a closed schema, conservative scope, and classification. Authentication/routing fields are not model inputs; model data cannot override protected identity.
2. Existing agent credential surface holds GAIAKEEP_PRINCIPAL and GAIAKEEP_PRIVATE_KEY. Operator config supplies the existing gateway port/TLS name/CA, trusted core public key, allowed core addresses, and runtime service key. Gaia roles are enforced by the actual core.
3. The owner-scoped remote-machine record supplies SSH address/port/user/credential. A public context manager in remote_transport reuses existing egress, peer-address, and pinned-host checks, refuses missing pin, and closes the client. A bounded socketpair forwarding worker adapts the SSH channel to the WebSocket client; no permanent listener or host service is created.
4. Native control RPC framing and dataplane stream headers are implemented with verified TLS. Only allowed core peers can receive credentials, including SDK redirects. Decompression and reply/flow sizes are bounded; credentials and auth artifacts are filtered before results. Disconnected mutations are unconfirmed/nonretryable.
5. All mutations reuse durable remote-operation approval. Classification is shared with the registry, covers aliases/batch/chained dispatch, rejects unattended work and ignores model-supplied confirmation claims.
6. Optional SDK loads lazily and executes signed CoreClient.call or bounded ingest/read/have_ingest. Exact dependency provenance is checked. Caller-supplied request_id is validated; a missing native request_id is generated once and retained in success/error reconciliation data. SDK signatures and transport correlation IDs remain code-owned. Upload may preserve a base version, and both upload strategies support expected_head; have does not support base_vid. Legacy actions require explicit opt-in; signed prototype user/approver fields derive from credentials. Unsupported fetch and legalorder shortening fail before dispatch.
8. Verify extraction certificate bytes/signature against the pinned core key and expected extract_id. Return exact public artifact bytes only when redaction leaves them intact; otherwise omit them and remove the verified claim. Leaf inclusion is explicitly not verified.
7. Backend tests exercise catalog coverage, full dispatch fixtures, flag-off behavior, owner isolation, protected inputs, auth/TLS/egress failure, jobs, compression/secret containment, uncertain effects, and transfer bounds. The absent real deployment blocks only live qualification, not local agent development.

## Complexity Tracking

The SSH socket adapter is necessary because Paramiko channels are not OS sockets accepted by WebSocket TLS clients. The SDK avoids reimplementing Gaia's signing, encrypted transfer, integrity and leader logic; the custom transport avoids its documented unverified dataplane path.
