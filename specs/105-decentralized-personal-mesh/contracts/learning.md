# Learning contract, `astral.learning/v1`

The learning loop lets a model propose a memory, a skill or an edit to its standing instructions, and lets deterministic code stage it, gate it, replay it, adopt it, watch it and undo it. This contract fixes the bodies of the three learning event types, the candidate hash, the static gate, the replay report, the trial, and the ledger fold that decides which items are active.

Reference code: `reference/astral_mesh_ref/learning.py`. Vectors: `learning.json`. The Rust code it was written from is `astral-engine` (`learning/types.rs`, `gate.rs`, `ledger.rs`, `replay.rs`, `trial.rs`, `pipeline.rs`) and `astral-kernel` (`learning_service.rs`).

The event header, signatures, canonical JSON and the order key come from `event.md`. Labels come from `labels.md`. The rule that a learned item never carries authority and never widens a permission is the gate's. Spec section 11.3 is the intent.

Where the Rust code is ambiguous or looks weaker than spec section 11.3, this document says what the code does and lists the point in section 10. It does not invent a fix.

## 1. Events, scope and ids

| Type | Scope | Meaning |
|---|---|---|
| `learn.proposal` | `mesh` | One step of a proposal. The body's `op` is `stage`, `gate`, `replay` or `decline` |
| `learn.adoption` | `mesh` | An item became active |
| `learn.reversal` | `mesh` | An adopted item was withdrawn |

A proposal id is the event id of its `stage` event. An adoption id is the event id of its adoption event. A body is a canonical JSON object. Every number is a non-negative integer.

The ledger is the learning events of the `mesh` scope whose bodies this device holds. An event with its body withheld (PHI the device may not read) or not yet fetched is left out, so the fold of two devices agrees only once they hold the same bodies.

## 2. Items, candidates and the content hash

| Item kind | Meaning | Size limit of `content` |
|---|---|---|
| `memory` | A fact or preference | 4096 bytes |
| `skill` | A reusable procedure in text | 4096 bytes |
| `instruction_edit` | A small edit to the standing instructions | 2048 bytes |

A candidate has these fields. The model writes `kind`, `name`, `content`, `tools`, `requested_permissions`, `scope` and `wider`. It never writes `evidence` labels (section 3).

| Field | Type | Meaning |
|---|---|---|
| `kind` | string | An item kind |
| `name` | string | 1 to 64 bytes: lowercase ASCII letters and digits first, then those or `-` or `_` |
| `content` | string | The item text |
| `tools` | array of strings | Tools the text names. Each must already be granted |
| `requested_permissions` | array of strings | Must be empty |
| `scope` | object | `project` and `tool`, each a string or null. Where the item applies first |
| `wider` | boolean | The candidate asks to apply beyond `scope` |
| `evidence` | array of `{event, labels}` | The events that justify it |

The **content hash** is the hash (`sha256:` and 64 hex digits) of the UTF-8 text `kind`, a newline, `name`, a newline, `content`. It covers the kind and the name as well as the text.

## 3. Evidence and labels

A candidate takes the join (`labels.md` section 2) of its evidence labels. The join of no evidence is `{public, owner}`.

The caller never supplies evidence labels. The kernel looks up each evidence event id in its own log and takes that event's header labels. An id the log does not hold stops the proposal with `engine.journal`. The labels in the staged body are therefore the resolved ones.

A stage, gate, replay, system decline and automatic adoption event takes the join of the candidate labels and the bookkeeping label `{private, tool-trusted}` (`runs.md` section 1.2). An owner adoption keeps that join's confidentiality and sets integrity to `owner`. An owner decline or undo carries exactly `{private, owner}`. A reversal by the trial carries exactly the bookkeeping label.

## 4. Bodies

### 4.1 `learn.proposal`, op `stage`

| Field | Type | Rule |
|---|---|---|
| `op` | string | `stage` |
| `kind`, `name`, `content` | strings | As in section 2 |
| `content_hash` | string | The content hash of section 2 |
| `tools`, `requested_permissions` | arrays of strings | |
| `scope` | object | `{project, tool}` |
| `wider` | boolean | |
| `evidence` | array | `{event, labels}` for each piece of evidence. Not empty when written |

A proposal with no evidence is refused with `learn.no_evidence` before anything is written.

### 4.2 `learn.proposal`, op `gate`

