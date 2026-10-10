# Personal mesh enrollment

Install `astral-sdk[mesh]` to enroll a device that has its own Ed25519 signing key.
The mesh API is independent of the SDK's Work framework credential interface.
An authenticated human owner creates and confirms the first invitation using an
existing Keycloak session whose refresh credential is held by AstralDeep. A
current member with `mesh:confirm` can confirm a different creator's invitation;
it cannot confirm its own invitation or grant scopes it does not hold.

```python
import httpx
from astral_sdk.mesh import MeshClient, MeshDevice

device = MeshDevice.generate()
device.save("tablet.mesh", custody_password)

# Supply the existing authenticated owner's HTTP client and signed session cookie.
owner = MeshClient(server_url, device, client=owner_http)
invitation = owner.invitation(label="Kitchen tablet", scopes=["tools:read", "mesh:confirm"])
owner.confirm(invitation["invitation"]["invite_id"])

member = MeshClient(server_url, device)
member.redeem(invitation["payload"], agent_id="reader")
device.save("tablet.mesh", custody_password, replace=True)
result = member.invoke("reader", "read", {"value": "example"})
member.close()
```

`owner_http` uses normal Keycloak bearer or cookie authentication and contains
the server-issued `astral_session` cookie. Bearer authentication alone cannot
authorize durable owner confirmation without that matching session custody.
The owner and member clients should use separate HTTP clients. Never copy an
owner access token, refresh token, or cookie onto the enrolled device.

The CLI keeps credentials out of arguments and output:

```text
python -m astral_sdk.mesh --custody tablet.mesh key-init
python -m astral_sdk.mesh --custody tablet.mesh --server https://astral.example enroll --agent reader
python -m astral_sdk.mesh --custody tablet.mesh --server https://astral.example tool reader read --arguments '{"value":"example"}'
```

`key-init` prints only the public JWK. Give it to the owner when creating the
invitation. `enroll` prompts for the invitation payload and device-custody
password. `tool` loads the encrypted key, obtains a fresh member session with a
possession proof, and invokes the ordinary tool dispatcher. Password-encrypted
custody uses PBKDF2-SHA256 with a fresh salt and AES-256-GCM with an authenticated
format discriminator. Publication is atomic; an existing file is replaced only
after explicit enrollment updates its member identity. Access tokens remain in
memory and are cleared when the client closes. Losing the file or password
requires an owner to revoke the member and issue a new invitation.

## REST contract

All paths below use the operator-pinned `PUBLIC_BASE_URL` (or
`BACKEND_PUBLIC_URL`). HTTPS is required in production. Payload links place the
one-time secret in a URL fragment; it must not be sent as a query parameter.
Invitation payloads are secrets: do not log them, place them in shell arguments,
or save them in reports. An Ed25519 public JWK has exactly `kty=OKP`,
`crv=Ed25519`, and the base64url public-key `x` value.

| Request | Authorization and body | Result |
| --- | --- | --- |
| `POST /api/mesh/invitations` | Owner IAM or proof-bound member with `mesh:confirm`; `label`, `device_key`, `scopes`, optional `ttl_seconds` | Public invitation and one-time payload |
| `POST /api/mesh/invitations/{id}/confirm` | Owner IAM plus matching server custody, or another current confirming member | Confirmed, attenuated invitation |
| `POST /api/mesh/invitations/{id}/reject` | Owner IAM or current confirming member | Rejected invitation and canceled challenge |
| `POST /api/mesh/enrollment/redeem` | `payload`, Ed25519 `signature` over the decoded invitation challenge, optional `agent_id` | `token_type=DPoP`, short-lived `access_token`, public member |
| `POST /api/mesh/nonce` | `owner_id`, `member_id` | Single-use nonce, valid for 60 seconds; bounded issuance |
| `POST /api/mesh/session` | `owner_id`, `member_id`, `nonce`, `proof`, optional `agent_id` | New proof-bound member token; the previous token is invalidated |
| `GET /api/mesh/me`, `/members`, `/invitations` | Member proof headers; owner IAM may read the owner roster and invitations | Public records; invitation reads require confirmation authority |
| `POST /api/mesh/members/{id}/revoke` | Owner IAM | Atomically revoked neutral and host member records |
| `POST /api/mesh/tools/{agent_id}/{tool}` | Member proof headers and `{"arguments": {...}}` | Ordinary audited dispatcher response |

