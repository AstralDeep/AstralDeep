# Remote MCP connections

Remote MCP connections add tools from a remote server to your AstralDeep account. Configure
them from the Mac app; AstralDeep stores the owner-scoped connection settings and connects to
the remote service from its server.

## Availability

The server feature flag `FF_REMOTE_MCP` defaults to off. The server operator must enable it
using the qualified immutable image and recreate the Compose service so it reads the new
environment. A restart alone does not load changed environment settings. If Remote MCP is
reported as disabled, ask the deployment operator; changing a setting in the Mac app cannot
enable it.

Remote server URLs must use HTTPS on port 443 and resolve to a public server. Private-network,
loopback, link-local, and metadata-service addresses are rejected. Enter the provider's final
endpoint URL because redirects are refused. The connection runs from AstralDeep's server, not
from your Mac.

## Add and authorize a connection

In the Mac app, open **Settings → This Mac → Remote MCP…**. Enter a name and the server URL,
choose the transport documented by the provider (**Streamable HTTP** or **Legacy SSE**), and
choose its authentication method:

- **None** for a server that accepts unauthenticated requests.
- **Bearer token** when the provider gives you a token.
- **OAuth** for a server that requires user authorization. AstralDeep uses PKCE with S256.

For OAuth servers with dynamic client registration, leave the OAuth client ID and secret blank.
For a pre-registered client, enter its client ID and enter a client secret only if that client
requires one. OAuth scope comes from the server's authorization challenge when supplied;
otherwise AstralDeep uses the provider's published supported scopes. There is no scope field in
the connection form.

For token requests, AstralDeep supports public-client `none`, `client_secret_basic`, and
`client_secret_post`. New authorization honors a saved method when present; otherwise it selects
an advertised method. When authorization-server metadata omits
`token_endpoint_auth_methods_supported`, RFC 8414 §2 defaults to `client_secret_basic`.
Confidential methods require a configured or dynamically issued secret. Older saved confidential
connections without a method keep their legacy form-post behavior during refresh until they are
authorized again. Older public-client connections continue using `none`.

Dynamic registration prefers `none` when advertised, then chooses `client_secret_basic` or
`client_secret_post` from the server's supported methods, using the RFC 8414 default when the
metadata omits the list. The registration response must agree with the requested method; an
omitted response method defaults to `client_secret_basic` under RFC 7591 §2. Therefore, a public
client registration must explicitly return `none`, while confidential registration must provide a
nonempty client secret. AstralDeep does not fall back to another method after a mismatch. These
rules follow [RFC 8414 §2](https://www.rfc-editor.org/rfc/rfc8414.html#section-2),
[RFC 7591 §2](https://www.rfc-editor.org/rfc/rfc7591.html#section-2), and
[RFC 6749 §2.3.1](https://www.rfc-editor.org/rfc/rfc6749.html#section-2.3.1) (accessed
2026-11-05).

Save the connection. For OAuth, choose **Authorize** and complete the provider's sign-in in
the page that opens. Sign in with your own provider account; AstralDeep does not set up or
select that account for you. After authorization, choose **Test and refresh tools**.

## Review tool access

Discovered tools start disabled. Enable only the individual tools needed for your work. A chat
can call a tool only when its connector and that tool are enabled. Calls continue through
AstralDeep's permissions, HTTPS destination checks, PHI and policy checks, and local audit
records. The **Standard MCP connection** warning beside a connector explains that MCP does not
carry Astral's agent identity and delegation protocol, local records do not prove what the remote
server executed, and the provider controls its data handling.

An agent registered through the server's standard A2A-only discovery path may show a separate
**Standard A2A connection** warning. It explains that the native Astral handshake and the remote
enforcement of delegated permissions have not been verified for that connection. AstralDeep still
applies local permissions, policy checks, and audit records; those records do not prove what the
remote server executed. This is an agent connection warning, not an MCP connector; the Remote MCP
form does not configure A2A connections.

## PanAtlas example

The PanAtlas MCP endpoints are interoperability-test examples only: public
`https://panatlas.net/mcp` and OAuth-protected `https://panatlas.net/mcp/private`. The existing
native Astral agent at `https://panatlas.net/agent` remains available and is not replaced by
these MCP examples. To test the protected MCP endpoint, choose OAuth. Its authorization
metadata supports dynamic registration; leave client credentials blank for that flow. When
prompted, authorize with your own PanAtlas account through ORCID. The server's challenge
requests `panatlas:read`. After authorization, test and refresh the tool list, then enable the
PanAtlas read tools you need.

The public MCP endpoint does not require OAuth. These examples do not mean that a PanAtlas
account or connection is already configured.