| Field | Type | Rule |
|---|---|---|
| `op` | string | `gate` |
| `proposal` | string | The proposal id |
| `passed` | boolean | True exactly when `violations` is empty |
| `violations` | array of `{code, detail}` | Code from section 5. `detail` is free text for people |
| `hygiene` | array of strings | Phrases found by the hygiene scan |
| `owner_reasons` | array of strings | `untrusted_evidence`, `wider_scope`, `instruction_edit`, `suspicious_text`, in that order, only those that apply |

### 4.3 `learn.proposal`, op `replay`

| Field | Type | Rule |
|---|---|---|
| `op` | string | `replay` |
| `proposal` | string | The proposal id |
| `adopt` | boolean | `must_pass_ok` and `target_improved` and not `target_regressed` |
| `must_pass_ok`, `target_improved`, `target_regressed` | booleans | The report's results |
| `suite_hash` | string | Hash of the recorded check suite. Opaque here |
| `candidate_hash` | string | The content hash the replay was run for |
| `report_hash` | string | The report digest of section 6 |

### 4.4 `learn.proposal`, op `decline`

| Field | Type | Rule |
|---|---|---|
| `op` | string | `decline` |
| `proposal` | string | The proposal id |
| `reason` | string | `owner_declined` or `gate_failed_again` |
| `by` | string | `owner` or `system` |

### 4.5 `learn.adoption`

| Field | Type | Rule |
|---|---|---|
| `proposal` | string | The proposal id |
| `mode` | string | `auto` or `owner` |
| `content_hash` | string | Must equal the staged `content_hash` |
| `report_hash` | string | Must equal the replay record's `report_hash` |
| `scope` | object | `{project, tool, wide}`. `wide` is a boolean |
| `baseline` | object | `samples`, `errors`, `repeats` of the history before adoption |
| `policy` | object | The trial policy: `min_samples`, `max_error_rise_permille`, `max_repeat_rise_permille` |
| `at` | integer | Device clock in milliseconds |

### 4.6 `learn.reversal`

| Field | Type | Rule |
|---|---|---|
| `adoption` | string | The adoption id |
| `proposal` | string | The proposal id |
| `content_hash` | string | The adopted content hash |
| `reason` | string | `undo` for the owner, `error_rate_rose` or `repeat_rate_rose` for the trial |
| `by` | string | `owner` or `system` |

## 5. The static gate

The reference function is `static_gate`. It takes a candidate and the grants (`granted`: tools the owner allowed, `known`: every tool the agent has). It returns the findings in order, the hygiene phrases, the owner reasons and the labels of section 3. Findings carry a code and a detail. Only the code is part of this contract, except that a finding with the same code and detail as the one before it is dropped.

Checks run in this order. All of them run, and each adds at most the findings named.

| Code | Finding when |
|---|---|
| `learn.gate.empty` | `content` is empty or only Unicode white space |
| `learn.gate.size` | `content` is longer than its limit in bytes (section 2) |
| `learn.gate.name` | `name` breaks the rule of section 2 |
| `learn.gate.format` | Any line has more than 1000 characters. Lines end at `\n`, and a `\r` before it is not counted. The last empty piece after a final newline is not a line |
| `learn.gate.hidden_characters` | Any character is in a hidden range, or is a control character (category Cc) other than newline and tab. Carriage return is a control character |
| `learn.gate.script` | The lowercased text contains a script marker. One finding for the first marker in list order |
| `learn.gate.script` | A line, after leading white space, starts with three backticks and a language in the executable list. One finding for the first such language in text order. A second finding can follow the marker finding because their details differ |
| `learn.gate.network` | The lowercased text contains a network marker (first in list order), or if none, the text holds an IPv4 address |
| `learn.gate.tool_not_granted` | Once for each name in `tools` that is not granted, in the order written |
| `learn.gate.tool_undeclared` | For each known tool, in ascending order, that the text mentions and `tools` does not name |
| `learn.gate.tool_not_granted` | For the same known tool, right after the undeclared finding, if the tool is not granted |
| `learn.gate.carries_authority` | `requested_permissions` is not empty |
| `learn.gate.no_evidence` | `evidence` is empty |

A mention is an occurrence of the tool name in the original text (case-sensitive) where the byte before and the byte after are not an ASCII letter, a digit, `_` or `-`. A text boundary and any byte of 128 or above count as a boundary. The detail of a not-granted finding is the tool name followed by ` is not granted`, so naming a tool twice, or naming and mentioning an ungranted tool, gives one finding.

A candidate passes the gate when there are no findings. Hygiene never changes that.

