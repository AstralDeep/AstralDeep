# Connection `astral.connection/v1`

A connection lets the owner's devices reach an outside service, such as an MCP server that asks for an OAuth sign-in. The record says what the owner approved. Each device keeps its own credential and logs a small state event so the other devices can show "signed in on Mac". This version implements MCP over HTTP (kind `mcp-http`). Later kinds extend this document in place at `v: 1`.

Existing roster v2 stores keep working unchanged. This contract adds no event type and no roster field. It adds a `consent` field to some `settings.set` bodies, which deployed kernels accept and fold as an unread setting.

Reference code: `reference/connection.py` and `reference/connection_oauth.py`. Generator: `reference/generate_connection_vectors.py`. Vectors: `vectors/connection-v1.json` (keys, records, states, the fold, use, names and egress) and `vectors/connection-oauth-v1.json` (the adapter decisions of section 8). LETS-CJ/1, hashes, signatures and the consent object come from the preserved 105 reference. The name rule reads the caller ticket label table `reference/label_classes.json`.

## 1. Carrier

A deployed kernel refuses an unknown event type when it opens the header, and it never stores the refused event. Every later event of that feed then waits as a gap. One new type written by an upgraded Mac would stop an iPhone on an older build from receiving anything more from that Mac. So connections ride on `settings.set`, family memory, which every deployed kernel accepts.

| Logical event | `settings.set` body |
|---|---|
| put | `{"key": "connection/<id>", "value": <record>, "consent": <consent>}` |
| remove | `{"key": "connection/<id>", "value": null}` |
| state | `{"key": "connection.state/<id>/<device id>", "value": <state>}` |

Every connection event has scope `mesh` and the labels `{"confidentiality": "private", "integrity": "owner"}` exactly. Any other scope or label is `connection.malformed`.

Only `settings.set` carries connections. A `mem.remember` or `mem.supersede` event under a reserved key is not a connection event, so the fold never reads it, and writers refuse to write one. Deployed kernels fold the connection keys as ordinary settings that no code reads. Their settings and authority folds give the same results for every other key, with or without connection events.

## 2. Keys

Every `settings.set` key that starts with `connection/` or `connection.` is reserved. This version defines two forms.

| Form | Grammar |
|---|---|
| record | `connection/` then 32 lowercase hex digits |
| state | `connection.state/` then 32 lowercase hex digits, `/` and a device id (`sha256:` and 64 lowercase hex digits) |

The 32 hex digits are the connection id, drawn from 16 random bytes at creation. Any other reserved key, such as an uppercase id, a missing device or an unknown family like `connection.card/<id>`, is `connection.malformed`. Keys such as `connection`, `connections/<id>` and `theme` are not reserved and the fold does not read them.

## 3. Record

### 3.1 The common part

Every reader checks these rules for every kind and every later version. A put whose value fails one is `connection.malformed` and does not count in the fold.

| Field | Rule |
|---|---|
| `v` | An integer of at least 1. A boolean is not an integer |
| `id` | Equal to the id in the key |
| `name` | 1 to 80 Unicode code points typed or confirmed by the owner. A code point that the caller ticket label table refuses (Unicode 16.0 categories Cc, Cf, Cs, Co and Cn) is refused here too, so a name cannot hide a control or a direction override |
| `kind` | Matches `[a-z][a-z0-9-]{0,31}` |

The value is a JSON object whose LETS-CJ/1 bytes are at most 32768 bytes. The record holds no token, client secret or pasted key.

### 3.2 MCP over HTTP

A record of kind `mcp-http` at `v: 1` has exactly these fields.