For a member request, send `Authorization: DPoP <access_token>`, one `DPoP`
proof header, and one `DPoP-Nonce` header. Each proof is an EdDSA JWT with header
`typ=dpop+jwt`, the public `jwk`, and payload fields `jti`, exact uppercase
`htm`, exact operator-pinned absolute `htu`, integer `iat`, and the nonce. Add
`ath`, the base64url SHA256 of the access token, for authenticated requests.
Session-issuance proofs omit `ath`. Nonces are single-use and owner/member
bound. Duplicate headers, wrong methods/origins/keys/tokens, replay, omitted
proofs, a bearer downgrade, and the retired `X-Astral-Member-Key` header fail
closed. The SDK constructs these proofs and obtains a new nonce per request.

## Authority and failure behavior

Keycloak remains the only user IAM. The host refreshes the confirmed owner's
existing, issuer/client-bound session, verifies the current Keycloak user token,
and performs the existing RFC 8693 exchange into the agent-service audience.
The actual signed exchange result must contain exactly the allowed `tool:*`
set and the effective `tools:*` intersection of member grants and current owner
permissions. An administrative role, additional tool scope, foreign owner,
wrong audience, or expired exchange result refuses issuance. Configure the
existing Keycloak optional client scopes and role mappings for this attenuation;
the member flow does not require Keycloak's DPoP feature or a second IAM.

Only the resulting private agent-service token stays encrypted on the host.
The device receives a short-lived internal delegation token with `cnf.jkt`, an
exact target agent (or a control-only audience), mesh membership/revocation
epochs, member version, stable host authority revision, and owner-session
incarnation. A public key or invitation secret alone grants no IAM or tool
authority. The human owner's independent Keycloak authority remains distinct
from any device member.

AstralPlane owns neutral mesh, member, public-identity, invitation and challenge
records. Deep uses its qualified public facade, locking mesh before member and
session fences, and stores only host policy and encrypted IAM custody through
Plane credential repositories. Invitation consumption, member activation,
identity binding, host credential CAS, and durable audit share one transaction.
A failed audit or expiry during a blocking durable write rolls them all back.
Rejected, replayed, stale, foreign, revoked or ambiguously bound records cannot
produce a usable member session.

Every ordinary dispatcher admission, delegation lookup and physical tool attempt
rechecks current Plane epochs, active member/key identity, token custody, target
agent, and scopes. Existing permission, policy, PHI, egress, confirmation and
governed-dispatch gates still run. Delegation remains mandatory for members even
when the host's general development delegation flag is disabled. Already
admitted physical work follows the existing dispatcher lifecycle; member
revocation does not claim to undo an effect that already began.

Adding or revoking a member changes the mesh's global epochs and invalidates
previous member tokens. A still-active device can obtain a fresh session using
its key. Revoked devices cannot refresh. Owner logout, replacement of the
custodied session, hard expiry, or an unavailable IAM refresh refuses new
sessions; current dispatch also refuses dead custody. There is no offline IAM
fallback or durable owner bearer on a device. Existing web/native human clients
retain their ordinary owner IAM flows; this API contribution adds no shared UI
or primitive contract.

The nonce and proof construction follows [RFC 9449](https://www.rfc-editor.org/rfc/rfc9449.html).
User authority uses the existing [Keycloak RFC 8693 token exchange](https://www.keycloak.org/securing-apps/token-exchange).
The application verifies its own resource proofs and does not assume an enabled
[Keycloak DPoP deployment](https://www.keycloak.org/securing-apps/dpop).

## Two-device qualification sequence

Use a synthetic owner with a real Keycloak session held by the running backend,
the exact qualified Plane pin and PostgreSQL schema, and a harmless registered
read tool. Keep all tokens, cookies, keys and invitation payloads private.

1. Device A generates its key. The owner creates and confirms A's invitation.
   A proves possession, redeems into its selected agent audience, and invokes
   the harmless tool through `/api/mesh/tools/...`.
2. Device B generates a different key. The owner creates B's invitation. A
   confirms it using a fresh nonce and token-bound proof. B redeems and invokes
   the same ordinary tool. An invitation created by A cannot be confirmed by A.
3. A's old token fails after B's activation changes the membership epoch. A
   proves possession to `/session`, receives a fresh token, and succeeds.
   Verify foreign targets, missing proof headers, bearer downgrade, replayed
   nonces and a scope outside B's attenuated grants fail without an actuator call.
4. The owner revokes A. A's nonce/session/tool paths fail. B's previous token
   fails the new revocation epoch; B refreshes and succeeds. The independent
   owner's IAM roster request still succeeds.
5. Log out the synthetic owner's custodied session using the existing logout
   endpoint. B cannot obtain a nonce/session or invoke a tool. Separately test
   actual IAM unavailability and broader exchange roles/scopes: issuance fails
   and the invitation remains unredeemed with no activated member.

Local deterministic tests use real PostgreSQL and signed synthetic Keycloak
responses at only the external HTTP boundary. A local pass does not establish
live Keycloak staging or exact-head hosted qualification; retain those receipts
separately before merging.