**Hygiene** is a scan of the lowercased text for the phrases below. Each phrase found is listed in this order. A scan is hygiene only, because spec section 9.1 shows detection alone does not hold. A hit adds the owner reason `suspicious_text` and nothing else.

`ignore previous`, `ignore all previous`, `ignore the above`, `disregard`, `forget your instructions`, `you are now`, `new instructions`, `system prompt`, `developer mode`, `override`, `without asking`, `do not ask`, `don't ask`, `skip confirmation`, `skip the confirmation`, `no need to confirm`, `always approve`, `auto-approve`, `authorized to`, `you have permission`, `pretend`, `jailbreak`, `do not tell the user`, `don't tell the user`, `exfiltrate`, `send the contents`, `reveal your`.

**Owner reasons**, in this order: `untrusted_evidence` when the joined integrity is `untrusted`, `wider_scope` when `wider` is true, `instruction_edit` when the kind is `instruction_edit`, `suspicious_text` when any hygiene phrase was found.

**Hidden ranges** (code points, both ends included): 00AD, 034F, 061C, 115F to 1160, 17B4 to 17B5, 180B to 180F, 200B to 200F, 202A to 202E, 2060 to 206F, 3164, FE00 to FE0F, FEFF, FFA0, FFF9 to FFFB, E0000 to E007F, E0100 to E01EF.

**Script markers**, in this order: `<script`, `</script`, `<iframe`, `<object`, `<embed`, `javascript:`, `vbscript:`, `onerror=`, `onload=`, `#!/`, `eval(`, `exec(`, `os.system`, `subprocess`, `child_process`, `powershell`, `| sh`, `| bash`, `|sh`, `|bash`, `curl ` (with a space), `wget ` (with a space), `rm -rf`, `base64 -d`, `chmod +x`, `$(`, and a backtick followed by `sh`.

**Executable fence languages** (compared in ASCII lowercase): `sh`, `bash`, `zsh`, `fish`, `python`, `py`, `js`, `javascript`, `ts`, `node`, `ps1`, `powershell`, `cmd`, `bat`, `ruby`, `rb`, `perl`, `php`, `lua`, `swift`, `kotlin`, `java`, `go`, `rust`, `c`, `cpp`, `sql`, `applescript`.

**Network markers**, in this order: `://`, `www.`, `localhost`, `ngrok`, `data:text`, `data:application`, `mailto:`, `ftp.`.

**IPv4 address**: split the original text at every character that is not an ASCII digit or `.`. A piece is an address when it has exactly four dot-separated parts, each of one to three digits with a value of at most 255.

## 6. The replay report

A replay runs recorded turns from the local log with tool results stubbed, once without the candidate and once with it. Deterministic checks on the tool calls and outcomes decide every result. No model's opinion is read. The checks and their data are built by code from recorded outcomes. The candidate cannot write them.

| Report field | Meaning |
|---|---|
| `cases` | One entry per replayed turn: `turn` (id), `must_pass_with`, `target_without`, `target_with` (lists of booleans, one per check) |
| `must_pass_ok` | Every must-pass check holds with the candidate |
| `target_improved` | At least one targeted check went from failing without the candidate to passing with it |
| `target_regressed` | At least one targeted check went from passing to failing |
| `suite_hash`, `candidate_hash` | Hashes of the suite and of the candidate content |

`adopt` is `must_pass_ok and target_improved and not target_regressed`. A history with no targeted failure to replay stops with `learn.not_ready`.

The **digest** is the hash of this UTF-8 text:

```
suite_hash "|" candidate_hash "|" ("true" or "false" for adopt)
then, for each case, a newline and
turn "|" bools(must_pass_with) "|" bools(target_without) "|" bools(target_with)
```

`bools` is `[`, the items written `true` or `false` separated by a comma and a space, and `]`. An empty list is `[]`.

## 7. The trial

An adopted item applies first only to the project and tool it came from, unless the owner adopted it wide. A trial counts samples while it applies. A sample records whether a turn errored and whether it repeated a call. The reference function is `trial`.

```
permille(part, whole) = 0 when whole is 0, else min(part * 1000 / whole, 2^32 - 1)   (integer division)
```

For each sample the current counts rise. While `current.samples` is below `policy.min_samples` the verdict is `continue`. Otherwise:

1. If `permille(current.errors, current.samples)` is above `permille(baseline.errors, baseline.samples) + max_error_rise_permille`, the verdict is `withdraw` with reason `error_rate_rose`.
2. Else if the same test on repeats is true, the verdict is `withdraw` with reason `repeat_rate_rose`.
3. Else `continue`.