| Field | Rule |
|---|---|
| `v`, `id`, `name`, `kind` | Section 3.1, with `kind` equal to `mcp-http` |
| `endpoint` | The server address, a URL in the canonical form of section 3.3 |
| `local` | A boolean. True when the owner added a loopback server |
| `device` | When `local` is true, the device id of the device that runs the calls. Otherwise null |
| `auth` | An object with exactly `scheme`, `issuer`, `scopes`, `resource` and `client_id` |
| `tools` | 0 to 128 objects with exactly `name` and `hash`, sorted by `name` with no repeats |
| `flow` | `private`, or `phi` when the owner marks the connection a PHI source |
| `egress` | 1 to 16 entries of section 3.4 |

`auth.scheme` is `oauth`, `none` or `key`.

- With `oauth`, `issuer` is the authorization server's issuer URL (section 3.3), `resource` equals `endpoint`, `scopes` is the approved scope list and `client_id` is null or a client id the owner entered.
- With `none`, `issuer` and `resource` are null, `scopes` is empty and `client_id` is null.
- A scope matches the RFC 6749 scope token, 1 to 256 characters from `%x21`, `%x23-5B` and `%x5D-7E`. `scopes` holds at most 32, sorted with no repeats.
- A `client_id` is 1 to 1024 characters from `%x21-7E`.
- A tool `name` is 1 to 128 characters of `[A-Za-z0-9_.-]`, the MCP tool name characters. A name that cannot be offered to a model (section 7.2) is still a valid record entry. `hash` is the definition hash of 105 [tools.md](../../105-decentralized-personal-mesh/contracts/tools.md) section 2.

The pairing of `local`, `device` and `endpoint` holds both ways. `local` true needs a device id in `device` and a loopback endpoint. `local` false needs a null `device` and an HTTPS endpoint on a DNS name. The device named in a local record may be any member, a node included. The consent still comes from a device that is not a node (section 6.1).

The first put of a server that answers `401` before sign-in has empty `tools`. After a device signs in, it lists the tools in its state (section 4) and the host shows a review card. A consented put then adds them. Any change to any field is a new put with a new consent.

### 3.3 URLs and hosts

Every URL in a record must already be canonical. A reader compares strings and refuses any other spelling. It never normalizes.

- At most 2048 characters, all from `%x21-7E`.
- The scheme is `https`, or `http` only for a loopback host.
- The host is a DNS name or one of the two loopback literals, `127.0.0.1` and `[::1]`. A DNS name is lowercase ASCII of at most 253 characters with at least two labels. Each label matches `[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?`. The last label is neither all digits nor `0x` followed by hex digits. This is the host rule of [exec-policy.md](exec-policy.md) section 1, so every other IP literal, `localhost`, a trailing dot, an underscore or a wildcard is refused. Write Unicode names in punycode.
- A port, when present, is `1` to `65535` with no leading zero, and never the scheme's default (443 for `https`, 80 for `http`).
- No userinfo, query or fragment.

An `endpoint` path is empty or one or more `/segment` parts. A segment is one or more RFC 3986 `pchar` characters with each percent escape written as `%` and two uppercase hex digits. A segment is never `.` or `..`, and there is no trailing slash. An issuer path follows the same rule and may also end in one `/`, because some issuers end that way and the issuer is compared exactly.

A loopback host is allowed only in a local record. Private LAN addresses stay refused, because they are IP literals. A record that breaks any rule of sections 3.2 to 3.4 is listed unsupported with `connection.record_unsupported` (section 3.5).

### 3.4 Egress

An egress entry is `host` or `host:port`. `host` alone means port 443, so `host:443` is not a canonical spelling and is refused. Other ports follow the port rule of section 3.3. A loopback literal may appear only in a local record. IPv6 loopback is written `[::1]:8765`.

`egress` is sorted by host as text and then by port as a number, with no repeats. So `mcp.research.example:8443` comes before `mcp.research.example.cdn`. Containment compares `(host, port)` pairs, and because entries are canonical, equal pairs are equal strings.

