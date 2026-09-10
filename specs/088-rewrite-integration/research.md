# Integration decisions

Status: design, 2026-09-10; source identities and review evidence are pinned in the spec and inventory. Decisions are not implementation claims.

## Component ownership

**Decision:** Behavioral port into existing Deep/Plane/Projection. **Rationale:** Separate Git roots and incompatible schemas/sessions make wholesale copying unsafe; Deep already has durable admission, assignments and scheduling. **Alternatives:** Source replacement discards working capabilities; a mounted donor duplicates UI, persistence and authorization authorities.

## Institutional identity

**Decision:** Reuse `https://iam.ai.uky.edu/realms/Astral`, Deep BFF/CSRF, native PKCE and RFC 8693. **Evidence:** Allowlisted local configuration and unauthenticated OIDC discovery agree on the exact issuer and S256. Existing clients are `astral-frontend`, `astral-desktop`, `astral-mobile`, `astral-watch`; no secrets are recorded. **Rationale:** Preserve verified subject owner keys. **Alternatives:** Donor lowercase local realm and incompatible same-named cookie cannot replace existing identity. Admin registrations/mappers and authenticated live flows remain unverified.

## Durable work

**Decision:** Extend existing Plane facades. Keep current ordinary chat dispatch during initial UI work; enhanced durable tasks use a qualified additive one-shot profile. **Rationale:** Legacy assignment validation requires source/tools and offline grants; source-less one-shot work needs its own explicit profile. WorkAdmission retains claim/capacity fences. **Alternatives:** Relaxing all persistent validation weakens authority; copying donor SQLAlchemy creates another lifecycle owner.

## Retention and retries

**Decision:** Reacquire permitted ephemeral sources per attempt, revalidate selected guidance, and persist bounded references/digests rather than transient source/private-note plaintext. Extend existing action execution with an explicit reconstruction mode where needed. **Rationale:** Donor fetched markers can outlive bytes; existing action intents can archive full model requests. **Alternatives:** Blanket prompt persistence violates retention, and trusting a prior fetched marker loses grounded retry content.

## Shared quiet UI

**Decision:** One primary composer, advanced controls on demand, immediate ordinary Send and exact effect/publication/grant review. Projection remains the server-owned UI source. **Rationale:** Owner explicitly selected this policy and web-first timing. Existing primitives cover donor semantic components. **Alternatives:** A parallel SPA or client-local capability hiding would contradict shared UI/authorization contracts. Personalized Deep shell HTML cannot be cached with the donor service-worker rule; use public offline assets only.

## Provider and guidance preservation

**Decision:** Preserve all working providers and separate user/system configuration; local inference optional. Add donor instruction skills and expiring encrypted private notes alongside existing procedural skills, tool permissions and personalization. **Rationale:** Donor custom-provider save/check does not prove generation support, and instruction skills differ from permission catalogs. **Alternatives:** Copying donor restrictions or implicit provider fallback would remove capabilities/cross credential boundaries.

## Feature accounting

**Decision:** Every donor family receives source anchors, target owner, implementation and evidence status; no data import. **Rationale:** User requested all features, not visual polish alone. **Alternatives:** Cosmetic adoption or unchecked copying cannot establish completeness. Donor tests are context only; integrated live, human and native checks must actually run.