A withdrawal is a `learn.reversal` with `by` `system`. Wide items and items with no trial are not withdrawn this way. The default policy is `min_samples` 5, `max_error_rise_permille` 100 and `max_repeat_rise_permille` 100. A learned item whose named tools are not all granted at that moment is not offered to the model, but stays active in the ledger.

## 8. The ledger fold

Input: the learning events of the `mesh` scope in order key order `(clock.ms, clock.n, feed, seq)`. The reference function is `ledger_fold`. The fold keeps proposals in the order they were staged and keeps an ordered list of live adoptions.

An event is applied by its type.

**`stage`.** Read the body. It is well formed when `scope` is present, `evidence` is an array whose items each have a string `event` and well-formed `labels`, `kind` is an item kind, `name`, `content` and `content_hash` are strings and `wider` is a boolean. `tools` and `requested_permissions` read as the string items of the array, or empty. The stage is accepted when well formed and `content_hash` equals the hash of section 2 for the stage's own kind, name and content. Otherwise the event is invalid with `learn.fold.malformed_proposal`. An accepted proposal starts as `staged`, takes the stage event's header labels as its labels, and has no gate or replay record.

**Other proposal ops.** An event with no string `proposal` is ignored. One that names no staged proposal is invalid with `learn.fold.unknown_proposal`. One that names a proposal that is already adopted or reversed is ignored. Otherwise by `op`:

- `gate`, if the proposal has no gate record yet: record `passed` (a boolean, false when missing), the `code` of each violation, the string items of `owner_reasons` that are known reasons, and the string items of `hygiene`. The status becomes `gated` when `passed`, otherwise `rejected` with the codes. A second gate record is ignored.
- `replay`, if the status is `gated`, `replayed` or `waiting_owner`: record `adopt` (false when missing) and the three hash strings (empty when missing). The status becomes `declined` with reason `replay_failed` when `adopt` is false. Else it becomes `waiting_owner` when the gate record has any owner reason, and `replayed` when it has none. A later replay record in these statuses replaces the earlier one.
- `decline`: the status becomes `declined` with the body's `reason` (empty when missing), from any status except adopted and reversed.
- any other `op`: ignored.

**`learn.adoption`.** The adoption is invalid when any row below applies. The first row that applies gives the code.

| The adoption fails when | Invalid code |
|---|---|
| The body names no proposal | `learn.fold.no_proposal` |
| The named proposal was never staged | `learn.fold.unknown_proposal` |
| The proposal has no gate record | `learn.fold.no_gate` |
| The gate record has `passed` false | `learn.fold.gate_failed` |
| The proposal has no replay record | `learn.fold.no_replay` |
| The replay record has `adopt` false | `learn.fold.replay_failed` |
| The adoption's `content_hash` differs from the staged one, or the replay's `candidate_hash` differs from the hash of section 2 for the proposal's item | `learn.fold.hash_mismatch` |
| The adoption's `report_hash` differs from the replay record's | `learn.fold.report_mismatch` |
| `mode` is `auto` and the gate record lists any owner reason | `learn.fold.needs_owner` |
| `mode` is `auto` and the proposal's labels or the adoption event's labels have integrity `untrusted` | `learn.fold.untrusted_auto` |
| `mode` is `owner` and the adoption event's labels do not have integrity `owner` | `learn.fold.owner_label` |
| `mode` is anything but `auto` or `owner`, or is missing | `learn.fold.unknown_mode` |
| The proposal's status is already `adopted` | `learn.fold.already_adopted` |
| `scope.wide` is true and the proposal did not ask for `wider` | `learn.fold.wider_than_asked` |

The checks run in the order of the rows. The two rows that depend on `mode` are tried in the order `auto` then `owner`.

A valid adoption sets the proposal's status to `adopted` and adds an active item with the adoption id, the proposal id, the item, the proposal's labels, `mode`, `wide`, and the `baseline` and `policy` read from the body. A baseline field that is missing or out of range reads as 0. A policy field that is missing or out of range reads as its default.

**`learn.reversal`.** A body with no string `adoption` is ignored. A reversal that does not name a live adoption is invalid with `learn.fold.unknown_adoption`. Otherwise that adoption leaves the active list and its proposal's status becomes `reversed` with the body's `reason`.