A URL's entry is its host when the port is 443 (written or implied by `https`), and `host:port` otherwise, so `http://127.0.0.1/mcp` gives `127.0.0.1:80`. The reader checks that `egress` holds the endpoint's entry and, with `oauth`, the issuer's entry. A writer also lists the host of every authorization-server endpoint it found in metadata (authorization, token and registration). The reader cannot see those, and the device refuses any fetch outside `egress` (section 7.3).

Each entry maps onto an exec policy `hosts` object. `host` gives `{"host": host, "port": 443}` and `host:port` gives `{"host": host, "port": port}`. Exec policy v1 accepts the result only for a DNS name with port 80 or 443. An entry with another port or a loopback literal has no exec form, and a backend that needs one refuses to launch.

### 3.5 Unsupported records

A put that passes section 3.1 and the consent checks of section 6.1 counts in the fold even when the reader cannot use the record. The reader lists it as unsupported, never uses it and never rewrites it. A later value that a reader cannot read must not leave that reader using an older one.

The first matching row gives the reason.

| Order | Condition | Reason |
|---|---|---|
| 1 | `v` is not 1 | `connection.version_unsupported` |
| 2 | The reader does not implement `kind` | `connection.kind_unsupported` |
| 3 | The fields differ from the kind's field set, or `auth` is not an object with exactly its five fields and a text `scheme` | `connection.record_unsupported` |
| 4 | The reader does not implement `auth.scheme` for this kind | `connection.scheme_unsupported` |
| 5 | Any rule of sections 3.2 to 3.4 fails | `connection.record_unsupported` |
| 6 | Another listed connection keeps the server id (section 6.5) | `connection.server_id_collision` |

This version implements kind `mcp-http` with the schemes `oauth` and `none`. A record with scheme `key` or any other scheme is listed with `connection.scheme_unsupported` before the reader looks at the scheme's fields. Row 6 replaces only an `ok` result, so a record that is already unsupported keeps its own reason.

The contract carries no server-declared confidentiality hint in this version. `flow` is the owner's choice.

## 4. State

A device writes its own state for a connection only on a transition. The value has exactly these fields, plus an optional `tools`.

| Field | Rule |
|---|---|
| `v` | The integer 1 |
| `state` | `not_signed_in`, `signed_in` or `needs_attention` |
| `reason` | With `needs_attention`, one of `refresh_refused`, `expired`, `scope` and `refused`. Otherwise null |
| `issuer` | Null, or an issuer URL of section 3.3 (a loopback issuer is allowed) |
| `ms` | The transition time in milliseconds, a non-negative integer |
| `tools` | Only with `signed_in`. 0 to 128 tool entries of section 3.2, the tools the device listed after it signed in |

A device with no state event reads as `not_signed_in`. A device that deletes its credential, for example after a removal syncs in, writes `not_signed_in`. Any other shape, including a state or reason this version does not define, is `connection.malformed`, and that device's older state stands. States are shown and never authorize anything.

`tools` is how the owner reviews the tools of a server that only a node can reach. The node signs in, writes its state with the tools, and an owner device draws the review card from it. A call still checks each tool's hash against the record.

## 5. Consent

A put carries the 105 consent object `{v, kind, digest, device, clock, sig}` with `kind` equal to `connection`. `digest` is `sha256:` and the lowercase hex SHA-256 of the LETS-CJ/1 bytes of `value`. `sig` is the consenting device's presence key P-256 low-S signature over `astral.consent/v1\n` followed by the LETS-CJ/1 bytes of the consent without `sig`, as in 105 [policy.md](../../105-decentralized-personal-mesh/contracts/policy.md) section 2.

The kernel draws the presence prompt from the record and never from model output. Its consent kind for this prompt is new (`ConsentKind::Connection`). A node never signs it, and Deep's node signer, which signs only the other kind, refuses it with no change. The consent is accepted for 10 minutes by the event's own clock: `event.clock.ms - 600000 <= consent.clock.ms <= event.clock.ms`.

## 6. Fold

The fold is a pure function of the events, the same on every peer. It does not depend on the device that computes it.

