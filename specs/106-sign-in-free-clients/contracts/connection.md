# Connection `astral.connection/v1`

A connection lets the owner's devices reach an outside service, such as an MCP server that asks for an OAuth sign-in. The record says what the owner approved. Each device keeps its own credential and logs a small state event so the other devices can show "signed in on Mac". The first reader implements MCP over HTTP (kind `mcp-http`). The kinds `a2a`, `mcp-stdio`, `http-api` and `model` extend this document in place at `v: 1` (sections 3.6 to 3.9 and 13). A reader that implements only `mcp-http` lists them unsupported, and a reader that implements them is called the extended reader here.

Existing roster v2 stores keep working unchanged. This contract adds no event type and no roster field. It adds a `consent` field to some `settings.set` bodies, which deployed kernels accept and fold as an unread setting.

Reference code: `reference/connection.py`, `reference/connection_oauth.py`, `reference/a2a_card.py` and `reference/credential_items.py`. Generators: `reference/generate_connection_vectors.py`, `reference/generate_a2a_vectors.py` and `reference/generate_credential_vectors.py`. Vectors: `vectors/connection-v1.json` (keys, records, states, the fold, use, names and egress), `vectors/connection-oauth-v1.json` (the adapter decisions of section 8), `vectors/a2a-card-v1.json` (the card decisions of section 13) and `vectors/credential-items-v1.json` (the item rules of [credential-items.md](credential-items.md)). LETS-CJ/1, hashes, signatures and the consent object come from the preserved 105 reference. The name rule reads the caller ticket label table `reference/label_classes.json`.

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
| `name` | 1 to 80 Unicode code points typed or confirmed by the owner. A code point that the caller ticket label table refuses (Unicode 16.0 categories Cc, Cf, Cs, Co and Cn) is refused here too, so a name cannot hide a control, a direction override or an invisible character. Cf includes U+200D, the zero width joiner, so an emoji joined by it is refused too |
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
| `flow` | `private`, or `phi` when the owner marks the connection a PHI source. The owner chooses it and a re-approval may set it to either value (section 3.5) |
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

An `endpoint` path is empty or one or more `/segment` parts. A segment is one or more RFC 3986 `pchar` characters with each percent escape written as `%` and two uppercase hex digits. A percent escape never encodes an unreserved character (`A` to `Z`, `a` to `z`, `0` to `9`, `-`, `.`, `_` and `~`), because URL parsers rewrite those. So each URL has exactly one spelling, and `%2E%2E` cannot hide a dot segment. An escape of a reserved character such as `%2F` stays. A segment is never `.` or `..`, and there is no trailing slash. An issuer path follows the same rule and may also end in one `/`, because some issuers end that way and the issuer is compared exactly.

A loopback host is allowed only in a local record. Private LAN addresses stay refused, because they are IP literals. A record that breaks any rule of sections 3.2 to 3.4 is listed unsupported with `connection.record_unsupported` (section 3.5).

### 3.4 Egress

An egress entry is `host` or `host:port`. `host` alone means port 443, so `host:443` is not a canonical spelling and is refused. Other ports follow the port rule of section 3.3. A loopback literal may appear only in a local record. IPv6 loopback is written `[::1]:8765`.

`egress` is sorted by host as text and then by port as a number, with no repeats. So `mcp.research.example:8443` comes before `mcp.research.example.cdn`. Containment compares `(host, port)` pairs, and because entries are canonical, equal pairs are equal strings.

A URL's entry is its host when the port is 443 (written or implied by `https`), and `host:port` otherwise, so `http://127.0.0.1/mcp` gives `127.0.0.1:80`. The reader checks that `egress` holds the endpoint's entry and, with `oauth`, the issuer's entry. A writer also lists the host of every authorization-server endpoint it found in metadata: authorization, token, registration and device authorization. The reader cannot see those, and the device refuses any fetch outside `egress` (section 7.3).

The writer takes each endpoint that is present and is text. Each must be a canonical fetch URL of section 7.3, with loopback only in a local record, otherwise the write fails with `connection.egress_refused`. A field that is not text is ignored, as the metadata checks of section 8 ignore it. The result is the endpoint's entry, the issuer's entry when there is one and one entry for each endpoint, sorted and without repeats. So an authorization server whose `device_authorization_endpoint` is on another host adds that host to `egress` before any device flow starts, and a device flow never fetches outside the record. The vectors `egress_entries_*` pin this.

Each entry maps onto an exec policy `hosts` object. `host` gives `{"host": host, "port": 443}` and `host:port` gives `{"host": host, "port": port}`. Exec policy v1 accepts the result only for a DNS name with port 80 or 443. An entry with another port or a loopback literal has no exec form, and a backend that needs one refuses to launch. An entry that is not canonical, such as `host:443`, has no mapping and is `connection.egress_refused`.

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

The first reader implements kind `mcp-http` with the schemes `oauth` and `none`. A record with scheme `key` or any other scheme is listed with `connection.scheme_unsupported` before the reader looks at the scheme's fields. The extended reader implements these kinds and schemes.

