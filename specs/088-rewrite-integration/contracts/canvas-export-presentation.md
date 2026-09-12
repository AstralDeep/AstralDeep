# Ephemeral native canvas presentation

The native Export page operation first performs the existing authenticated
canvas GET authorization/audit, then captures its currently displayed canvas.
The new presentation POST independently checks current authority; a prior GET
is never an authorization grant. Public Share and the legacy canvas GET keep
their existing stored-snapshot, PHI, audit and response behavior.

## HTTP contract

`POST /api/export/canvas/{chat_id}/presentation?render_revision=7`

- Authentication uses current Keycloak bearer credentials or the existing web
  session cookie, with the existing user/admin role policy. These read checks
  do not persist a user profile. Query-token credentials are refused for POST.
- Cookie requests require one exact Origin matching the configured
  `PUBLIC_BASE_URL`, then `BACKEND_PUBLIC_URL`, or the request origin when
  neither is configured. Explicit bearer requests do not require Origin.
  Default HTTP/HTTPS ports compare equivalently; an explicit port zero does
  not become the default port.
- The only query parameter is one required `render_revision`, encoded as
  canonical nonnegative decimal, at most `2^63-1`.
- Content-Type must be JSON. Encodings other than identity, duplicate sensitive
  headers, malformed Content-Length and declared/actual size disagreement are
  refused. The body is read as bounded chunks before JSON parsing, with an
  8 MiB limit imported from Projection.
- The request is the exact six-field `astral.canvas-export/v1` representation:
  `version`, `components`, `viewport`, `theme`, `display_state`, `images`.
  Projection's `contracts/canvas_export_v1.json` and adjacent Markdown own all
  tree, identity, pixel, viewport and palette rules. Deep does not independently
  invent component adaptation or presentation validation.
- The response is exactly `version`, `html`, `viewport`, `theme`, with
  `X-Astral-Render-Revision` carrying the verified server revision. The complete
  encoded JSON response is bounded by Projection's 32 MiB output limit.
- Every success and endpoint refusal sends `Cache-Control: no-store`,
  `X-Content-Type-Options: nosniff`, and `Referrer-Policy: no-referrer`.

The viewport carries both actual canvas and source window dimensions so the
native isolated finalizer can preserve the source layout and media queries.
Loaded images and already-ready charts use captured PNG pixels; native clients
must refuse capture when the required current pixels are unavailable. The
renderer must not fetch a URL or reconstruct a chart in a different view state.

## Lifetime and authority

Before consuming capture bytes, Deep reads the exact owner's conversation
through the application Plane runtime and checks the revision and export flag.
It repeats current IAM/role validation, exact owner identity, conversation
ownership, revision and flag after rendering. Application replacement also
refuses the response. The regular JWT validity contract applies; this endpoint
does not introduce access-token revocation introspection.

The submitted capture is untrusted display data. It is not evidence of a
committed result, and its revision does not attest its contents. The endpoint
does not dispatch an action, apply ROTE again, call a tool, resolve a reference,
fetch a URL, create a share, write a file/database record, hash the capture,
or record capture text in logs/audit. A user can submit transient visible
content, but cannot use this route to commit or publish it.

Each application service admits at most two export requests immediately,
without a render waiting queue. Body reading has a 10-second limit and CPU
render awaiting has a 15-second limit. Rendering and JSON serialization run
off the ASGI loop; each Plane transaction also runs through its existing
bounded off-loop adapter. A timed-out or cancelled request retains its render
slot until the already-started worker actually finishes, and its eventual
exception is consumed without logging its content. Cancellation does not
pretend to terminate a Python worker thread.

Native clients must still capture and recheck their own owner/session,
chat, presentation and export-revision lifetime before using a response or
finishing a save. The resulting HTML belongs only in the isolated, offline,
bridge-free export finalizer. It is never loaded as trusted application UI.
No native operation automatically retries a presentation POST.

## Bounded refusals

Refusals return only `{"error": "<closed code>"}`; unexpected exceptions,
authentication details, validation internals and submitted data are not echoed.

| Status | Codes |
| --- | --- |
| 400 | `presentation_length_invalid` |
| 401 | `presentation_authentication_required`, `presentation_identity_changed` |
| 403 | `presentation_authentication_required` (role denial), `presentation_origin_refused`, `presentation_query_token_refused` |
| 404 | `presentation_not_found` (disabled, absent or foreign conversation) |
| 408 | `presentation_interrupted` |
| 409 | `presentation_revision_changed` |
| 413 | `presentation_too_large`, `presentation_output_too_large` |
| 415 | `presentation_json_required`, `presentation_encoding_refused` |
| 422 | `presentation_revision_invalid`, `presentation_invalid` |
| 429 | `presentation_busy` |
| 503 | `presentation_unavailable` |

This route introduces no migration, dependency, feature flag, primitive,
framework credential or publication authority. It uses `artifact_export`.
Its isolated-worktree qualification is local evidence until the exact
Projection helper, client implementations and Deep composition are integrated
and qualified together. It does not establish release readiness.
