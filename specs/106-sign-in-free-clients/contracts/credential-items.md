# Credential items

This contract fixes what a `key.credential` item may hold. It replaces the content rule of 105 [keys.md](../../105-decentralized-personal-mesh/contracts/keys.md) section 4.1 and leaves the rest of that section as it is: the event, the boxes, the audience, the recipients, the late device rule and the fold.

Reference code: `reference/credential_items.py`. Generator: `reference/generate_credential_vectors.py`. Vectors: `vectors/credential-items-v1.json`. LETS-CJ/1 and the caller ticket label table come from the preserved 105 reference.

## 1. What an item holds

An item holds a pasted secret and the configuration that travels with it. Examples are a model provider's endpoint, models and keys, a legacy remote server's pins, a turn binding, and the keys of a connection. It never holds a token obtained by signing in. Sign-in credentials live in the device's own keystore and never sync (connection.md section 10).

The value is a JSON object whose LETS-CJ/1 bytes are at most 65536 bytes. A value that is not an object is `credential.malformed`. A value over the limit is `credential.too_large`.

## 2. Names

A name is 1 to 128 characters of `a-z`, `0-9`, `.`, `_` and `-`, starting with a letter or digit, as 105 keys.md section 4.1 says. Any other name is `credential.malformed`. A name belongs to a family by its prefix.

| Family | Name | Shape of the value |
|---|---|---|
| `connection_key` | `connection.key.` and 32 lowercase hex digits, the connection id | Section 4 |
| `provider` | `model.provider.` and a name | Unchanged. Whatever the writer wrote before stays valid |
| `legacy_server` | `remote.mcp.` and a name | Unchanged |
| `other` | Anything else, including turn bindings | Unchanged |

A name that starts with `connection.key.` and is not followed by exactly 32 lowercase hex digits is `credential.malformed`.

## 3. Sign-in tokens

Every writer refuses a value that has a field named `access_token`, `refresh_token` or `id_token`, at any depth and inside any array, compared without regard to case. The code is `credential.sign_in_token`. The rule exists so a sign-in cannot leak into a synced item, and the device that holds a sign-in credential keeps it where section 1 says.

A field name is not the same as text. A value that has the words in a text, or a field named `access_tokens` or `token`, is allowed. For a `connection_key` item the names of the secrets (section 4) are not fields, because they are environment variable names the owner chose, so `ACCESS_TOKEN` is a valid secret name. Every other top-level field of that item is scanned.

The check comes after the size check and before the shape check of the family, so an oversized item is `credential.too_large` and a key item with a token field is `credential.sign_in_token` before its shape is read.

## 4. Connection key items

The value has exactly `v`, the integer 1, and `secrets`, an object of 1 to 32 entries. A secret name matches `[A-Za-z_][A-Za-z0-9_]{0,63}`. A secret value is 1 to 4096 code points that the caller ticket label table accepts, so a control, a format character such as the zero width joiner and an unassigned code point are refused. Any other shape is `credential.malformed`.

A record needs exactly these secret names.

| Record | Secret names |
|---|---|
| `http-api` | `key` |
| `mcp-http` with scheme `key` | `key` |
| `a2a` with scheme `key` | `key` |
| `mcp-stdio` | The record's `env` names |
| Any other record | None. It takes no key item, and a model connection uses its provider item |

An item whose names differ from the record's is `credential.key_mismatch`. The device reads the item only at dispatch. For a stdio record it injects the environment when it starts the server, and the kernel never returns a secret through the binding.

## 5. Who may write

Keys come only from a native secure field, through the foreign interface of the kernel. No tool output and no model output can call the writer. The source of a write is `secure_field`. Any other source, such as `tool_output`, `model_output`, `import` or `sync`, is `credential.source_refused`, and the check comes before every other check. A key received by sync is not written again. It is resealed only by the rule of 105 keys.md section 4.1.

## 6. Codes

| Code | Meaning |
|---|---|
| `credential.malformed` | A bad name, a value that is not an object, or a key item of the wrong shape |
| `credential.too_large` | The value is over 65536 bytes |
| `credential.sign_in_token` | The value has an `access_token`, `refresh_token` or `id_token` field |
| `credential.key_mismatch` | The secret names of a key item differ from the record's |
| `credential.source_refused` | The writer was not the native secure field |

The codes of 105 keys.md section 4.1 stay: `credential.not_sealed`, `credential.label`, `credential.bad_recipients`, `credential.not_a_recipient` and `wrap.decrypt_failed`.

## 7. Existing data

Items written before this contract stay valid and keep their bytes. A provider item, a legacy server item and a turn binding item carry no sign-in token today, so the new refusal changes nothing for them. No migration runs. A writer that finds a value it would now refuse leaves it in place and reads it as before, and it refuses only a new write.

## 8. Vectors

`credential-items-v1.json` uses the vector format of the roster v2 contract. The case kinds are `credential_name` (the family or `credential.malformed`), `credential_write` (a name, a source and a value) and `credential_binding` (a record and a key item value). The groups are `name_*`, `write_*` with each limit at and one past its value (32 secrets, a secret of 4096, a secret name of 64, an item of 65536 bytes), `write_token_*` for each field, case, depth and the near misses that are allowed, `write_source_*`, and `binding_*` for each kind of record. Regenerate with `generate_credential_vectors.py` and verify with `--check`.