| Input | Meaning |
|---|---|
| `members` | Current members, each with `kind`, `presence` (base64url of the 65-byte key) and `feeds` |
| `removed` | Removed members in the same shape |
| `events` | Decoded event summaries with `id`, `feed`, `seq`, `clock`, `after`, `type`, `scope`, `labels` and `body`, the 105 fold shape plus scope and labels |
| `legacy_servers` | The names of the remote MCP servers the device already holds from before connections |

For each connection id the winner is the latest valid put or remove by `(clock.ms, clock.n, feed)`, as in 105 [event.md](../../105-decentralized-personal-mesh/contracts/event.md) section 10.4. An invalid event never wins. It is listed in `ignored` with its code. A remove at the head means the connection is not listed.

### 6.1 A put

The first failure wins, in this order.

| Step | Check | Code |
|---|---|---|
| 1 | Scope and labels (section 1), the key (section 2), a body of `key`, a non-null `value` and no field other than `consent`, and the common part of the value (section 3.1) | `connection.malformed` |
| 2 | The body has a `consent` | `connection.consent_missing` |
| 3 | The consent has exactly its six fields, `v` 1, text `kind`, `digest`, `device` and `sig`, and a clock of two non-negative integers | `connection.malformed` |
| 4 | `kind` is `connection` and `digest` is the digest of `value` | `connection.consent_wrong_action` |
| 5 | `device` is a current member | `connection.consent_unknown_device` |
| 6 | That member's roster kind is not `node` | `connection.consent_node_device` |
| 7 | The event's own feed is one of that member's feeds | `connection.consent_foreign_feed` |
| 8 | The signature verifies against that member's presence key, low-S only | `connection.consent_bad_signature` |
| 9 | The consent is inside the 10-minute window of section 5 | `connection.consent_stale` |

Step 7 stops any other member, a node included, from carrying an older consented value again later to roll a change back. Steps 5 and 6 cannot fail together, so the vectors pin step 4 before step 6 directly.

### 6.2 A remove

A remove needs no consent. Its body is exactly `key` and a null `value`, otherwise `connection.malformed`. A body that also carries a consent is malformed. Its feed must belong to a current member, otherwise `connection.writer_not_member`.

### 6.3 A state

The first failure wins.

| Step | Check | Code |
|---|---|---|
| 1 | Scope and labels, the key, a body of exactly `key` and `value`, and the state rules of section 4 | `connection.malformed` |
| 2 | The writer's feed belongs to a current member | `connection.writer_not_member` |
| 3 | That member is the device named in the key | `connection.state_foreign` |

For each connection and device the latest valid state wins. States attach only to listed connections. A state for an id with no listed connection is dropped without a code.

### 6.4 Removed members

The fold reads current members only, as the authority fold reads only their presence keys. A put whose consenting device was later removed fails step 5. An older valid put for the same id stands, as an older consented grant does. The fold also lists it in `removed_approvals` when the removed device really approved it: its feed is that device's feed, the signature verifies against that device's last presence key, the window holds, and it is later than the connection's winner or the connection has none. The host shows "approved by a removed device" until the owner approves it again from another device.

A remove or a state written by a device that was later removed is `connection.writer_not_member`. So a connection that a removed device removed comes back with the last value a current member approved. That value was consented by a current member that is not a node, and every device whose credential was deleted reads `not_signed_in`. The vector `fold_removal_by_removed_device_ignored` pins this.

### 6.5 Output

```
{
  "connections": [{"id", "server", "event", "approved_by", "record", "status", "reason", "states"}],
  "removed_approvals": [{"id", "event", "device", "record"}],
  "ignored": {"<event id>": "<code>"}
}
```

`connections` and `removed_approvals` are sorted by id. `status` is `ok` or `unsupported`, and `reason` is null or a code of section 3.5. `states` maps each device id to its latest valid state.

