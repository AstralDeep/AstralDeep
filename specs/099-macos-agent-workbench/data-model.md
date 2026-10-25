# Data Model

No schema change. Existing computer hosts/sessions and generic source-bound pending approvals are ephemeral; durable remote-operation proposals continue through existing Plane APIs. No new durable computer registry is claimed.

| Entity | Identity/state | Validation/lifecycle |
|---|---|---|
| Consent | Owner+server+stable UUID4 host, disabled/enabled | Default off; direct-download and actual sandbox-entitlement checks; clear on account/server replacement. |
| Descriptor | Exact v1 fields | macos, strict SemVer,1–8 indexed displays/one primary, implemented closed verbs. |
| Session | Canonical UUID, host, active/paused/ended, local epoch | Current owner/channel; retired sessions cannot return. |
| Ticket | creq_+32hex, session, args digest, epochs, receipt/deadline | Reserve before queue; physical-boundary check; duplicate cannot execute; fail closed on capacity. |
| Observation | DisplayID/global bounds/captured dimensions/topology epoch | Independent axis scaling/negative origins; invalidate geometry on change. |
| Window | Positive opaque handle to current AX element/process | Bounded inventory; stale/ambiguous refusal. |
| Root | Owner/server-bound selected bookmark/open directory | openat/no-follow traversal, no parent/symlink/special file escape; revoke with owner. |
| Result | success/error/uncertain and bounded evidence | Transport accepted does not mean semantic effect verified. |

Any future durable receipt requires separately governed Plane work rather than implicit Deep storage.
