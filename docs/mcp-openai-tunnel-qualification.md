# Optional OpenAI MCP tunnel qualification

The compatibility finding is **conditional and source-reviewed**. Offline
fixtures show that Astral accepts authenticated modern discovery and refuses
the reviewed legacy fallback. They do not establish the pinned tunnel binary's
readiness, an OAuth browser flow, ChatGPT interoperability, TLS behavior, or a
deployment. No tunnel was started or published for this qualification.
`FF_MCP_SERVER` remains default-off.

## Privacy and authentication boundaries

Use synthetic, non-sensitive inputs in an isolated operator-approved pilot.
MCP prompts, arguments, results, and connector-forwarded authorization pass
through OpenAI's hosted tunnel service. Outbound-only networking keeps the
server private; it does not keep this traffic inside the local trust boundary.
**Strict-local-auth is unsupported for this pilot**: a requirement that the
user's bearer never traverse OpenAI is incompatible with forwarded bearer auth.
Static headers injected by the local client do not prove per-user OAuth or
owner isolation and must not substitute a shared credential for either.

The pinned client's [OAuth configuration](https://github.com/openai/tunnel-client/blob/fda241e2343ddd49dd63a7da3e1afab2a6e91722/docs/configuration.md)
forwards inbound authorization and leaves browser authorization directed at the
upstream identity provider. Private Keycloak browser authorization is not
automatically tunneled: verify browser reachability, the registered redirect
URI, and the intended issuer/audience without weakening Keycloak or TLS checks.
Separate OAuth metadata origins require explicit client trust; that trust does
not expose arbitrary Keycloak routes.