| Kind | Schemes the extended reader implements | Fields |
|---|---|---|
| `mcp-http` | `oauth`, `none`, `key` | Section 3.2 |
| `a2a` | `none`, `key`, `oauth` | Section 3.6 |
| `mcp-stdio` | none, the record has no `auth` | Section 3.7 |
| `http-api` | none, the record has no `auth` | Section 3.8 |
| `model` | none, the record has no `auth` | Section 3.9 |

Row 3 uses the field set of the kind and checks `auth` only for a kind that has it. Row 4 applies only to a kind that has `auth`. Row 6 replaces only an `ok` result, so a record that is already unsupported keeps its own reason. A reader that implements only `mcp-http` lists every record of the four new kinds with `connection.kind_unsupported`, and the vectors `record_phase2_*` and `fold_phase2_lists_every_new_kind_unsupported` pin that.

The contract carries no server-declared confidentiality hint. `flow` is the owner's choice. A re-approval may lower a connection's `flow` from `phi` to `private` as well as raise it, and nothing limits it. The check `connection.flow_below_hint` was removed and no code or vector refers to it.

### 3.6 A2A agents

A record of kind `a2a` at `v: 1` has exactly these fields.

| Field | Rule |
|---|---|
| `v`, `id`, `name`, `kind` | Section 3.1, with `kind` equal to `a2a` |
| `card_url` | The address of the agent card. An HTTPS URL on a DNS name in the endpoint form of section 3.3, so no query |
| `card` | An object with exactly `hash`, `origin`, `signer` and `extended_hash` |
| `auth` | An object with exactly `scheme`, `issuer`, `scopes`, `resource` and `client_id` |
| `tools` | Empty, or one entry of section 3.2 named `ask_` and the slug of the record name (section 7.2). The hash is the card tool hash of section 13.6 |
| `flow` | `private` or `phi` |
| `egress` | 1 to 16 entries of section 3.4 |

The `card` object pins what the owner approved.

- `hash` is the card hash of section 13.2.
- `origin` is the origin of `card_url`, written as `https://` and the host with its port when it is not 443.
- `signer` is null for an unsigned card, and for a signed card the RFC 7638 thumbprint of the signing key, 43 characters of `[A-Za-z0-9_-]`.
- `extended_hash` is null, or the card hash of the extended card the owner reviewed (section 13.5).

`auth.scheme` is `none`, `key` or `oauth`. With `none` and `key`, `issuer`, `resource` and `client_id` are null and `scopes` is empty. A pasted key covers the card's `apiKey`, HTTP `bearer` and HTTP `basic` schemes, and the card says which one and where it goes. With `oauth`, `issuer` is the HTTPS issuer URL of the authorization server the owner approved and a loopback issuer is refused. For a card that names an `oauth2MetadataUrl` or an `openIdConnectUrl` the issuer is the one that document names. For a card that names only endpoints it is the origin of the token URL. `resource` is null, because A2A has no resource indicator. `scopes` and `client_id` follow section 3.2. The sign-in flow is `authorizationCode` on every device, `deviceCode` on a node only, and OpenID Connect through its discovery document, as section 13.3 says.

`egress` holds the entry of `card_url` and, with `oauth`, the issuer's entry. The writer also lists the entry of every interface URL in the card and the host of every authorization endpoint the card or the issuer names, with the device authorization host as section 3.4 says. The reader cannot see those. The device refuses a card whose interface is outside `egress` (section 13.4).

An A2A record has no `local` and no `device`. Any device that holds the credential may use it.

### 3.7 Local stdio servers

A record of kind `mcp-stdio` at `v: 1` has exactly these fields.

| Field | Rule |
|---|---|
| `v`, `id`, `name`, `kind` | Section 3.1, with `kind` equal to `mcp-stdio` |
| `command` | 1 to 1024 code points that the caller ticket label table accepts, so no control, format or unassigned code point |
| `args` | 0 to 64 texts of 0 to 4096 code points each, under the same code point rule |
| `env` | 0 to 32 names, each `[A-Z_][A-Z0-9_]{0,63}`, sorted with no repeats. `PATH` and every name that starts with `LD_` or `DYLD_` is refused, so a key can never become a library injection |
| `device` | The device id of the one device that runs the server. A node may be named |
| `tools` | 0 to 128 tool entries of section 3.2 |
| `flow` | `private` or `phi` |
| `egress` | 0 to 16 entries of section 3.4, with no loopback literal. Empty means the server may reach no host |

The record holds the names of the environment variables and never their values. The values are the secrets of the key item `connection.key.<id>` (section 3.10). The device starts the server only on first use and injects the environment at that moment, so no call of the binding returns a secret. Only the device named in `device` runs the server. Any other device refuses with `connection.local_elsewhere`, and an iPhone shows "runs on your Mac".

### 3.8 HTTP APIs

A record of kind `http-api` at `v: 1` has exactly these fields.

| Field | Rule |
|---|---|
| `v`, `id`, `name`, `kind` | Section 3.1, with `kind` equal to `http-api` |
| `endpoint` | The base address, an HTTPS URL on a DNS name in the canonical form of section 3.3 |
| `key` | An object with exactly `item`, `header` and `format` |
| `tools` | 0 to 128 tool entries of section 3.2, the operations the owner reviewed |
| `flow` | `private` or `phi` |
| `egress` | 1 to 16 entries of section 3.4 that include the endpoint's entry |

