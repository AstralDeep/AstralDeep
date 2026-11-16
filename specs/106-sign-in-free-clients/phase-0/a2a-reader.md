# A2A card reader spike

Task T0.10. Supports section 7.4.2 of the spec and decision O12 (unsigned cards pinned by hash and origin). It also checks the "Today no reader exists" premise of T3.3 (B97). Call C30 (fixture A2A servers in the no-Docker suite) can reuse the fixtures.

## What was tried

A pure Python reader that takes a parsed card and works offline. Code is in `a2a-reader/a2a_reader.py`. Fixtures are in `a2a-reader/fixtures/` and `a2a-reader/make_fixtures.py` regenerates them. Tests are in `a2a-reader/test_a2a_reader.py`.

The reader does four jobs:

- Turns `securitySchemes` and the requirements field into typed sign-in descriptions, or refuses with a code and a reason.
- Verifies a card signature as a JWS (RFC 7515) over the canonical card against a supplied key list.
- Pins the signer by RFC 7638 key thumbprint.
- Pins an unsigned card by SHA-256 of its canonical form plus its HTTPS origin.

## A2A version read

Version 1.0.1, tagged on GitHub on 2026-05-28. The spec page at https://a2a-protocol.org/latest/specification/ still names 1.0.0 as the latest released version, so the page lags the tag. I read the normative proto and the spec source at the tag:

- https://raw.githubusercontent.com/a2aproject/A2A/v1.0.1/specification/a2a.proto
- https://raw.githubusercontent.com/a2aproject/A2A/v1.0.1/docs/specification.md (sections 4.5, 7, 8.4)

Whether 1.0.1 changed security text against 1.0.0 is UNVERIFIED (D53). The installed `a2a-sdk` is 1.1.2 with PyJWT 2.13.0 and cryptography 48.0.1 on Python 3.11.15.

## Commands and results

```
cd specs/106-sign-in-free-clients/phase-0/a2a-reader
/Users/sam/Desktop/Work/AstralDeep/.venv/bin/python make_fixtures.py
/Users/sam/Desktop/Work/AstralDeep/.venv/bin/python -m pytest -q .
/Users/sam/Desktop/Work/AstralDeep/.venv/bin/python -m pytest -q --cov=a2a_reader --cov-report=term-missing .
/Users/sam/Desktop/Work/AstralDeep/.venv/bin/ruff check .
```

74 tests pass in about 0.5 seconds. Coverage of `a2a_reader.py` is 98 percent. Ruff reports no findings. The fixtures hold 19 valid cards, 19 malformed cards, 2 signed cards, 1 changed unsigned card and 2 public test keys. The private keys derive from fixed seed strings, so no private key is stored.

An interop test signs a card with the a2a-sdk signer and verifies it with the reader. It also signs with the fixture signer and verifies with the SDK verifier. Both directions pass.

## Schemes parsed

| Scheme | Fixture | Reader result |
|---|---|---|
| API key in header, query, cookie | `api_key_*` | `api_key` with location and parameter name |
| HTTP bearer | `http_bearer` | `http_bearer` with bearer format |
| HTTP basic | `http_basic` | `http_basic` |
| HTTP other (Digest) | `http_digest` | `http_other`, parsed and not usable |
| OAuth 2.0 authorization code with PKCE | `oauth2_auth_code_pkce` | URLs, scopes, PKCE flag, needs system browser |
| OAuth 2.0 device code | `oauth2_device_code` | Usable, needs no browser on the device |
| OAuth 2.0 client credentials | `oauth2_client_credentials` | Parsed and not usable, because it signs in no person |
| OAuth 2.0 implicit and password | `oauth2_implicit_deprecated` | Parsed and not usable, deprecated in the proto |
| OpenID Connect | `openid_connect` | Discovery URL and scopes, needs system browser |
| Mutual TLS | `mutual_tls` | Parsed and not usable, matching "Mutual TLS later" |
| Several alternatives | `alternatives` | OR across entries, AND inside one entry |
| Extended card | `extended_card` | `capabilities.extendedAgentCard` true |
| Per skill requirements | `skill_requirements` | Kept per skill id |
| Spec sample with flat `security` | `spec_sample_flat_security` | Accepted |
| 0.3 typed shape | `v03_flat_types` | Accepted, reported as dialect `v0.3` |

Each malformed fixture is refused with a code: bad API key location, missing name or scheme, missing flows, plain HTTP token URL, missing authorization URL, unknown flow, empty OIDC URL, two variants in one scheme, unknown variant, requirement naming an undefined scheme, scopes not a list, empty requirement, conflicting requirement fields, non-boolean extended flag, plain HTTP interface, schemes not an object, mixed dialects and a nameless card. Every non-HTTPS auth URL is refused.