Inside turns the server id of a connection is `c` and the first 8 hex digits of its id. Two connections can share it when two devices create them at once. Among listed connections the lowest full id keeps the server id, and every later one is listed unsupported with `connection.server_id_collision`. A legacy server whose name equals a server id keeps the name, and the connection is listed the same way. Every listed connection takes part, whatever its status, so all readers agree on who holds a name. The kernel draws a new id at creation when the server id is already taken, and refuses a new legacy server name of the form `c` and 8 hex digits.

## 7. Use on a device

### 7.1 Where a call runs

A device uses a connection only when its fold status is `ok`. Otherwise the call fails with the reason. A local record runs only on the device it names. Any other device refuses with `connection.local_elsewhere`. That device may be a node, which is how a node reaches a loopback server the owner added for it.

### 7.2 Tool names

A model sees `<server id>__<tool name>`, the form the kernel already uses. The prefix takes 11 characters, so a tool name has 53. A name longer than 64 characters in all is not offered (`tool.name_too_long`). A name with a character outside `[A-Za-z0-9_-]`, such as the `.` that MCP allows, is not offered (`tool.name_invalid`). Length is checked first. The gate refuses a tool whose listed hash differs from the record with `gate.tool_def_changed`, and the review card of section 3.2 handles it.

A connection that offers one tool named after the connection uses `ask_<slug>`. The slug comes from the record name, using ASCII only, so no runtime Unicode data takes part.

1. Lowercase `A` to `Z`.
2. Keep `a` to `z` and `0` to `9`. Every other code point, including `-`, `_` and every non-ASCII letter, separates words.
3. Join the words with one `_`.
4. Cut to 49 characters, then remove a trailing `_`.
5. An empty slug is `agent`.

`ask_` and 49 characters make 53, so the name always fits.

### 7.3 Egress

A device fetches a URL only when it uses `https`, or `http` or `https` to a loopback literal, and when its entry (section 3.4) is in the record's `egress`. Otherwise it refuses with `connection.egress_refused`. The URL must be canonical in scheme, host and port, and may carry a path and query. Userinfo and fragments are refused.

## 8. Adapter decisions for MCP over HTTP

These decisions are pure functions over results the device already fetched. A fetch result is `{"status": <int>, "json": <parsed body>}`, with `json` present only when the body parsed as JSON. A URL missing from the results did not answer. Every fetch has redirects off, so a `3xx` is an answer that is not metadata.

### 8.1 The challenge

