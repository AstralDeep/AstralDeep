# Direct ORCID identity for restricted external agents

Keycloak continues to authenticate the Astral account, but it does not need to
store or emit an ORCID attribute. Restricted agents can declare a separate
external-identity link. For PanAtlas, the user selects **Connect ORCID** in the
agent detail, signs in through PanAtlas's existing WordPress ORCID integration,
and returns to Astral with a short-lived signed assertion.

Tool arguments, request headers, browser fields, and agent-supplied caller
metadata are never identity evidence. Astral accepts the ORCID only from the
signed handoff, stores it against the authenticated Astral user, and projects
only that value into `caller_info.verified_identity.orcid`.

## 1. Configure the trust boundary

Generate one independent high-entropy secret. Put the exact same value in these
two root-owned deployment locations; do not reuse `AGENT_API_KEY`.

On Astral (`sandbox.ai.uky.edu`):

```dotenv
IDENTITY_CLAIM_TRUSTED_AGENTS=panatlas-1
EXTERNAL_IDENTITY_LINK_SECRETS={"panatlas-1":"<at-least-32-random-characters>"}
```

On PanAtlas (`panatlas.net`), write only the secret value (no `KEY=` prefix) to:

```text
/etc/panatlas/astral-link.secret
```

The file must be root-owned, group-readable only by the web-server group, and
mode `0640`. The WordPress plugin also permits an operator-defined
`PANATLAS_ASTRAL_LINK_SECRET` constant/environment value, but the dedicated file
keeps the secret out of the repository and WordPress database.

Both defaults are fail-closed. Restart/recreate Astral and reload the PanAtlas
PHP runtime after changing them.

## 2. Browser link flow

The PanAtlas card advertises its HTTPS authorization endpoint. Astral renders
the button only on that agent's detail surface and performs these checks:

1. Astral creates a five-minute signed state bound to the authenticated Astral
   subject, `panatlas-1`, and provider `orcid`.
2. PanAtlas validates the state, requires its normal WordPress ORCID login, and
   refuses any iD outside `PANATLAS_ALLOWED_ORCIDS`.
3. PanAtlas returns a two-minute signed assertion containing the canonical ORCID
   iD and the exact state.
4. Astral verifies both signatures and bindings, rejects expired/tampered/reused
   state, prevents one ORCID from being linked to two Astral accounts, and stores
   the result in the user's existing preferences row.
5. Normal chat, parallel/chained calls, component re-execution, background work,
   and Astral's external MCP endpoint all use the same identity gate.

The signed values travel only over HTTPS, are removed from the address bar by an
immediate redirect, and are never bearer credentials for either application.

## 3. Approved identities

PanAtlas is authoritative and independently enforces exactly:

- Cody: `0000-0001-9588-3501`
- Sam: `0009-0003-6606-0831`

Do not change the WordPress allowlist or `PANATLAS_ALLOWED_ORCIDS` on the agent
service independently. A mismatch fails closed but creates confusing UI.

## 4. Verify

1. Sign into Astral normally through Keycloak.
2. Open **Agents & permissions**, select **PanAtlas**, and choose **Connect
   ORCID**.
3. Complete the PanAtlas ORCID login. Reopen the detail and confirm it says
   `Connected: <ORCID iD>`.
4. Call `summarize_atlas_state`; inspect the PanAtlas service log and confirm the
   call succeeds without logging a token or link secret.
5. Confirm an unlinked Astral account receives no PanAtlas tools in chat or the
   Astral MCP `tools/list` projection.
6. Confirm a valid but unlisted ORCID receives a PanAtlas 403 during linking and
   a forced tool call still returns non-retryable MCP error `-32001`.

## 5. Rollback

Remove `panatlas-1` from `IDENTITY_CLAIM_TRUSTED_AGENTS` and recreate Astral.
That immediately hides and refuses the tools while retaining the user's link.
For an immediate PanAtlas-side stop, run `systemctl stop panatlas-astral`.
Removing `EXTERNAL_IDENTITY_LINK_SECRETS` additionally disables new links and
callbacks; it does not weaken the agent's own ORCID allowlist.
