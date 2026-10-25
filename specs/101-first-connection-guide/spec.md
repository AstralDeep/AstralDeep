# Feature Specification: First-Connection Guide and Configured-Service Verification

**Feature Branch**: `101-first-connection-guide`

**Created**: 2026-10-25

**Status**: Implemented at specification level below; live cross-client and staging verification are recorded as required, not performed.

**Input**: Issue #295: "The current source/Compose and component-wheel workflows require operator setup. The easy-install wishlist needs one guided path that establishes a valid composed deployment and explains actionable failures without accepting placeholder production secrets or hiding missing services. This child is limited to: guide first client connection and configured-service verification."

## Overview

A new user's first contact with a composed AstralDeep deployment currently works only when everything is healthy: `make up` builds and starts the stack, the web shell redirects to sign-in, registration pushes the mandatory first-run LLM dialog, and `/readyz` answers the container health check. Nothing in the product tells a connecting user which endpoint they reached, whether that endpoint is the one the deployment publishes, or which configured services are actually ready. Failures surface as generic errors or silence. The host-side `scripts/install_doctor.py` diagnoses the installation before the app is reachable, but it is not visible to a user inside a client.

This feature adds one server-owned first-connection guide: an endpoint-attestation report plus honest per-service readiness (database, authentication, voice workers, generated-agent publication, LETS warden) with actionable remediation, delivered through the standard server-driven UI channels to every client, and additionally available as an authenticated REST resource. The guide never accepts placeholder production secrets, never performs or types a user's sign-in or provider keys, and never hides a missing service behind a success state.

## User Stories

### User Story 1 - Verify the first connection and endpoint trust (Priority: P1)

A user who just opened a client (or a native client configured with a server URL) wants to confirm they reached the right server before signing in.

**Independent test**: call `GET /api/connection/status` (or open the connection surface) and observe the endpoint report.

**Acceptance scenarios**:

1. **Given** a deployment with `PUBLIC_BASE_URL` set, **When** a request arrives from that origin, **Then** the endpoint is reported `verified` with guidance to complete sign-in only on the server's own sign-in page.
2. **Given** a deployment with `PUBLIC_BASE_URL` set, **When** a connection arrives from a different origin, **Then** the mismatch is reported honestly with both addresses and no verification claim.
3. **Given** no public endpoint configured, **When** the report is built, **Then** the guidance names the operator-published address and the Compose default (`http://localhost:8001`) without asserting verification.

### User Story 2 - See honest configured-service readiness with actionable errors (Priority: P1)

**Independent test**: degrade each probed dependency behind its seam and observe the check status, detail, and remediation.

**Acceptance scenarios**:

1. **Given** a ready database, reachable authentication, and completed publication recovery, **When** the report is built, **Then** the overall state is `ready` and each required check is `ready`.
2. **Given** the AstralPlane readiness probe fails, **When** the report is built, **Then** the database check is `unavailable` with remediation naming PostgreSQL diagnosis, and the overall state is `unavailable`.
3. **Given** production posture with an unconfigured or unreachable identity provider, **When** the report is built, **Then** the authentication check is `unavailable` with remediation; **Given** development mock authentication, **Then** the check is `ready` but explicitly labeled development-only.
4. **Given** voice enabled with no connected worker (or an invalid speech-backend selection, or a failed voice bootstrap), **When** the report is built, **Then** the worker check reports the honest degraded/unavailable state with remediation; **Given** voice disabled or client-local speech, **Then** the check is `off` and does not block the required overall state.
5. **Given** generated-agent publication unresolved (or the service missing/raising), **When** the report is built, **Then** the publication check is `unavailable` and the overall state is `unavailable`.
6. **Given** the LETS warden off, healthy, blocked, or unreadable, **When** the report is built, **Then** the LETS check reports `off`/`ready`/`unavailable`/`degraded` respectively and never blocks the required overall state.
7. **Given** a required service that later recovers, **When** the report is rebuilt, **Then** the check and overall state return to `ready` (recovery is observable).

### User Story 3 - Be guided through user-completed sign-in and encrypted key setup (Priority: P1)

**Acceptance scenarios**:

1. **Given** the guide payload, **When** rendered on any client, **Then** the sign-in guidance instructs the user to complete sign-in themselves on the server's own sign-in page and to enter the provider key only in the encrypted in-product LLM setup, and states that agents never see or type user provider keys.
2. **Given** a first registration where the LLM gate owns the modal slot, **When** registration completes, **Then** the connection guide does not displace the mandatory LLM setup dialog (the guide may be re-opened and the REST resource remains available).

### User Story 4 - Be shown the guide when a required service is not ready (Priority: P2)

**Acceptance scenarios**:

1. **Given** a registration where a required service probe fails, **When** registration completes, **Then** the server pushes the connection guide modal (mandatory, with the failing checks and remediation).
2. **Given** a registration where all required services are ready, **When** registration completes, **Then** no connection guide is pushed.
3. **Given** a registration where the guide evaluation or push fails, **When** registration completes, **Then** registration still completes (the failure is logged, never fatal).
4. **Given** any client, **When** the `chrome_connection_refresh` action arrives for the open surface, **Then** the surface re-renders with fresh checks.

## Requirements

- **FR-001**: The connection guide payload MUST include: the contract identifier, the endpoint report (connected origin, configured public endpoints, verification verdict, guidance), the per-service checks, the overall state, and the sign-in/key guidance.
- **FR-002**: Service checks MUST report real probes, not cached or hard-coded success: database via the orchestrator's own AstralPlane readiness probe, authentication via the configured identity provider posture and discovery reachability, voice workers via the live worker-pool readiness, publication via the generated-agent publication readiness service, LETS via the composition-bound snapshot.
- **FR-003**: The overall state MUST be driven only by required checks (database, authentication, publication); optional checks (voice workers, LETS) are reported but never manufacture a required-service failure.
- **FR-004**: Every non-ready check MUST carry an actionable remediation string; no check may claim readiness it did not observe.
- **FR-005**: The REST resource `GET /api/connection/status` MUST require authentication (401 otherwise), return 200 with the payload when the overall state is ready, and 503 with the same honest body when it is not.
- **FR-006**: The guide surface (`connection` key in the server-owned surface registry) MUST render through the existing shared channels — modal HTML for the web and adapted primitive components for native clients — with no per-client renderer changes, only already-approved primitive types, and no new dispatch action (the re-check control re-opens the surface through the standard, already-manifested `chrome_open` action).
- **FR-007**: The registration flow MUST push the guide when a required service is not ready, MUST NOT push it when the LLM first-run gate owns the modal or an active chat is resumed, and MUST treat its own failure as non-fatal.
- **FR-008**: No production secret may be accepted, displayed, or stored by the guide; user sign-in and provider keys remain user-completed in the existing encrypted flows.
- **FR-009**: No component repository source, schema, migration, UI-protocol manifest entry, or workflow file may change in this feature; delivery uses pinned components through their declared contracts.

## Non-Goals

- Host-side installation diagnosis (`scripts/install_doctor.py`) is unchanged.
- No new settings-menu entry: the shared chrome menu model is Projection-owned; the guide reaches clients via push, the standard surface-open path, and the REST resource.
- No staged/protected release claim: staging, real-dispatch, and per-client live verification remain required (see plan).