`key.item` is `connection.key.` and the record id. `key.header` is the name of the header that carries the key, 1 to 64 characters of `[A-Za-z][A-Za-z0-9-]`. It is never one of `host`, `content-length`, `content-type`, `transfer-encoding`, `connection`, `upgrade`, `te`, `trailer`, `cookie`, `set-cookie` or `proxy-authorization`, compared without regard to case. `key.format` is 1 to 64 printable ASCII characters (`%x20-7E`) holding the text `{key}` exactly once, for example `Bearer {key}` or `{key}`. The device sends the header only to the record's endpoint host (section 3.10).

### 3.9 Model providers

A record of kind `model` at `v: 1` pairs a record with a provider item in today's `model.provider.<name>` format. It has exactly these fields.

| Field | Rule |
|---|---|
| `v`, `id`, `name`, `kind` | Section 3.1, with `kind` equal to `model` |
| `endpoint` | The provider address, an HTTPS URL on a DNS name in the canonical form of section 3.3 |
| `key` | An object with exactly `item`, which is `model.provider.` and a name of `[a-z0-9][a-z0-9._-]*`, at most 128 characters in all |
| `flow` | `private` or `phi` |
| `egress` | 1 to 16 entries of section 3.4 that include the endpoint's entry |

The provider item keeps the endpoint, models and keys it has today and stays valid as written. A device on a kernel from before connections keeps using a provider item that an upgraded device wrote, and it ignores the record.

### 3.10 Keys

Keys for `http-api`, for `mcp-http` with scheme `key`, for A2A with scheme `key` and for the environment of a stdio server are `key.credential` items named `connection.key.<id>`, as [credential-items.md](credential-items.md) defines. The record names them and never holds a key. An item is sealed to every device by default or to the list the owner picked. A device that holds no box for the item reports `needs_attention` with reason `not_signed_in` and fails the call.

A key leaves a device only inside a request to the host of the record. The `http-api` key goes in `key.header` to the endpoint host. An `mcp-http` key goes in the `Authorization` header to the endpoint's exact origin as section 8.9 says. An A2A key goes where the card's scheme puts it and only to a host in `egress` that the approved card lists as an interface. The stdio environment goes only to the server process. No key appears in an event, a log line, an error text, a return value of the binding or any model input.

## 4. State

A device writes its own state for a connection only on a transition. The value has exactly these fields, plus an optional `tools`.

| Field | Rule |
|---|---|
| `v` | The integer 1 |
| `state` | `not_signed_in`, `signed_in` or `needs_attention` |
| `reason` | With `needs_attention`, one of `refresh_refused`, `expired`, `scope`, `refused` and `not_signed_in`. Otherwise null |
| `issuer` | Null, or an issuer URL of section 3.3 (a loopback issuer is allowed) |
| `ms` | The transition time in milliseconds, a non-negative integer |
| `tools` | Only with `signed_in`. 0 to 128 tool entries of section 3.2, the tools the device listed after it signed in |

States are strict where records are tolerant. An unknown, missing or out-of-range field makes a state `connection.malformed`, because a state is display only and written by one device about itself, so a reader that cannot read it loses only a status line. A record stays tolerant (section 3.5), because a reader that dropped an unreadable record would fall back to an older one and use it.

A device with no state event reads as `not_signed_in`. A device that holds no stored credential for a connection that needs one writes `needs_attention` with reason `not_signed_in` when a call finds none. That covers a key item with no box for the device. The reason `expired` is only for a credential that was held and has run out (section 8.10). A device that deletes its credential, for example after a removal syncs in, writes `not_signed_in`. Any other shape, including a state or reason this version does not define, is `connection.malformed`, and that device's older state stands. States are shown and never authorize anything.

`tools` is how the owner reviews the tools of a server that only a node can reach. The node signs in, writes its state with the tools, and an owner device draws the review card from it. A call still checks each tool's hash against the record.

## 5. Consent

A put carries the 105 consent object `{v, kind, digest, device, clock, sig}` with `kind` equal to `connection`. `digest` is `sha256:` and the lowercase hex SHA-256 of the LETS-CJ/1 bytes of `value`. `sig` is the consenting device's presence key P-256 low-S signature over `astral.consent/v1\n` followed by the LETS-CJ/1 bytes of the consent without `sig`, as in 105 [policy.md](../../105-decentralized-personal-mesh/contracts/policy.md) section 2.

The kernel draws the presence prompt from the record and never from model output. Its consent kind for this prompt is new (`ConsentKind::Connection`). A node never signs it, and Deep's node signer, which signs only the other kind, refuses it with no change. The consent is accepted for 10 minutes by the event's own clock: `event.clock.ms - 600000 <= consent.clock.ms <= event.clock.ms`.

## 6. Fold

The winner of each id, `ignored`, `removed_approvals` and `states` are a pure function of the events and the roster, the same on every peer. `status` and `reason` also depend on the kinds and schemes the reader implements and on the device's own legacy server names.

| Input | Meaning |
|---|---|
| `members` | Current members, each with `kind`, `presence` (base64url of the 65-byte key) and `feeds`, every feed the device has written from. A device can have more than one |
| `removed` | Removed members in the same shape |
| `events` | Decoded summaries of the events the log accepted, with `id`, `feed`, `seq`, `clock`, `after`, `type`, `scope`, `labels` and `body`, the 105 fold shape plus scope and labels. A removed device's events past its removal cutoff never reach the fold, because the log refuses them with `feed.removed` (105 event.md section 8) |
| `legacy_servers` | The names of the remote MCP servers the device already holds from before connections |