**Result.** `proposals` in staging order, each with `id`, `item` (`kind`, `name`, `content_hash`, `tools`, `requested_permissions`, `scope`, `wider`), `evidence`, `labels`, `status` (a `name` and its fields), `gate` (`passed`, `codes`, `owner_reasons`, `hygiene`, or null) and `replay` (or null). `active` in adoption order, each with `adoption`, `proposal`, `name`, `kind`, `content_hash`, `mode`, `wide`, `scope`, `labels`, `baseline` and `policy`. `invalid` as `{event, reason}` in event order.

The status names are `staged`, `gated`, `replayed`, `waiting_owner`, `rejected` (with `codes`), `declined` (with `reason`), `adopted` (with `adoption`) and `reversed` (with `adoption` and `reason`).

Two properties follow. A forged adoption, one written with no gate, a failed gate, no replay, a failed replay, another content hash or report hash, or the wrong label or mode, never becomes active. Evidence labelled untrusted is never adopted automatically. It waits in `waiting_owner` until the owner adopts it with an owner-labelled event.

The Rust fold writes the invalid reason as English text. The codes of the table are this contract's names for them, and the Rust vector test maps the text to the codes.

## 9. Errors

| Code | Raised when |
|---|---|
| `learn.no_evidence` | A proposal names no evidence |
| `learn.unknown` | A review or undo names no proposal or adoption |
| `learn.not_ready` | The proposal has not passed a step the action needs, or there is no failure to replay |
| `learn.refused` | The proposal was rejected by the gate |
| `learn.malformed` | A ledger record cannot be read |
| `learn.gate.*` | The finding codes of section 5 |
| `learn.fold.*` | The invalid codes of section 8 |
| `engine.journal` | An evidence event id is not in the log |

`learn.unknown`, `learn.not_ready`, `learn.refused` and `learn.malformed` come from the pipeline and have no vector. The vectors cover the gate, the event builders, the replay digest, the trial and the fold.

## 10. Open points

These are places where the Rust code is silent or looks weaker than spec section 11.3. The vectors do not pin any of them. Points 2 to 5 were run against the Rust fold and behave as described.

1. **The fold trusts the records it checks.** It re-checks every adoption against the gate and replay records in the ledger, but it cannot re-run either. Any roster device can write a stage, a gate record that says `passed` and a replay record that says `adopt`. The re-check is structural. It does not make a record's claim true.
2. **Owner reasons are not recomputed from the item.** The fold reads `owner_reasons` from the gate record. An `instruction_edit` or a `wider` proposal whose gate record lists no owner reason can be adopted with mode `auto`. Spec 11.3 step 6 says both need the owner. The fold should derive `instruction_edit` and `wider_scope` from the staged item itself.
3. **The proposal's labels are the stage event's header labels.** The fold does not compare them with the join of the `evidence` labels in the body. A stage whose header says `owner` but whose body lists untrusted evidence is treated as trusted. The kernel resolves labels from the log when it proposes, but a fold reading the log does not.
4. **A proposal with empty `evidence` is accepted by the fold.** The kernel refuses to write one. A well-formed stage with an empty array folds as a proposal.
5. **A declined or reversed proposal can be adopted again.** The fold refuses a second adoption only when the status is `adopted`. After an owner decline or an undo, an adoption record that passes the checks of section 8 makes the item active again. The gate and replay records stay valid, so a stale or forged adoption can undo the owner's decision.
6. **Reversals are not checked.** Any device's reversal that names a live adoption removes it. The fold does not look at the writer's labels, the body's `by`, or whether `proposal` and `content_hash` match.
7. **Owner mode needs only an owner label.** The fold cannot tell an owner action from a device that wrote the label for itself. `labels.md` says the kernel assigns labels, which depends on how a header's labels are checked on the receiving side.
8. **Fold order is clock order, not causal order.** Two concurrent records, such as an adoption and a decline from different devices, resolve by `(clock, feed, seq)`.
9. **Invalid reasons have no codes in Rust.** They are English strings. Section 8 names the codes. The engine should expose them.
10. **The replay digest uses Rust's debug spelling of a list of booleans.** That is `[true, false]`, which section 6 writes out. Any other spelling gives another digest.
11. **Carriage return counts as hidden.** Every `\r` is a control character, so text with Windows line endings never passes the gate.
12. **Tool mentions are case-sensitive and the rest of the scan is not.** A tool name written in another case is not found by the mention rule.
13. **A learn event in a chat scope is ignored.** The kernel reads only the `mesh` scope, but header validation accepts any scope for the learning family.
14. **No dedicated code for an unknown evidence event.** The kernel reports `engine.journal` with free text.
