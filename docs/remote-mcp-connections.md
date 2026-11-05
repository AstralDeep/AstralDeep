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