Signature results: pinned signer gives `verified_pinned`. A valid signature by a different key gives `verified_new_signer` and is not accepted. An edited card, a missing key, a malformed header and `alg: none` all give `invalid`. A key with a copied `kid` but a different key material still counts as a new signer, because the pin is the thumbprint.

Unsigned results: the same card from the same origin gives `match`. A changed version, a widened scheme and a different origin or port each fail, and `changed_paths` names the changed field.

## What today's code does

The task row cites lines 5943 and 6126 of `backend/orchestrator/orchestrator.py`. They have drifted. Current locations:

- `discover_agent` (about line 5958) fetches `/.well-known/agent-card.json` with aiohttp for Astral peers and reads only the `WWW-Authenticate` challenge for the agent key scheme.
- `discover_a2a_agent` (about 6141 to 6150) and `_setup_a2a_client_for_agent` (about 6198 to 6206) use `a2a.client.A2ACardResolver` and then `_admit_a2a_card`.
- `backend/shared/a2a_bridge.py` lines 119 to 153 publish the one `keycloak_oidc` OpenID Connect scheme with the four tool scopes, only when `KEYCLOAK_AUTHORITY` is set.
- `backend/shared/base_agent.py` (about 753 to 790) serves the card and does not read cards.

B97 holds: nothing reads an external agent's `securitySchemes` or requirements. Research did not mention that `backend/orchestrator/a2a_card_trust.py` exists. It already refuses unsigned cards, verifies a signature against an owner pinned JWKS URL or inline key, and refuses a changed card after approval. It uses the a2a-sdk verifier and Python only. It has no unsigned path, no thumbprint signer pin and no sign-in model.

## What Phase 3 can reuse

- T3.3: the typed model (`Scheme`, `Flow`, `Requirement`, `CardReading`), the refusal codes, the OR and AND semantics, the dialect handling and the 19 valid and 19 malformed fixtures as a conformance set for the Rust crate. `usable_alternatives` and `needs_browser` map onto section 7.5's sign-in table. `auth_hosts` gives the host line for the first-connect card in 7.6.
- T3.4: `verify_signed` and the signed fixtures drive the "new signer refused until approved" test. The thumbprint pin and the `verified_new_signer` state are the behaviour to port. The existing `a2a_card_trust.py` already covers the Deep side of signed cards.
- T3.5: `pin_unsigned`, `check_unsigned`, `https_origin` and `changed_paths` match O12 and the "approval card showing what changed" row in 7.7.
- T3.6: untouched. The spike did not model `TASK_STATE_AUTH_REQUIRED`.

## Gaps and open risks

- The spec contradicts itself on the requirements field. The proto and JSON binding use `securityRequirements` with `{"schemes": {name: {"list": [...]}}}`. The spec text and its sample card use `security` with `[{name: [scopes]}]`. The reader accepts both and refuses a card whose two fields disagree. A real 1.0 server may emit either (D55).
- AND and OR semantics still rest on a third party source (D56). The proto comment says "a map of security schemes to the required scopes" and gives no more. UNVERIFIED from primary text.
- Canonical form diverges from the SDK for non-ASCII text. Measured: for a card with `café` in the description the reader's RFC 8785 bytes differ from `a2a.utils.signing._canonicalize_agent_card`, which escapes non-ASCII. A card signed by the Python SDK with non-ASCII text fails this reader. Phase 3 must pick one form or accept both. ASCII cards and the `pkceRequired: false` case match the SDK byte for byte.
- Default-value stripping is approximate. The spec says to omit defaults unless a field is REQUIRED or `optional`. The reader drops empty strings, lists and objects, drops `false` and 0 except for the three presence booleans, and refuses non-integer numbers. The SDK strips by protobuf rules. Interop is proven for the fixtures only. Third party signers are UNVERIFIED.
- Key resolution is out of scope. `jku` fetching, expiry and revocation (the spec forbids expired or revoked keys) are not modeled. The caller supplies keys.
- The pin binds the key, not the owner's approval record. Storing pins and showing the approval card belong to T3.4 and T3.5.
- Client enforces PKCE. A flow with `pkceRequired: false` parses and carries the flag `pkce_not_declared_client_enforces`. Phase 3 should always send S256.
- Extended card fetch, the extended card endpoint path per binding and its merge into the cached card were not built. Spec 13.3 and sections 9.4.8, 10.4.11 and 11.3.4 hold the paths. UNVERIFIED here.
- Interface URLs on another origin than the card fetch are listed but not judged. A card may send traffic to a host the owner never approved. The approval card should show them.
- Python only. The Rust crate choice for T3.3 is not made here.