For each connection id the winner is the latest valid put or remove by `(clock.ms, clock.n, feed, seq)`, the order key of the 105 folds ([event.md](../../105-decentralized-personal-mesh/contracts/event.md) section 10). Within one feed the clock rises, so `seq` never decides between two events that differ in clock. An invalid event never wins. It is listed in `ignored` with its code. A remove at the head means the connection is not listed.

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
| 10 | No earlier accepted put used the same consent, compared on every consent field except `sig` | `connection.consent_reused` |

Step 7 stops any other member, a node included, from carrying an older consented value again later to roll a change back. Step 10 counts each consent once, so the same device cannot repeat its own older put inside the 10-minute window either. "Earlier" is the order key, and a consent stays used after its put is superseded or removed. Steps 5 and 6 cannot fail together, so the vectors pin step 4 before step 6 directly.

### 6.2 A remove

A remove needs no consent. Its body is exactly `key` and a null `value`, otherwise `connection.malformed`. A body that also carries a consent is malformed. Its feed must belong to a current member or a removed one, otherwise `connection.writer_not_member` (section 6.4).

### 6.3 A state

The first failure wins.

| Step | Check | Code |
|---|---|---|
| 1 | Scope and labels, the key, a body of exactly `key` and `value`, and the state rules of section 4 | `connection.malformed` |
| 2 | The writer's feed belongs to a current member | `connection.writer_not_member` |
| 3 | That member is the device named in the key | `connection.state_foreign` |

For each connection and device the latest valid state wins. States attach only to listed connections. A state for an id with no listed connection is dropped without a code.

### 6.4 Removed members

Puts and removes treat a removed device differently, on purpose. Removing access is the safe direction.

- A put counts only while its consenting device is a current member, as the authority fold reads only current presence keys. A put whose consenting device was later removed fails step 5.
- A remove that was valid when written stands even after its writer is removed. A removed device's feed holds only the events it wrote before its removal cutoff, so the fold reads removes from the feeds of current and removed members. An owner who removed a connection on a phone and later removed the lost phone does not see the connection come back. A remove from a feed that no roster device ever owned is still `connection.writer_not_member`.
- A state counts only from a current member, because a removed device holds no credential the mesh should show.

For a put whose approver was removed, an older valid put for the same id stands, as an older consented grant does. The fold also lists the put in `removed_approvals` when the removed device really approved it: its feed is that device's feed, the signature verifies against that device's last presence key, the window holds, and it is later than the connection's winner or the connection has none. The host shows "approved by a removed device" until the owner approves it again from another device.

The vectors `fold_put_consenting_device_removed` and `fold_removal_by_removed_device_stands` pin the two sides, and `fold_state_of_removed_member` pins the state rule.

### 6.5 Output

```
{
  "connections": [{"id", "server", "event", "approved_by", "record", "status", "reason", "states"}],
  "removed_approvals": [{"id", "event", "device", "record"}],
  "ignored": {"<event id>": "<code>"}
}
```

`connections` and `removed_approvals` are sorted by id. `status` is `ok` or `unsupported`, and `reason` is null or a code of section 3.5. `states` maps each device id to its latest valid state.

Inside turns the server id of a connection is `c` and the first 8 hex digits of its id. Two connections can share it when two devices create them at once. Among listed connections the lowest full id keeps the server id, and every later one is listed unsupported with `connection.server_id_collision`. A legacy server whose name equals a server id keeps the name, and the connection is listed the same way. Every listed connection takes part, whatever its status, so two readers with the same legacy server names agree on who holds a name. The kernel draws a new id at creation when the server id is already taken, and refuses a new legacy server name of the form `c` and 8 hex digits.

## 7. Use on a device

### 7.1 Where a call runs

A device uses a connection only when its fold status is `ok`. Otherwise the call fails with the reason. A local record, and a stdio record, runs only on the device it names. Any other device refuses with `connection.local_elsewhere`. That device may be a node, which is how a node reaches a loopback server the owner added for it.

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

A device fetches a URL only when it uses `https`, or `http` or `https` to a loopback literal, and when its entry (section 3.4) is in the record's `egress`. Otherwise it refuses with `connection.egress_refused`. The URL must be canonical in scheme, host and port, and may carry a path and query. Userinfo and fragments are refused. The check then continues in the egress resolver. For a record that is not local, a name that resolves to a loopback, private or link-local address is refused with `connection.egress_refused`, and the connection pins the address it checked. A local record reaches only the loopback literals it lists.

## 8. Adapter decisions for MCP over HTTP

These decisions are pure functions over results the device already fetched. An endpoint or issuer passed to them that is not canonical (section 3.3) is `connection.malformed`. A fetch result is `{"status": <int>, "json": <parsed body>}`, with `json` present only when the body parsed as JSON. A URL missing from the results did not answer. Every fetch has redirects off, so a `3xx` is an answer that is not metadata.

### 8.1 The challenge