The device first calls the endpoint with no token. On `401` it reads `WWW-Authenticate` by RFC 9110 section 11.6.1. Scheme and parameter names match without regard to case, parameter names are reported in lowercase and a value is a token or a quoted string with `\` escapes. A `token68` after a scheme is skipped. The parameters of the first `Bearer` challenge are used. A header with a character outside tab and `%x20-7E`, an unterminated quote, a repeated parameter, or a parameter with no scheme before it is malformed and reads as no challenge.

### 8.2 Resource metadata URLs

Let `base` be the endpoint's origin followed by `/.well-known/oauth-protected-resource`. When the challenge has `resource_metadata`, it must be exactly `base`, or `base` then `/` and a path of the endpoint rule of section 3.3. That keeps it on the endpoint's origin in the same spelling, with no query, fragment, userinfo or dot segment. Otherwise the device refuses with `connection.resource_metadata_offsite`. A pinned URL is the only one tried. With no `resource_metadata` the device tries `base` followed by the endpoint path, then `base`, or only `base` when the endpoint has no path.

### 8.3 Resource metadata checks

The first candidate that answers `200` with a JSON object is the metadata. None gives `connection.no_resource_metadata`. Its `resource` must equal the endpoint exactly, else `connection.resource_mismatch`. From `authorization_servers` the device keeps each entry that is an issuer URL of section 3.3 (a loopback one only for a local record), in order and without repeats. None left gives `connection.no_authorization_server`. `scopes_supported` is kept only when every entry is a valid scope.

### 8.4 Authorization server metadata URLs

The path of the issuer is taken with any terminating `/` removed, as RFC 8414 says. With a path the device tries, in order, `<origin>/.well-known/oauth-authorization-server<path>`, `<origin>/.well-known/openid-configuration<path>` and `<origin><path>/.well-known/openid-configuration`. Without one it tries `<origin>/.well-known/oauth-authorization-server`, then `<origin>/.well-known/openid-configuration`.

### 8.5 Server selection

The device takes the servers in order. For each, it tries the URLs of section 8.4 until one answers `200` with a JSON object. The first such answer decides that server.

- Its `issuer` must equal the issuer used to find it, else the flow stops with `connection.issuer_mismatch`. A mismatched issuer is an integrity failure, so no later server is tried.
- When `code_challenge_methods_supported` is a list holding `S256`, the server is chosen and selection stops.
- A server whose metadata is missing, or that lacks `S256` (absent, empty or `plain` only), is skipped.

No server chosen gives `connection.as_unreachable` when no metadata fetch got any answer, and `connection.no_s256` when at least one did.

When the owner first adds the connection, selection runs over the listed servers and the chosen issuer goes into the record. At sign-in on any device, the device repeats discovery but signs in only with the record's issuer. The metadata must still list it, else `connection.issuer_changed`, and a new put with a new consent is needed. Then the same checks run on that one server.

### 8.6 Registration

The first branch that applies wins.

| Order | Condition | Branch |
|---|---|---|
| 1 | The record has a `client_id` | `record` |
| 2 | The metadata has `client_id_metadata_document_supported` equal to `true` | `metadata_document` |
| 3 | The metadata has a non-empty text `registration_endpoint` | `dynamic` |
| 4 | None of these | `connection.no_client_id`. The host asks the owner for a client id, which goes into the record by a new put |

Dynamic registration sends `{"application_type", "client_name": "Astral", "grant_types": ["authorization_code", "refresh_token"], "redirect_uris": [<redirect>], "response_types": ["code"], "token_endpoint_auth_method": "none"}` with `application_type` `native`. A `200` or `201` answer with a non-empty text `client_id` succeeds. Only a `400` to the `native` request is retried, once, with `web`. Any other outcome is `connection.registration_refused`, and the vectors list the requests sent. A dynamically registered client belongs to one device and lives in its keystore, so each device registers its own and reuses it.

### 8.7 Scopes

When the owner adds the connection, the scopes come from the `401` challenge's `scope`, else from `scopes_supported`, else none. A `scope` value is split on spaces, empty parts are dropped and the result is sorted with no repeats. A value with an invalid token counts as absent. More than 32 scopes gives `connection.too_many_scopes`. Sign-in always asks for the record's scopes.

### 8.8 The `iss` check

The device checks the redirect before it sends any token request, by the RFC 9207 table. "Advertised" means the metadata has `authorization_response_iss_parameter_supported` equal to `true`. Equality is exact string equality with the record's issuer.

| Advertised | `iss` in the redirect | Result |
|---|---|---|
| yes | absent | `connection.iss_missing` |
| yes | equal | accepted |
| yes | different | `connection.iss_mismatch` |
| no | absent | accepted |
| no | equal | accepted |
| no | different | `connection.iss_mismatch` |

A repeated `iss` is `connection.iss_mismatch`. On a refusal the device discards the code. The check also applies to an error response.

### 8.9 Requests

The device sends `resource` set to the endpoint in the authorization, token and refresh requests. It sends the bearer token only in the `Authorization` header and only to the endpoint's origin, compared as the exact origin string followed by the end, `/` or `?`. Any other host, port, scheme or spelling is `connection.token_passthrough`.

### 8.10 Refresh and step-up

On a `401` with a credential the device refreshes once and retries once. A refused refresh gives `needs_attention` with reason `refresh_refused`, no refresh token gives reason `expired`, and a `401` after the retry gives reason `refused`. Each fails the call.

A `403` whose `Bearer` challenge has `error` equal to `insufficient_scope` starts a step-up. Any other `403` is `connection.refused`. The step-up is the union of the record's scopes and the challenge's scopes, put again by a reviewed record put and then a sign-in. A union equal to the record's scopes needs no put and is a sign-in alone. A device allows one step-up between successful calls, so a second one before any call succeeds is `connection.step_up_limit`. The limit is checked before the union. A union of more than 32 scopes is `connection.too_many_scopes` and does not count as a step-up. Until the step-up completes the device logs `needs_attention` with reason `scope` and fails the call.

## 9. Codes

| Code | Where |
|---|---|
| `connection.malformed` | Fold, any event |
| `connection.consent_missing`, `connection.consent_wrong_action`, `connection.consent_unknown_device`, `connection.consent_node_device`, `connection.consent_foreign_feed`, `connection.consent_bad_signature`, `connection.consent_stale` | Fold, a put |
| `connection.writer_not_member` | Fold, a remove or a state |
| `connection.state_foreign` | Fold, a state |
| `connection.version_unsupported`, `connection.kind_unsupported`, `connection.record_unsupported`, `connection.scheme_unsupported`, `connection.server_id_collision` | Fold, the reason of an unsupported record |
| `connection.local_elsewhere`, `connection.egress_refused`, `tool.name_too_long`, `tool.name_invalid` | Use on a device |
| `connection.resource_metadata_offsite`, `connection.no_resource_metadata`, `connection.resource_mismatch`, `connection.no_authorization_server`, `connection.issuer_mismatch`, `connection.issuer_changed`, `connection.no_s256`, `connection.as_unreachable` | Discovery |
| `connection.no_client_id`, `connection.registration_refused` | Registration |
| `connection.too_many_scopes`, `connection.iss_missing`, `connection.iss_mismatch`, `connection.token_passthrough`, `connection.refused`, `connection.step_up_limit` | Sign-in and calls |

## 10. Existing data and mixed versions

Existing data:

| Change | Existing data | Handling |
|---|---|---|
| `settings.set` connection keys | v2 stores on Mac 97, iPhone 99 and nodes | Additive. The settings and authority folds give identical results for all other keys |
| Kernel meta `connection.credential/*` | v2 stores | Additive. Absent means not signed in. Not in backups |
| `remote.mcp.*` items | Mac 97, iPhone 99 | Unchanged and still offered in turns. Listed as "Added before connections". A new legacy name of the form `c` and 8 hex digits is refused |
| `model.provider.*` items | Mac 97, iPhone 99, nodes | Unchanged and used. Later key connections write the same format |
| Node `__remote_mcp_connections__` rows (personal) | Deployed personal nodes | Bytes unchanged, unused, reported |
| Same rows on server or institution profiles | Deployed servers | Unchanged and served |
| `remote-mcp-1` leaves the personal catalog | Grants naming its tools | Grants untouched and inert |
| Keychain service `connection-credentials-v1` | New. Existing services untouched | Items in `mesh-device-v2` unchanged |
| Client metadata republish | Servers holding the old document | Same `client_id`, lists only grow |

Mixed versions:

| New thing | Kernel `c2c1cc4` or the sign-in-free kernel | Connection kernel |
|---|---|---|
| Connection `settings.set` events | Accepted, folded as unread settings | Folded |
| Consent kind `connection` | Never read | Verified |
| `connection.key.<id>` items | Accepted, resealed to late devices when the audience is `all`, name unused | Accepted and resealed the same way, name unused |
| Model provider items written for model connections | Used as today | Used |
| Kinds `a2a`, `mcp-stdio` and `http-api` | Ignored | Listed unsupported |

A test with two real kernels proves the first row.

## 11. Vectors

Both files use the vector format of the roster v2 contract. `connection-v1.json` publishes `test_devices`, the roster v2 test devices `alice` (desktop), `bob` (pocket), `carol` (satellite) and `node` (node), the removed `dave` (desktop) and the stranger `mallory`, with their ids, feeds and presence scalars. They are public test values derived from the labels named there. Clocks are fixed and no case reads a wall clock or the network.

Case kinds in `connection-v1.json`:

- `connection_key` has `key` and expects the form or `connection.malformed`.
- `connection_record` has `id` and `value` and expects `{status, reason, digest, server}` or `connection.malformed`.
- `connection_state` has `value` and expects `{}` or `connection.malformed`.
- `connection_fold` has the inputs of section 6 and expects the output of section 6.5. Every fold case gives the same output for its events in any order.
- `connection_use` has a fold entry and a device id.
- `connection_exposed_name` has `id` and `tool`. `connection_ask_slug` has `name`.
- `connection_egress` has `egress` and `url`. `connection_exec_host` has `entry`.
- `connection_coexistence` has a log with ordinary and connection events and `presence_keys`, and expects the deployed authority fold and the deployed settings fold with and without the connection events.

Case kinds in `connection-oauth-v1.json`: `oauth_challenge`, `oauth_resource_metadata_urls`, `oauth_resource_metadata`, `oauth_as_metadata_urls`, `oauth_select_server` (with `record_issuer` null when the owner first adds the connection), `oauth_registration`, `oauth_dynamic_registration` (its expectation lists the requests in both outcomes), `oauth_scopes`, `oauth_iss`, `oauth_step_up_sequence` (steps of `forbidden` with a header or `success`) and `oauth_bearer_target`.

Groups of cases:

| Group | Cases |
|---|---|
| Keys | `key_*`, each form, the lookalikes that are not reserved and each malformed form |
| Record | `record_*`, every field rule, the size limit at 32768 and 32769 bytes, the limits at and one past 80, 128, 32 and 16, the unsupported reasons and `record_order_*` for their order |
| State | `state_*`, every state and reason, `tools` placement and limits |
| Put | valid, node consent, the window at 600000 and 600001 ms before and 0 and 1 ms after, wrong digest and kind, tampered and high-S signatures, re-carried values on another member's feed and on the node's feed, removed and unknown consenting devices, a forged removed approval, and `fold_order_*` for each adjacent pair of section 6.1 that can fail together |
| Remove | without consent, by the node, by a removed device, with a consent field, and a put after a removal |
| Winner | a later put, a later invalid put, an equal clock broken by feed, an equal millisecond broken by the counter, and arrival order |
| State fold | own device, another device, a removed member, the latest state, a later unreadable state, several devices, tools from a node, and a state for an unlisted connection |
| View | unknown kind, an unsupported put after a supported one, scheme `key`, server id collisions with an ok, an unsupported and a legacy holder, and a local record naming the node |
| Carrier | `mem.remember` under a reserved key, other events, a malformed reserved key, an unknown family, wrong labels and a chat scope |
| Use and names | `use_*`, `exposed_*` at 53 and 54 characters, `slug_*` |
| Egress | `egress_*` and `exec_*` |
| Coexistence | `coexistence_deployed_folds` |
| Adapter | `challenge_*`, `rm_*` including each near miss of the origin pin, `prm_*`, `as_urls_*`, `select_*` including the mixed unreachable cases and the record issuer, `reg_*`, `dyn_*`, `scopes_*`, the six `iss_*` rows and their spellings, `stepup_*` sequences and `bearer_*` |

Regenerate with `python specs/106-sign-in-free-clients/contracts/reference/generate_connection_vectors.py` and verify with `--check`, which writes nothing.

## 12. Versioning

A change to a rule, a field or a code raises the format version, except a new kind or scheme defined in place at `v: 1`, which older readers list as unsupported. This document, the vectors and the reference modules change together, and `generate_connection_vectors.py --check` and the reference tests pass before a change merges.