The [official secure MCP tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
(living document retrieved 2026-10-28) describes private connections and OAuth
limitations. A temporary private tunnel does not satisfy stable public plugin
hosting or public distribution requirements. Its hosted transport is separate
from Astral's Keycloak IAM, RFC 8693 delegation, per-owner permissions, PHI,
egress, confirmation, LETS, and audit controls. No bypass is part of this guide.

## Exact source and dependency pins

The [pin manifest](../specs/103-openai-mcp-tunnel-qualification/contracts/tunnel-client-pins.json)
records independently retrieved source bytes from 2026-10-28:

| Input | Exact identity |
|---|---|
| Tunnel source commit | `fda241e2343ddd49dd63a7da3e1afab2a6e91722` |
| Go toolchain required by upstream `go.mod` | `1.27.0` |
| Transitive Go module | `github.com/modelcontextprotocol/go-sdk v1.7.0` |
| SDK release tag's resolved source commit | `bc72835f62eb94d0fb484439f886b6885b075f36` |
| Module archive checksum recorded in upstream `go.sum` | `h1:yqjY2dsbKAC0LSuWZVBMrHgiG8ukXv6NRo0JiALay44=` |
| Module `go.mod` checksum recorded in upstream `go.sum` | `h1:dL7u98E/zjJTGzEq+j30jQ8K2k1mb6LeAH4inEcSGts=` |
| Tunnel `go.mod` SHA-256 | `4e4824c574831a0a0b5b083a99a240e03bdcee6907212d6be18562a678cf3bfa` |
| Tunnel `go.sum` SHA-256 | `8c6fe6af4c67b4c7f7b4cfb9c7c55f59a28f8c61201a2fc83e596416f0c03af3` |

Sources are the pinned [go.mod](https://github.com/openai/tunnel-client/blob/fda241e2343ddd49dd63a7da3e1afab2a6e91722/go.mod)
and [go.sum](https://github.com/openai/tunnel-client/blob/fda241e2343ddd49dd63a7da3e1afab2a6e91722/go.sum).
The checksums were read from source, not verified by downloading the module
archive. The original environment's egress failures remain historical evidence
in the manifest; source retrieval now succeeds. No Go dependency is added to
AstralDeep's Python runtime or container.

For an operator's separate source build, use a new directory and a Go toolchain
supporting upstream's declared version. These commands have **not** been run as
binary qualification. Compare both source hashes above and the `Sum` and
`GoModSum` fields below; stop on any difference. Keep the default Go checksum
verification enabled. Retain the full build log and binary digest privately.

```sh
TUNNEL_CHECKOUT="$(mktemp -d)/tunnel-client"
git clone https://github.com/openai/tunnel-client.git "$TUNNEL_CHECKOUT"
git -C "$TUNNEL_CHECKOUT" checkout --detach fda241e2343ddd49dd63a7da3e1afab2a6e91722
cd "$TUNNEL_CHECKOUT"
git rev-parse HEAD
go version
shasum -a 256 go.mod go.sum
go mod download -json github.com/modelcontextprotocol/go-sdk@v1.7.0
go mod verify
mkdir -p bin
go build -trimpath -o bin/tunnel-client ./cmd/client
go version -m bin/tunnel-client
shasum -a 256 bin/tunnel-client
TUNNEL_BIN="$TUNNEL_CHECKOUT/bin/tunnel-client"
export TUNNEL_BIN
"$TUNNEL_BIN" run --help
```

Do not replace this commit with a latest release and call it the same
qualification. A different binary or dependency graph needs its own evidence.

## Deterministic offline smoke test

From an initialized AstralDeep checkout with its normal Python test dependencies
and exact component submodules, run:

```sh
git submodule update --init --recursive
export PYTHONPATH=backend:components/AstralProjection/src:components/AstralProjection/backend:components/AstralProjection:components/AstralPlane/src:components/AstralPrimitives/src:components/LETS/src
ASTRAL_ENV=development PYTHON_DOTENV_DISABLED=1 python3.11 -m pytest -q \
  backend/tests/test_mcp_openai_tunnel_qualification_103.py
```

The 11 tests install the real routes directly in a local FastAPI test process.
They use development/fake token fixtures, an in-memory admission coordinator,
fake tool execution, and a simulated disconnect; they do not open an OpenAI
connection, validate real Keycloak issuance/revocation, or start the tunnel.

| Synthetic probe | Expected Astral response |
|---|---|
| `GET /.well-known/oauth-protected-resource/mcp` | `200`, resource and Keycloak authorization-server metadata |
| Unauthenticated modern `server/discover` | `401`, bearer challenge with protected-resource metadata |
| Legacy `initialize`, without or with fixture bearer | `400`, strict modern envelope validation refuses the request |
| Authorized modern `server/discover` | `200`, protocol `2026-07-28` |
| Rejected fixture tokens | `401`, sanitized denial |
| Tool unavailable to the caller | Empty projected catalog; call returns `isError`, no dispatch |

Modern requests require `params._meta` with
`io.modelcontextprotocol/protocolVersion`, `clientCapabilities`, and `clientInfo`,
plus matching `MCP-Protocol-Version` and `Mcp-Method` headers. Tool calls also
mirror `Mcp-Name`. Bearers belong only in `Authorization` headers. The suite
also covers bounded payload/media/JSON refusals and cancellation cleanup.
Its sanitized transcript stores paths, allowed header **names**, and statuses;
never credentials, arguments, or results.

The [SDK's pinned fallback](https://github.com/modelcontextprotocol/go-sdk/blob/bc72835f62eb94d0fb484439f886b6885b075f36/mcp/client.go)
tries modern discovery and then legacy `initialize` on a non-modern error.
The [client's startup probe](https://github.com/openai/tunnel-client/blob/fda241e2343ddd49dd63a7da3e1afab2a6e91722/pkg/mcpclient/fxmodule.go)
uses that SDK connection. The Python profile models selected requests,
including separate authenticated/unauthenticated legacy refusals; it is not a
byte-for-byte capture or implementation of that binary. Its modeled verdict
is NOT-READY. Whether the real client's final probe status causes `/readyz`
to return `503` is an inference pending live observation. The offline path
returns no Astral `5xx`; this is not a general availability guarantee.

## Bounded, opt-in live qualification

This is an operator procedure, outside CI and separate from deployment
qualification. Run it only after authorizing the outbound OpenAI connection
and privacy boundaries. Prepare a disposable candidate environment using
[MCP endpoint setup](mcp-server-endpoint.md) and
[production configuration](production-deployment.md): real Keycloak,
representative synthetic owners/data, normal database/workers, a trusted
canonical `PUBLIC_BASE_URL`, and verified HTTPS. Enable `FF_MCP_SERVER=true`
only in that environment and recreate its orchestrator container. Keep mock
auth disabled. Do not change a shared or production deployment for this test.

Use an operator-provisioned private tunnel with the intended organization and
workspace associations and a restricted runtime tunnel key. Supply that key
locally through the secret manager as `CONTROL_PLANE_API_KEY`; keep its value
out of chat, shell history, command arguments, logs, and screenshots. It is a
tunnel transport credential, not an Astral LLM-provider credential. The human
operator completes all OpenAI and Keycloak sign-ins.

In the build terminal, create a private config directory. Replace the three
non-secret placeholders with the pilot's values. The profile deliberately
sets no static MCP bearer or discovery bearer; readiness must be measured
under normal per-user authentication. Flags and YAML keys are from the pinned
[configuration reference](https://github.com/openai/tunnel-client/blob/fda241e2343ddd49dd63a7da3e1afab2a6e91722/docs/configuration.md).

```sh
umask 077
QUAL_DIR="$(mktemp -d)"
export QUAL_DIR
cat > "$QUAL_DIR/tunnel.yaml" <<EOF
config_version: 1
control_plane:
  tunnel_id: REPLACE_WITH_PRIVATE_TUNNEL_ID
  api_key: env:CONTROL_PLANE_API_KEY
mcp:
  server_urls:
    - channel: main
      url: https://REPLACE_WITH_ASTRAL_ORIGIN/mcp
  oauth_trusted_origins:
    - https://REPLACE_WITH_KEYCLOAK_ORIGIN
health:
  listen_addr: 127.0.0.1:0
  url_file: $QUAL_DIR/health-url
admin_ui:
  open_browser: false
EOF
python3.11 - <<'PY'
import os
import signal
import subprocess

process = subprocess.Popen([
    os.environ["TUNNEL_BIN"], "run", "--config",
    os.path.join(os.environ["QUAL_DIR"], "tunnel.yaml"),
])
try:
    process.wait(timeout=300)
except (subprocess.TimeoutExpired, KeyboardInterrupt):
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
raise SystemExit(process.returncode)
PY
```

The wrapper bounds the session to five minutes plus shutdown. It does not
interpret exit status as compatibility success. In another terminal, read the
pilot's private health-url file and query its loopback address with bounded
requests (set `QUAL_DIR` to the same directory first):

```sh
TUNNEL_HEALTH_URL="$(cat "$QUAL_DIR/health-url")"
curl --max-time 5 --silent --show-error -o /dev/null -w '%{http_code}\n' "${TUNNEL_HEALTH_URL%/}/healthz"
curl --max-time 5 --silent --show-error -o /dev/null -w '%{http_code}\n' "${TUNNEL_HEALTH_URL%/}/readyz"
```

Record liveness and readiness separately using the pinned
[health contract](https://github.com/openai/tunnel-client/blob/fda241e2343ddd49dd63a7da3e1afab2a6e91722/docs/health.md).
`401` auth-required reachability and a failed legacy probe are different
outcomes. A healthy process or successful poll does not prove tool readiness.
If the fallback leaves readiness at `503`, retain sanitized status evidence
and stop; do not enable legacy initialization, remove headers, weaken audience
validation, inject a shared token, or report success.

Only if readiness and the real OAuth flow succeed, use the intended OpenAI
surface for modern discovery and one non-destructive synthetic tool call.
Verify the permitted owner succeeds; a second owner without permission sees
no such tool and cannot dispatch it; rejected expired/revoked credentials
remain denied. Cancel one operation, interrupt and restart the tunnel once,
then verify no effects replay and a fresh permitted request recovers. Use
the normal authenticated dispatch and private audit evidence. Record any
platform, token-revocation, or transport limitation as unverified rather than
substituting fixture results. Retain only sanitized paths, methods, header
names, status/error codes, candidate/binary digests, and observation time in
shareable evidence; keep keys, payloads, identities, and full logs private.

Stop the client after the bounded session, revoke the temporary runtime key,
and remove the temporary tunnel/profile through the operator's normal
controls. Set the pilot's `FF_MCP_SERVER=false`, recreate its container, and
verify the MCP route and metadata disappear. Report source review, offline
tests, live observations, and deployment status separately. A successful
pilot does not authorize production activation or public plugin publication.
