# Contracts for spec 105

These documents fix the wire formats of the personal mesh. They are language neutral. A Python reference and shared test vectors make them executable, and the Rust kernel and every other implementation must reproduce the vectors byte for byte.

The formats are the ones fixed in [../tasks.md](../tasks.md). Where a format needed more detail, the detail is added here and listed under "Additions" below.

## Index

| Document | Format version | Covers | Vectors | Reference module |
|---|---|---|---|---|
| [event.md](event.md) | `astral.event/v1` | Canonical JSON `LETS-CJ/1`, ids, signature contexts, clock, header, body, wire envelope, feeds, chains, event types, merge rules of the families | `canonical-json.json`, `crypto.json`, `event.json`, `feed.json`, `fold.json` | `cj.py`, `crypto.py`, `event.py`, `fold.py` |
| [sync.md](sync.md) | `astral.sync/v1` (ALPN `astral/sync/1`) | Messages, session, event acceptance, bodies by hash, PHI withholding, checkpoints, fork and shortened-feed evidence, restored device | `sync.json`, `feed.json` | `sync.py` |
| [roster.md](roster.md) | `astral.roster/v1` | Genesis, add, remove, rotate, attest, accepted issuer, PHI authority and approval, epoch, wrap, stop, the merge, identity attestation, unattested devices, pairing | `roster.json`, `attestation.json`, `pairing.json` | `roster.py`, `attestation.py`, `pairing.py` |
| [keys.md](keys.md) | `astral.keys/v1`, wrap `astral.wrap/v1`, backup `astral.backup/v1` | Device keys, key groups and epochs, wrapping, content keys, recovery key and its 24 words, encrypted backup | `keys.json`, `crypto.json`, `credential.json` | `keys.py`, `crypto.py`, `credential.py` |
| [policy.md](policy.md) | `astral.policy/v1` | Budget policy, consent for widening, narrowing-only merge, input to LETS | `policy.json` | `policy.py`, `consent.py` |
| [ticket.md](ticket.md) | `astral.ticket/v1` | Biscuit capability chain, action digest, consent, receipt binding, labels as facts | `ticket.json` | `ticket.py`, `consent.py` |
| [labels.md](labels.md) | `astral.labels/v1` | Integrity and confidentiality axes, inheritance, quarantined readers, flow rules, gate order | `labels.json` | `labels.py` |
| [surface.md](surface.md) | `astral.surface/v1` | Surface documents, patches, action events and capabilities, value bindings | `surface.json` | `surface.py` |
| [runs.md](runs.md) | `astral.runs/v1` | Run ids, lease, goal and schedule bodies, the lease fold (one executor, handoff, takeover with the owner's presence consent, contested runs), the state fold, run start checks, schedule arithmetic, the pre-check, the judge's fixed answer | `runs.json` | `runs.py` |
| [learning.md](learning.md) | `astral.learning/v1` | Candidates and the content hash, evidence labels, proposal, gate, replay, adoption and reversal bodies, the static gate, the replay digest, the trial, the ledger fold that re-checks every adoption and recomputes owner review from the proposal itself | `learning.json` | `learning.py` |

A change to a format raises its version. A document, its vectors and its reference module change together.

## Not defined here

- LETS receipt and lease formats, and the derivation of a period's shares. LETS owns them. `policy.md` fixes the input and `ticket.md` fixes the two receipt fields this design needs.
- The mailbox protocol and the removal notice. A later contract adds them and a signing context for mailbox requests.
- The component vocabulary. It comes from the primitives schema. The surface validator takes it as input.
- The transport itself (iroh) and the FFI surface of the kernel.

## Additions to the shared choices

These are details the fixed list did not pin down. None changes a fixed choice.

- The HKDF salt of `astral.wrap/v1` is `epk || recipient public key`.
- Event headers seal under an epoch named by id. The first 32 bytes of `header_ct` are that id. Epoch ids are roster entry ids.
- `chat.create` has the scope `new`. The chat id is that event's id.
- Roster entry types beyond add and remove: `rotate`, `attest`, `epoch`, `wrap`. The mesh family of spec 5.3 lives in the roster and not in feeds.
- A device record's certificate uses the pairing context and binds the mesh.
- P-256 signatures are low-S only. The event id covers the signature, so malleability must not exist.
- The identity nonce is lowercase hex of `sha256(identity key || mesh id bytes)`.
- Tickets use blocks per hop: device, turn, agents, call. The grammar of a block is restricted (`ticket.md`).

## Conventions

**Errors.** Every invalid case raises a stable dotted code, such as `event.bad_signature`. The reference raises `MeshError` with that code. Implementations may use their own types but must report the same code when they surface errors.

**Time.** Clocks are in milliseconds. Policy periods and attestations use seconds. Vectors fix every clock. The base is 1794441600000 ms (2026-11-12T00:00:00Z).

**Test keys.** Every key and nonce in the vectors derives from a label with these functions, so a second implementation can regenerate any secret.

- `seed(label) = SHA-256("astral-test-seed/" || label)`.
- `scalar(label) = (int(seed(label)) mod (n - 1)) + 1`, where `n` is the P-256 group order.
- A device named `x` has identity scalar `scalar("x/identity")`, presence scalar `scalar("x/presence")`, operations seed `seed("x/ops")` and wrap private key `seed("x/wrap")`. Each vector file that uses them lists the secrets under `devices`.
- The random stream `rng(label)` is `SHA-256(seed("rng/" || label) || counter)` with an 8-byte big-endian counter from 0, concatenated.
- Test issuer keys: ES256 scalar `scalar("issuer/es256")` and a fixed 2048-bit RSA key listed in `reference/scenarios.py`. The institution key for PHI approvals is `seed("institution/uky")` read as a scalar.

These secrets are public and are for tests only.

## Vector files

All files live in `vectors/`. `index.json` lists each file with its case count and SHA-256. Every file is an object:

```
{ "format": "astral.vectors/v1", "name": <file name>, "spec": <document>,
  "cases": [ { "name": ..., "kind": <operation>, "input": {...}, "expect": {"ok": ...} or {"error": <code>} } ],
  ... extra top-level fields listed below }
```

A harness runs each case by `kind` with `input` and compares the result with `expect`. A result is `{"ok": value}` or `{"error": code}`. Names of keys, ids, hashes, signatures and bytes follow the documents. Fields ending in `_hex` are hexadecimal. Everything else that is bytes is unpadded base64url. The operations and their exact inputs are in `reference/astral_mesh_ref/runner.py`.

| File | Operations (`kind`) and their inputs | Extra top-level fields |
|---|---|---|
| `canonical-json.json` | `canonical` (`json`: text) gives `canonical` and `sha256`. `b64url` (`text`) gives `hex` | none |
| `crypto.json` | `keypair` (the four secrets). `sign` (`alg`, `context`, `object`, `secret_hex`) gives `sig`. `verify` (`alg`, `context`, `object`, `public`, `sig`) gives `valid`. `aead_seal`, `aead_open`. `hkdf` | `devices` (test secrets), `contexts` |
| `event.json` | `seal_event` (header fields, keys, nonces) gives `envelope`, `event_id`, `header`. `open_event` (`envelope`, `epoch_keys`, `content_keys`, `now_ms`) gives `header`, `body`, `event_id` | `devices` |
| `feed.json` | `feed_replay` (`steps`: headers, `statuses`: feed to roster status, `now_ms`) gives `results` (per step: `accepted`, `duplicate`, or an error code), `heads`, `forked` | `devices` |
| `roster.json` | `roster_fold` (`entries`, `mesh`, `now_s`) gives `members`, `removed`, `unattested`, `stale_attestation`, `principal`, `issuers`, `phi_authorities`, `phi_members`, `epochs`, `current_epoch`, `stop`, `void`, `rejected`, `pending`, `feeds`, `heads`, `count`, `mesh`. `roster_shape` (`entry`) gives `id` | `devices`, `names` (device id to name), `issuer_secrets` |
| `attestation.json` | `attestation_verify`. For identity: `att`, `identity`, `issuers`, `mesh`, `now_s`. For PHI: `att`, `authorities`, `now_s` | `devices`, `issuer_secrets`, `nonce_hex` (the nonce of the first case) |
| `pairing.json` | `pairing_invite` (`invite`, `inviter_identity`, `mesh`, `now_s`). `pairing_request` (`request`, `mesh`, `nonce`) gives `device` | `devices` |
| `keys.json` | `wrap`, `unwrap`, `epoch_open`, `recovery_encode` (gives words and derived keys), `recovery_decode`, `backup_seal`, `backup_open` | `devices` |
| `policy.json` | `policy_verify`, `policy_effective` (`wrappers`, `members`, `mesh`), `budget_input` (`effective`, `members`, `mesh`, `now_s`, `stop`) | `devices`, `budget_input_note` |
| `ticket.json` | `action_digest`. `ticket_verify` (`ticket`, `action`, `effect_kind`, `mesh`, `roster_feeds`, `presence_keys`, `stop`, `now_s`, `consent_required`, `receipt_valid`) gives the claims. `consent_verify` | `devices`, `tokens`, `tokens_note` |
| `labels.json` | `labels_join`, `labels_quarantine`, `labels_flow`, `labels_gate` | none |
| `surface.json` | `surface_validate`, `surface_fold`, `surface_action`, `surface_bind` | `vocabulary`, `actionable` |
| `credential.json` | `credential_check`, `credential_recipients`, `credential_seal`, `credential_open`, `credential_fold` | `devices` |
| `sync.json` | `sync_plan`, `sync_bodies`, `sync_message`, `sync_frame`, `checkpoint_verify`, `shortened` | `devices` |
| `fold.json` | `fold_branches`, `fold_authority`, `fold_settings` | none |
| `runs.json` | `run_lease_fold` (`events`, `devices`: feed id to device id, `presence_keys`: device id to presence key of the current roster) gives `runs`. `run_takeover_digest` (`run`, `supersedes`, `executor`) gives `digest`. `run_state_fold` (`run`, `chat`, `events`) gives `state`. `run_start` (`spec`, `device`, `now_ms`) gives `run` and `events`. `run_schedule_plan` (`schedule`, `now_ms`, `last_slot`). `run_precheck` (`probe`, `last_fingerprint`). `run_judge_answer` (`goal`, `round`, `round_output`, `raw`) gives `verdict`, `progress`, `content` | none |
| `learning.json` | `learn_gate` (`candidate`, `grants`) gives the finding codes, hygiene, owner reasons and labels. `learn_stage`, `learn_gate_record`, `learn_replay_record`, `learn_decline`, `learn_adoption`, `learn_reversal` give the `event` to write. `learn_trial` (`baseline`, `policy`, `samples`). `learn_ledger_fold` (`events`, in any order) gives `proposals`, `active`, `invalid` | none |

In `ticket_verify`, `receipt_valid` stands for the answer of the LETS receipt check. A harness passes it to the verifier's receipt callback.

`feed_replay` uses plain, decrypted headers so a harness can test the chain rules without any encryption. `open_event` covers sealing.

### Cases that matter most

Invalid cases include a bad signature (`event/bad-header-signature`), a wrong signing context (`crypto/wrong-context`, `event/signature-with-wrong-context`), a gap in a feed (`feed/gap-then-fill`), an edited event (`event/edited-body`, `feed/edited-header`), a forked feed (`feed/fork-two-events-at-one-seq`), a future clock (`feed/future-clock`, `event/future-clock-over-five-minutes`), a removed device's later entry (`roster/removed-device-later-entry-rejected`, `roster/concurrent-add-by-removed-device-is-void`), revoke beating grant (`fold/revoke-beats-a-concurrent-grant`), a narrowing-only policy merge (`policy/concurrent-narrowings-merge-to-the-minimum`) and a PHI body withheld from a non-PHI peer (`sync/phi-body-withheld-from-a-non-phi-peer`).

## Biscuit vectors

`biscuit-python` 0.4.0 can express every ticket the contract needs, so no vector set is marked "verified by the kernel only". Biscuit signs each block with a fresh ephemeral key, so token bytes differ on every run. The tokens in `ticket.json` are pinned in its `tokens` field and are not regenerated. `--check` verifies that the pinned tokens still give the committed outcomes. `generate_vectors.py --repin-tickets` mints new tokens, which changes `ticket.json`.

The library's default run limit for the authorizer is one millisecond and a busy machine can hit it. Executors must set a generous limit. The reference retries.

## Reference

`reference/astral_mesh_ref/` is a small package for Python 3.11 and later. It depends on `cryptography`, `pynacl` (XChaCha20-Poly1305) and `biscuit-python`, pinned in `reference/requirements.txt`.

```
cd reference
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python generate_vectors.py            # writes ../vectors
.venv/bin/python generate_vectors.py --check    # fails if a committed vector differs
.venv/bin/python -m pytest tests -q             # runs the reference against every vector
```

The tests also check the primitives against published vectors (RFC 8032, RFC 5869, RFC 7748, RFC 6979 and the BIP-39 test vectors), check that merges do not depend on arrival order, and check that every operation is exercised.

LETS has not yet published its receipt and period budget formats. When it does, the ticket receipt binding and the policy's LETS input are checked against its vectors.
