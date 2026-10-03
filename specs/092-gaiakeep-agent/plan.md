# Implementation Plan: GaiaKeep agent

**Branch**: `codex/092-gaiakeep-production` | **Updated**: 2026-10-03 | **Spec**: [spec.md](spec.md)

## Summary

A first-party Python agent exposes the pinned GaiaKeep catalog and bounded file conveniences through normal Astral dispatch. Default SSH mode executes an ephemeral reviewed adapter under the caller's pinned registered account. The private signing profile/key stay on DGX; verified TLS overrides the upstream development transport. Native backend compatibility remains optional. No fabric, gateway, or Gaia instance is provisioned.

## Production continuation decision

The owner reported the enrolled client on 2026-10-03 and authorized checks, PR, merge and sandbox deployment. Default SSH mode verifies the exact remote 0.2.0 wheel/file lock in `backend/agents/gaiakeep/sdk-artifact.json`, snapshots source/resources and compiles verified bytes without mutable bytecode or unpinned native speedups. The product image needs no private SDK dependency for this mode. Existing declared Paramiko, cryptography, jsonschema and websockets suffice. Native mode retains the original exact private 0.1.0 dependency/provenance below.

The command contains only reviewed first-party bootstrap and its source digest; requests/trust/file bytes use bounded stdin. The entire pinned SSH connection/send/drain/exit has one 120-second deadline. Diagnostics are bounded and discarded. An operator-trusted public CA/core pin/peer set is compared with the private profile; routing remains the login node's loopback at the operator port. Private service/signing keys never return to sandbox. Results have a separate 13 MiB file-wire bound, retain the existing 1 MiB ordinary response bound, and are rechecked for file identity/count/digest. Ingest request IDs can be supplied for explicit reconciliation and remain covered by exact approval arguments. Uncertain mutations are never replayed.

T023/T024 remain gates before activation. Use isolated candidate-image staging with real IAM, Plane-imported representative data, normal migrations, workers and ordinary authenticated owner/approval flows. The earlier owner waiver applied to the disabled initial merge only; on 2026-10-03 the owner separately approved skipping unavailable Apple client checks for this merge. Other outstanding checks remain open. No schema, component, primitive or client protocol changes are introduced. The retained design below describes the native compatibility path; the SSH continuation uses the same catalog, verified wire protocol and permission/approval/taint/audit seams.

Real staging exposed a pre-existing ordinary UI dispatch failure: connection-owned operations have no user owner, but conversation publication used that field and selected `legacy` instead of the authenticated caller. Capture only the verified socket subject at ingress, require the same current subject before publication and component inference, and retain the original connection-owned operation/fence. Missing, changed and foreign identities must fail before publication or tool dispatch. Qualify the repair against actual Plane publication and execution fences, then collect fresh candidate-image Gaia and client evidence; earlier candidate observations remain historical.

Hosted run 37106771102 verified actual owner publication but found one incorrect new test assertion: completed publication deliberately retires the execution fence. Verify the unchanged connection owner and live fence during execution, then require completed state and stale-fence denial after publication. Retain that failed run separately and qualify the corrected test against real PostgreSQL before the next push.

The corrected test bytes were independently verified against all 15 runtime source files in the 6d68a07f production image with isolated PostgreSQL: 19 focused cases passed with no skips, including the existing stale-base/fence regression. The first fixture failed collection because its temporary mount disallowed loading an executable shared library; that attempt remains retained. The corrected fixture preserved every pre-existing container, network and volume and removed its own temporary resources. This verifies the test correction; the next pushed candidate still requires its own hosted CI and live staging acceptance.

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
- VII: Keycloak/RFC8693 dispatch and audit preserved; FF_GAIAKEEP and FF_CRESCO both default off; native wsapi seam only. Default SSH mode keeps Gaia service/signing keys on DGX; native compatibility keeps CRESCO_SERVICE_KEY in environment and decrypts user Gaia key inside agent. Existing SSH owner isolation and host pins, verified TLS for control AND dataplane, bounded egress, conservative scopes and confirmation for all mutations.
- IX: all durable mechanics use existing Plane facades; no SQL or migrations.
- X: existing owner-operated DGX Gaia deployment plus sandbox is the intended real-auth staging topology. The enrolled client is available; real role/denial/file flows and affected clients must be verified against a candidate image before activation, with the explicit Apple-only waiver above. Record exact candidate SHA/image, representative collection, host/gateway/core pins and nonsecret observations. The owner authorized qualified PR, merge and sandbox deployment.
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
