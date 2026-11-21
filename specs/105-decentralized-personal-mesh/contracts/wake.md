# Device wake routes

A wake route delegates only a bounded, content-free push hint to a publisher gateway. A node never receives an APNs token or the publisher's Apple signing credential. Push is an optional sync hint: unavailable registration, an expired route or a lost background push does not prevent foreground or direct peer sync.

## Signed mesh record

`device.wake_route` belongs to the dedicated `device` event family. It has mesh scope and exactly Private/Owner labels. Its body is either `{"route":null}` to withdraw a route, or:

```json
{"route":{"publisher":"astral","capability":"opaque_url_safe_capability","expires_at_ms":1794441601000}}
```

The body has exactly one field; a non-null route has exactly the three shown. `astral` is the closed supported service identifier. The host resolves it through its configured HTTPS publisher endpoint; an event cannot supply a URL. A capability is nonempty ASCII `[A-Za-z0-9_-]+={0,2}`, at most 2048 bytes. It is opaque to the Kernel. Expiry is an integer from 1 through 9007199254740991, strictly after the event's signed clock and at most 86400000 milliseconds after it. Appending validates against the HLC chosen inside the actual log transaction; importing validates against the signed header clock. Invalid bodies raise `wake.malformed` without echoing token material.

The route contains no claimed mesh, author or device identity. The facade derives those from verified roster state and the signed header's current feed. A historical rotated feed is insufficient even if its older events remain valid. `set_wake_route(expected_mesh_id,expected_device_id,route)` checks exact expected identities before writing and reports `wake.target_mismatch` if they changed. Publication and use require current local authority, including pending-removal proof checks. A local withdrawal may proceed while halted; it grants no new authority. Removal, retired feeds and loss of current authority cannot authorize publication or use.

This opaque capability is an intentionally shared attenuated delegation, like a per-call ticket delivered to an authorized executor. It grants only rate-limited empty wakes, with no data access, arbitrary tool invocation or model access. Its Private/Owner event is encrypted within the mesh and excluded from chat, memory, model context and general plaintext-export/logging projections. Existing Secret credential sealing and sync-withholding rules remain unchanged. Capabilities must be redacted in host/debug records and errors. Encrypted mesh backup and transport preserve the ordinary signed feed.

## Fold

From verified mesh-scope events, consider only Private/Owner `device.wake_route` headers attributed to a current device's current operations feed. For each device choose the greatest `(clock.ms,clock.n,feed,seq)` **before** inspecting the body or expiry. A newest tombstone, unavailable/malformed body or expired route suppresses older delegations; none revives an old capability. Return readable valid routes whose expiry is strictly above the current time, sorted by device id. Unrelated, foreign, removed and retired feeds cannot replace a current device's route.

`wake_routes()` is a host-only interface. Its records add the current mesh and derived device ids. Callers recheck target/current context after awaits and before sending. A model-facing tool must not expose this interface.

## Publisher boundary

The publisher alone holds APNs credentials. Phone registration authenticates through normal Keycloak authority and binds the actual verified Kernel mesh/device, APNs destination, generation and expiry. It issues a random, opaque, revocable wake capability. Rotation and revocation use owner-isolated encrypted storage and compare-and-set preconditions. Wakes bind the exact mesh/device to the capability, contain no chat, prompt, PHI or notification content, and send only the existing APNs background `content-available` payload. The gateway maintains the per-destination minimum interval and hourly budget durably across concurrent workers, rotation and re-registration. The default is at least 300 seconds between sends and at most four sends per hour.

Nodes resolve only configured publisher services and use the approved bounded HTTPS egress path. Registration/account/mesh changes retire local publication and attempt remote revocation with the original authority; offline remote revocation is not instantaneous. Short expiry, signed withdrawal, removed-device filtering and local context checks remain effective without a gateway acknowledgment. Ordinary server notification enrollment/session rules remain separate. Missing publisher configuration or sign-in is reported as unavailable, never as a successful enrollment or wake.

## Shared vectors

`wake.json` uses `astral.vectors/v1`. `wake_route_validate` takes `body` and `signed_ms`, returning `{"route": object|null}` or `wake.malformed`. `wake_route_fold` takes `events`, verified `current_device_of_feed`, and `now_ms`, returning `{"routes":[{"device_id", "route"}]}`. Fold events carry `id`, `feed`, `seq`, `clock`, `type`, `labels`, `body`, and optional body `state` (default `present`). The vector input is an already verified mesh-scope projection; separate event vectors test header signature, scope and label denials. These pure vectors do not replace publisher authentication, native context-race or live APNs tests.