The device first calls the endpoint with no token. On `401` it reads `WWW-Authenticate` by RFC 9110 section 11.6.1. Several header lines are first joined with `, ` into one value, as RFC 9110 section 5.3 allows. Scheme and parameter names match without regard to case, parameter names are reported in lowercase and a value is a token or a quoted string with `\` escapes. A `token68` after a scheme is skipped. The parameters of the first `Bearer` challenge are used. A header with a character outside tab and `%x20-7E`, an unterminated quote, a repeated parameter, or a parameter with no scheme before it is malformed and reads as no challenge.

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

No server chosen gives `connection.no_s256` when at least one server returned metadata (a `200` JSON object) that lacks `S256`. Otherwise it gives `connection.as_unreachable`, whether the fetches timed out or answered with `404`, a `3xx`, a `5xx` or a body that is not a JSON object.

When the owner first adds the connection, selection runs over the listed servers and the chosen issuer goes into the record. At sign-in on any device, the device repeats discovery but signs in only with the record's issuer. The metadata must still list it, else `connection.issuer_changed`, and a new put with a new consent is needed. Then the same checks run on that one server.

### 8.6 Registration

The first branch that applies wins.

| Order | Condition | Branch |
|---|---|---|
| 1 | The record has a `client_id` | `record` |
| 2 | The metadata has `client_id_metadata_document_supported` equal to `true` | `metadata_document` |
| 3 | The metadata has a non-empty text `registration_endpoint` | `dynamic` |
| 4 | None of these | `connection.no_client_id`. The host asks the owner for a client id, which goes into the record by a new put |

Dynamic registration sends `{"application_type", "client_name": "Astral", "grant_types": ["authorization_code", "refresh_token"], "redirect_uris": [<redirect>], "response_types": ["code"], "token_endpoint_auth_method": "none"}` with `application_type` `native`. A `200` or `201` answer with a non-empty text `client_id` succeeds, unless it names a `token_endpoint_auth_method` the device cannot use. The device is a public client and sends no secret, so the only usable method is `none`. An answer that carries `token_endpoint_auth_method` with any other value, a different spelling such as `None`, a value that is not text or a JSON null, is `connection.registration_refused`. This fails closed. The device does not guess a method, does not use a `client_secret` and does not retry. An answer without the field succeeds, because the request asked for `none`. Only a `400` to the `native` request is retried, once, with `web`. Any other outcome is `connection.registration_refused`, and the vectors list the requests sent. A dynamically registered client belongs to one device and lives in its keystore, so each device registers its own and reuses it.

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

On a `401` with a credential the device refreshes once and retries once. A refused refresh gives `needs_attention` with reason `refresh_refused`, a credential that has run out with no refresh token gives reason `expired`, and a `401` after the retry gives reason `refused`. A device with no stored credential at all gives reason `not_signed_in` and sends no request. Each fails the call.

A `403` whose `Bearer` challenge has `error` equal to `insufficient_scope` starts a step-up. Any other `403` is `connection.refused`. The step-up is the union of the record's scopes and the challenge's scopes, put again by a reviewed record put and then a sign-in. A union equal to the record's scopes needs no put and is a sign-in alone. A device allows one step-up between successful calls, so a second one before any call succeeds is `connection.step_up_limit`. The limit is checked before the union. A union of more than 32 scopes is `connection.too_many_scopes` and does not count as a step-up. Until the step-up completes the device logs `needs_attention` with reason `scope` and fails the call.

## 9. Codes

| Code | Where |
|---|---|
| `connection.malformed` | Fold, any event. Also an adapter input that is not canonical |
| `connection.consent_missing`, `connection.consent_wrong_action`, `connection.consent_unknown_device`, `connection.consent_node_device`, `connection.consent_foreign_feed`, `connection.consent_bad_signature`, `connection.consent_stale`, `connection.consent_reused` | Fold, a put |
| `connection.writer_not_member` | Fold, a remove or a state |
| `connection.state_foreign` | Fold, a state |
| `connection.version_unsupported`, `connection.kind_unsupported`, `connection.record_unsupported`, `connection.scheme_unsupported`, `connection.server_id_collision` | Fold, the reason of an unsupported record |
| `connection.local_elsewhere`, `connection.egress_refused`, `tool.name_too_long`, `tool.name_invalid` | Use on a device |
| `connection.resource_metadata_offsite`, `connection.no_resource_metadata`, `connection.resource_mismatch`, `connection.no_authorization_server`, `connection.issuer_mismatch`, `connection.issuer_changed`, `connection.no_s256`, `connection.as_unreachable` | Discovery |
| `connection.no_client_id`, `connection.registration_refused` | Registration |
| `connection.too_many_scopes`, `connection.iss_missing`, `connection.iss_mismatch`, `connection.token_passthrough`, `connection.refused`, `connection.step_up_limit` | Sign-in and calls |

The A2A codes are in section 13.8 and the credential item codes are in [credential-items.md](credential-items.md). The device authorization host and an unusable registration method add no code. They use `connection.egress_refused` and `connection.registration_refused`.

## 10. Existing data and mixed versions

Existing data:

| Change | Existing data | Handling |
|---|---|---|
| `settings.set` connection keys | v2 stores on Mac 97, iPhone 99 and nodes | Additive. The settings and authority folds give identical results for all other keys |
| Kernel meta `connection.credential/*` | v2 stores | Additive. Absent means not signed in. Not in backups |
| `remote.mcp.*` items | Mac 97, iPhone 99 | Unchanged and still offered in turns. Listed as "Added before connections". A new legacy name of the form `c` and 8 hex digits is refused |
| `model.provider.*` items | Mac 97, iPhone 99, nodes | Unchanged and used. Later key connections write the same format |
| Provider, legacy server and turn binding items | Mac 97, iPhone 99, nodes | Unchanged and valid, because the content rule of credential-items.md refuses only sign-in token fields and a size over 65536 bytes |
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
| Kinds `a2a`, `mcp-stdio`, `http-api` and `model` | Ignored | Listed unsupported by a reader that implements only `mcp-http`, and used by the extended reader |
| State reason `not_signed_in` | A state with an unknown reason is `connection.malformed` and the device's older state stands | Read |

A test with two real kernels proves the first row.

## 11. Vectors

Both files use the vector format of the roster v2 contract. `connection-v1.json` publishes `test_devices`, the roster v2 test devices `alice` (desktop), `bob` (pocket), `carol` (satellite) and `node` (node), the removed `dave` (desktop) and the stranger `mallory`, with their ids, feeds and presence scalars. `bob` writes from two feeds. They are public test values derived from the labels named there. Clocks are fixed and no case reads a wall clock or the network.

Case kinds in `connection-v1.json`:

- `connection_key` has `key` and expects the form or `connection.malformed`.
- `connection_record` has `id` and `value` and expects `{status, reason, digest, server}` or `connection.malformed`.
- `connection_state` has `value` and expects `{}` or `connection.malformed`.
- `connection_fold` has the inputs of section 6 and expects the output of section 6.5. Every fold case gives the same output for its events in any order.
- `connection_use` has a fold entry and a device id.
- `connection_exposed_name` has `id` and `tool`. `connection_ask_slug` has `name`.
- `connection_egress` has `egress` and `url`. `connection_exec_host` has `entry`.
- `connection_coexistence` has a log with ordinary and connection events and `presence_keys`, and expects the deployed authority fold and the deployed settings fold with and without the connection events.

The extended reader's cases use their own case kinds so that a reader that implements only `mcp-http` can skip them. `connection_record_ext` and `connection_fold_ext` run the same checks with the kinds and schemes of section 3.5. `connection_use` takes a fold entry from either reader. The cases `record_phase2_*` and `fold_phase2_lists_every_new_kind_unsupported` pin the first reader on the new kinds, and every older case keeps its first-reader outcome.

Case kinds in `connection-oauth-v1.json`: `oauth_challenge`, `oauth_resource_metadata_urls`, `oauth_resource_metadata`, `oauth_as_metadata_urls`, `oauth_select_server` (with `record_issuer` null when the owner first adds the connection), `oauth_registration`, `oauth_dynamic_registration` (its expectation lists the requests in both outcomes), `oauth_egress_entries` (the entries a writer lists, section 3.4), `oauth_scopes`, `oauth_iss`, `oauth_step_up_sequence` (steps of `forbidden` with a header or `success`) and `oauth_bearer_target`.

Groups of cases:

| Group | Cases |
|---|---|
| Keys | `key_*`, each form, the lookalikes that are not reserved and each malformed form |
| Record | `record_*`, every field rule, and each limit at and one past its value: 32768 bytes, a name of 80, 128 tools, a tool name of 128, 32 scopes, a scope of 256, a `client_id` of 1024, 16 egress entries, a URL of 2048, port 65535, a host of 253 and a label of 63. Percent escapes of a dot, of a letter and of a reserved character, and a name with a zero width joiner. Also the unsupported reasons and `record_order_*` for their order |
| State | `state_*`, every state and reason, `tools` placement and limits |
| Put | valid, node consent, a malformed consent object, the window at 600000 and 600001 ms before and 0 and 1 ms after, wrong digest and kind, tampered and high-S signatures, re-carried values on another member's feed, on the node's feed and on the same feed inside the window, a stranger's feed carrying a member's consent, a put from a second feed, removed and unknown consenting devices, a forged removed approval, removed approvals with a bad signature, outside the window, before a remove and after one, and `fold_order_*` for each adjacent pair of section 6.1 that can fail together |
| Remove | without consent, by the node, from a second feed, by a removed device (it stands), from a feed no roster device owned, with a consent field, and a put after a removal |
| Winner | a later put, a later invalid put, an equal clock broken by feed, an equal millisecond broken by the counter, and arrival order |
| State fold | own device, a second feed of the device, another device, a removed member, the latest state, a later unreadable state, several devices, tools from a node, and a state for an unlisted connection |
| View | unknown kind, an unsupported put after a supported one, scheme `key`, server id collisions with an ok, an unsupported and a legacy holder, and a local record naming the node |
| Carrier | `mem.remember` under a reserved key, other events, a malformed reserved key, an unknown family, wrong labels and a chat scope |
| Use and names | `use_*`, `exposed_*` at 53 and 54 characters, `slug_*` |
| Egress | `egress_*` and `exec_*`, including an entry that is not canonical |
| Extended records | `record_ext_*` for each new kind, every field rule, and each limit at and one past its value: 64 args, an arg of 4096, a command of 1024, 32 env names, an env name of 64, a header of 64 and a format of 64 characters, a provider item of 128 characters. The refused env names `PATH`, `LD_` and `DYLD_`, the refused headers in any case, an A2A tool that is not the ask tool, a card origin that differs from `card_url`, an issuer or card host missing from `egress`, and the phase 2 pins `record_phase2_*` |
| Extended fold and use | `fold_phase2_lists_every_new_kind_unsupported`, `fold_ext_*` including a server id collision between new kinds and the state reason `not_signed_in`, and `use_ext_*` including a stdio server on another device |
| Coexistence | `coexistence_deployed_folds` |
| Adapter | `challenge_*` including two header lines, `rm_*` including each near miss of the origin pin and an encoded dot segment, `prm_*`, `as_urls_*`, `select_*` including the mixed unreachable cases, error answers only and the record issuer, `reg_*`, `dyn_*` including each unusable `token_endpoint_auth_method`, `egress_entries_*` including a device authorization host on another host and on another port, `scopes_*`, the six `iss_*` rows and their spellings, `stepup_*` sequences, `bearer_*`, and an endpoint or issuer that is not canonical for each URL decision |

`a2a-card-v1.json` and `credential-items-v1.json` use the same format. Their case kinds are `a2a_card_read`, `a2a_card_canonical`, `a2a_card_select`, `a2a_thumbprint`, `a2a_card_verify`, `a2a_card_propose`, `a2a_card_check`, `a2a_card_extended` and `a2a_task_state` for section 13, and `credential_name`, `credential_write` and `credential_binding` for credential-items.md. The A2A cases come from the phase 0 fixture cards, the 19 valid and the 19 malformed, the signed and changed cards and the two public test keys, with a non-ASCII card and cards signed by deterministic test keys added. No private key is committed. The test keys derive from fixed seed strings.

Regenerate with `python specs/106-sign-in-free-clients/contracts/reference/generate_connection_vectors.py`, `generate_a2a_vectors.py` and `generate_credential_vectors.py` and verify with `--check`, which writes nothing.

## 12. Versioning

A change to a rule, a field or a code raises the format version, except a new kind or scheme defined in place at `v: 1`, which older readers list as unsupported. This document, the vectors and the reference modules change together, and the three `--check` commands and the reference tests pass before a change merges.

## 13. A2A cards

These decisions are pure functions over an agent card that the device already fetched and parsed. The client speaks the JSON-RPC binding of A2A 1.0.1. It sends a message, polls the task and fetches the extended card, and it takes the method names from the specification text at the `v1.0.1` tag. Each connection is one tool, `ask_<slug>`, with the input `{message}`. A card is never fetched without the record's egress check (section 7.3).

### 13.1 Reading a card

The reader accepts the 1.0 shape and the 0.3 shape and never a mix. It reads `securitySchemes`, the requirements, the skills, the interfaces and the extended card flag. Refusals have the codes `a2a.not_an_object`, `a2a.missing_field`, `a2a.wrong_type`, `a2a.insecure_url`, `a2a.ambiguous_scheme`, `a2a.unknown_scheme_type`, `a2a.bad_api_key_location`, `a2a.unknown_flow`, `a2a.requirement_unknown_scheme`, `a2a.empty_requirement`, `a2a.conflicting_requirements`, `a2a.mixed_dialects`, `a2a.float_in_card` and `a2a.bad_key`.

- Requirements are OR across entries and AND inside one entry. The reader accepts `securityRequirements` or `security`, and refuses a card where both are present and disagree.
- Every URL in a scheme or an interface is HTTPS with no credentials and no fragment.
- A 1.0 scheme holds exactly one variant, and an OAuth scheme in 1.0 holds exactly one flow.
- An unknown scheme, flow or variant is refused. A scheme that is known and not usable is parsed and refused at selection (section 13.3).
- The extended card flag is `capabilities.extendedAgentCard` or the 0.3 `supportsAuthenticatedExtendedCard` and must be a boolean.

### 13.2 Canonical form and the card hash

The canonical form is RFC 8785 over the card with `signatures` removed, after pruning. Pruning drops null, empty text, empty arrays and objects and `false`, with three exceptions. The containers named `securitySchemes`, `securityRequirements` and `security`, and everything inside them, are kept as they are. The booleans `streaming`, `pushNotifications` and `extendedAgentCard` are kept when false. A number with no fraction is written as an integer, and a number with a fraction is refused with `a2a.float_in_card`. Keys sort by UTF-16 code units and text is written as UTF-8 with no escapes beyond JSON's own. The card hash is `sha256:` and the lowercase hex SHA-256 of those bytes. A signature, a key order and an empty field never change it.

The SDK form drops every empty value, including the empty scope lists and empty requirement entries inside the requirement containers. A signer that follows the SDK produces a signature over that form.

### 13.3 Selecting a sign-in

The device takes the requirement entries in card order and uses the first one whose schemes are all usable. A card with no requirements needs no sign-in.

| Scheme | Usable |
|---|---|
| `apiKey`, HTTP `bearer`, HTTP `basic` | Yes, with a pasted key (section 3.10) |
| OAuth 2.0 `authorizationCode` | Yes, always with S256 and the flow of section 8 |
| OAuth 2.0 `deviceCode` | On a node only |
| OpenID Connect | Yes, through the discovery document |
| Other HTTP schemes, `clientCredentials`, `implicit`, `password`, mutual TLS | No |

No usable entry is `a2a.scheme_unsupported`. A card whose only entry uses `deviceCode` is unusable on a device that is not a node.

### 13.4 Pins and the check

A put pins the card as section 3.6 says. A signed card pins the signer's thumbprint, and an unsigned card pins the hash and the HTTPS origin and the card says "unsigned". The first sight, before any put, is `a2a_card_propose`. It reads the card, takes the origin of the address it was fetched from, verifies any signature against the public keys the device was given and returns `{hash, origin, signer, signed}`.

Before each use the device fetches the card and checks it in this order, and the first failure wins.

| Order | Check | Code |
|---|---|---|
| 1 | The card reads (section 13.1) | The reader's code |
| 2 | The origin it was fetched from equals the pin's origin | `a2a.origin_changed` |
| 3 | With a pinned signer, the card is signed and a signature verifies over the card, and it covers every requirement entry | `a2a.signature_missing`, `a2a.signature_invalid`, `a2a.signature_scope` |
| 4 | With a pinned signer, the verifying key's thumbprint is the pin | `a2a.signer_changed` |
| 5 | The card hash equals the pinned hash | `a2a.card_changed` |
| 6 | Every interface host is in `egress` | `a2a.interface_offsite` |

Any change to the card, a new signer or an interface host outside `egress` refuses use until the owner approves a reviewed put. A card that is re-signed by the pinned signer after a change is still `a2a.card_changed`, because the owner never reviewed the new content. An interface counts only when it is HTTPS on a DNS name and its entry is in `egress`, so an IP literal is `a2a.interface_offsite`. The pin of an unsigned card is not changed by the card gaining a signature.

### 13.5 Signatures

A signature is a JWS in the card's `signatures`, as A2A defines. Its protected header holds `alg` and a text `kid`. The algorithms are `ES256`, `EdDSA` and `RS256`, and an RSA key of fewer than 2048 bits is refused. Every other algorithm, including `none` and `HS256`, is not verified. A key is a public JWK of the kind the algorithm needs, and a key with a `kid` is tried only when it equals the header's `kid`, and a key with no `kid` is tried for any header. The signature is over the protected header, a dot and the base64url of the canonical form of section 13.2.

The signer is the key, never its `kid`. The thumbprint is the RFC 7638 value, the base64url of the SHA-256 of the key's required members in sorted order with no spaces. Two keys with the same `kid` are different signers.

A signature that verifies only over the SDK form is accepted only when it covers every requirement entry, which means the requirement containers of the card and of its skills are the same in both forms. A card whose empty scope lists or empty entries the SDK form dropped gets `a2a.signature_scope`. A card where the two forms differ only outside the requirements, such as a `false` flag, is accepted. A signature that is malformed, uses an unknown algorithm or verifies under no supplied key is `a2a.signature_invalid`. The first signature that verifies decides, and a bad signature before a good one is skipped.

The extended card is fetched after sign-in. Its extra skills are used only after a reviewed put pins its hash in `card.extended_hash`. With no pinned hash the device uses none of them and reports `a2a.extended_unreviewed`, and with a different hash it reports `a2a.extended_changed`.

### 13.6 The tool

The connection offers one tool named `ask_<slug>` of the record name (section 7.2), with the input `{message}`. Its hash is the object id of 105 [tools.md](../../105-decentralized-personal-mesh/contracts/tools.md), `sha256:` and the SHA-256 of the LETS-CJ/1 bytes of `{"description", "name", "skills"}` taken from the card after pruning (section 13.2), with `""`, `""` and `[]` for a field the card does not have. A change to the card's name, description or skills is a new tool hash, so it goes through the review card of section 3.2.

### 13.7 Tasks

`TASK_STATE_AUTH_REQUIRED` fails the call with `connection.sign_in_needed`. The device writes `needs_attention` with reason `not_signed_in` when it holds no credential and `refused` when it does. It sends nothing on its own, so no credential is guessed and no message is retried. Every other task state continues.

### 13.8 A2A codes

| Code | Where |
|---|---|
| `a2a.not_an_object`, `a2a.missing_field`, `a2a.wrong_type`, `a2a.insecure_url`, `a2a.ambiguous_scheme`, `a2a.unknown_scheme_type`, `a2a.bad_api_key_location`, `a2a.unknown_flow`, `a2a.requirement_unknown_scheme`, `a2a.empty_requirement`, `a2a.conflicting_requirements`, `a2a.mixed_dialects`, `a2a.float_in_card`, `a2a.bad_key` | Reading a card |
| `a2a.scheme_unsupported` | Selection |
| `a2a.signature_invalid`, `a2a.signature_missing`, `a2a.signature_scope`, `a2a.signer_changed`, `a2a.origin_changed`, `a2a.card_changed`, `a2a.interface_offsite`, `a2a.not_a_card_pin` | Proposal and the check |
| `a2a.extended_unreviewed`, `a2a.extended_changed` | The extended card |
| `connection.sign_in_needed` | A task that needs authorization |

