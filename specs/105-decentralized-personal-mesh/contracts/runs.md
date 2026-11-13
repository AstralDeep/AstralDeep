# Run contract, `astral.runs/v1`

A run is work that outlives one turn. This contract fixes the bodies of the three run event types, the fold that names one executor per run, the fold that rebuilds a run's state from its events, the rules for starting a run, the schedule arithmetic, the pre-check and the answer the goal judge may give.

Reference code: `reference/astral_mesh_ref/runs.py`. Vectors: `runs.json`. The Rust code it was written from is `astral-engine` (`runs/events.rs`, `runs/driver.rs`, `runs/schedule.rs`, `runs/judge.rs`) and `astral-log` (`fold/runs.rs`).

The event header, signatures, canonical JSON and the order key come from `event.md`. Labels come from `labels.md`. This document only adds what the run family needs.

Where the Rust code is ambiguous or looks wrong against spec section 11, this document says what the code does and lists the point in section 11. It does not invent a fix.

## 1. Events, scope and ids

| Type | Scope | Written by | Meaning |
|---|---|---|---|
| `run.lease` | chat id | the starting, handing off or taking over device | Who executes the run |
| `run.goal` | chat id | the executor, or the owner's device for owner actions | The goal, each round, status changes, notes, unknown steps |
| `run.schedule` | chat id | the executor | The schedule, each slot fired, each slot skipped |

Run events live in the scope of the chat the run works in. The run's `chat` is the chat id of that scope. A run in a chat appears in that chat's feed of events and nowhere else.

A body is a canonical JSON object (`event.md` section 1). Every number is a non-negative integer. A field that a table marks "or null" is written as null and never omitted. A reader accepts a missing field wherever section 4 or 3 says it has a default.

### 1.1 Run ids

A run id is the text `<chat id>.<suffix>`. The chat id is a hash of `event.md` section 2. The suffix written by this version is 24 lowercase hex digits.

A reader splits the id at its first `.`. If the part before it is not a hash or the part after it is empty, the id names nothing and the error is `run.unknown`. The suffix has no other meaning to a reader. The id is opaque once written.

The starting device derives the suffix like this:

```
seed   = chat id "|" device id "|" now_ms "|" counter "|" goal
suffix = first 24 hex digits of SHA-256(seed as UTF-8)
```

`now_ms` is the device clock in decimal. `counter` is a per-process counter that starts at 0, so two starts in one millisecond differ. The goal is the full goal text. The reference takes `counter` as 0.

### 1.2 Labels the writer gives

The kernel assigns labels (`labels.md`). The run family uses two fixed labels and the join rule.

| Label | Value | Used for |
|---|---|---|
| bookkeeping | `{"confidentiality": "private", "integrity": "tool-trusted"}` | Every lease that is not a takeover, rounds, schedule events, unknown steps, non-owner status changes |
| owner action | `{"confidentiality": "private", "integrity": "owner"}` | A takeover lease, status changes made by the owner (pause, resume, stop, status reset on takeover) |
| goal label | the spec's labels | The `define` event. It carries the labels of whatever the goal text came from |
| notes label | join of the notes' source labels and bookkeeping | A `notes` event |

## 2. Bodies

### 2.1 `run.lease`

| Field | Type | Rule |
|---|---|---|
| `run` | string | The run id |
| `executor` | string | Device id that executes the run from this lease on |
| `supersedes` | array of event ids | The leases this one replaces. Empty for the first lease of a run |
| `takeover` | boolean | True when the owner continues a run on this device without the executor's consent |
| `consent` | object or null | The owner's consent to the takeover (section 3.2). Required when `takeover` is true. Ignored otherwise. A reader accepts a missing field as null |
| `reason` | string | Free text for people: `start`, `handoff`, `takeover` |

`reason` is not read by any fold.

### 2.2 `run.goal`

Every `run.goal` body has a string `op` and a string `run`. The ops are `define`, `round`, `status`, `notes` and `unknown`. A body with another `op`, or with no `op`, changes nothing.

`define`, the first event of a run

| Field | Type | Rule |
|---|---|---|
| `kind` | string | `goal`, `schedule` or `watch` |
| `goal` | string | 1 to 8192 bytes of UTF-8, not only whitespace. It is data and never an instruction to any model |
| `project` | string or null | Project the run belongs to |
| `limits` | object | `max_rounds`, `max_stalls`, `max_wait_ms` and `max_tokens` (integer or null) |

`round`, written after each round of a run

| Field | Type | Rule |
|---|---|---|
| `n` | integer | Round number, from 1 |
| `executor` | string | Device id that ran the round |
| `turn` | string | Event id of the turn that ran the round |
| `turn_status` | string | `completed`, `incomplete`, `repeat_stopped`, `max_rounds`, `interrupted`, `failed` or `needs_owner` |
| `verdict` | string or null | `done`, `continue` or `blocked`. Null for a schedule or watch round, which no judge sees, and for a round whose judge call failed |
| `progress` | boolean | The judge's progress flag, forced to false when the round's text equals the previous round's text and the verdict is `continue`. True for a schedule or watch round. False when the judge failed |
| `text_hash` | string | Hash of the round's final text, used to spot a repeated round |
| `tokens` | integer | Tokens spent by the round |
| `at` | integer | Device clock in milliseconds |

`status`

| Field | Type | Rule |
|---|---|---|
| `status` | string | `active`, `paused`, `done` or `stopped` |
| `pause` | string or null | `owner`, `condition` or `waiting` when the status is `paused`, otherwise null |
| `reason` | string | Empty for `active` and `done`. Otherwise one of the reasons in section 9 |
| `executor` | string | Device that wrote the status |
| `at` | integer | Device clock in milliseconds |

`notes`, the per-run notes document

| Field | Type | Rule |
|---|---|---|
| `text` | string | The document |
| `through_round` | integer | The last round the notes cover |
| `sources` | array of event ids | The events the notes were written from |

`unknown`, a step whose outcome nobody knows

| Field | Type | Rule |
|---|---|---|
| `turn` | string | Event id of the turn |
| `calls` | array of strings | Call ids that were dispatched with no recorded result |
| `reason` | string | `takeover` |

### 2.3 `run.schedule`

Every body has a string `op` and a string `run`. The ops are `define`, `fired` and `skipped`.

`define`

| Field | Type | Rule |
|---|---|---|
| `interval_ms` | integer | Above zero |
| `anchor_ms` | integer | The first slot |
| `catch_up` | boolean | Replay missed slots, up to `max_catch_up` |
| `grace_ms` | integer | How late a slot may fire and still count as on time |
| `max_catch_up` | integer | At most 32 bits |
| `precheck` | boolean | Run the pre-check before waking the model |

`fired`

| Field | Type | Rule |
|---|---|---|
| `slot` | integer | The slot that fired |
| `at` | integer | Device clock in milliseconds |
| `fingerprint` | string or null | The pre-check fingerprint |

`skipped`

| Field | Type | Rule |
|---|---|---|
| `slot` | integer | The last slot handled by this skip |
| `missed` | integer | How many slots it covers |
| `reason` | string | `missed`, `unchanged` or a pre-check reason |
| `at` | integer | Device clock in milliseconds |

## 3. The lease fold

Input: the `run.lease` events of one chat scope, the roster's map from feed id to device id, and the presence keys of the current roster devices (device id to key). A feed with no device maps to nothing. A device that is not a current member has no presence key. The reference function is `lease_fold`.

**Parsing.** A lease is readable when its body is an object with string `run` and `executor`, a `supersedes` that is missing, null, or an array of strings, and a `takeover` that is missing or a boolean. A lease that is not readable is dropped. It appears nowhere in the output.

**Order.** Leases are sorted by the order key `(clock.ms, clock.n, feed, seq)` of `event.md` section 10.4. Arrival order has no effect.

**Takeover consent.** Before any pass, every readable lease of the run with `takeover` true is checked against section 3.2. A lease that fails is rejected at once. Its id goes into `rejected` in lease order and into `refused` with the code of the failure. A takeover that passes is decided by the passes below like any other lease. A plain `takeover` flag is never enough.

**Decision.** Group the readable leases by `run` and handle runs in ascending order of the run id text. For one run, repeat passes over its leases in order until a pass changes nothing. In a pass, take each lease that is neither valid nor rejected. Let `author` be the device of the lease's feed. Look at the ids in `supersedes` in the order written. The first id that decides the lease stops the look:

| Target | Result for the lease |
|---|---|
| Names no readable lease (any run) | Undecided, so it keeps waiting |
| Names a lease of another run | Rejected |
| Names a rejected lease | Rejected |
| Names a lease that is not yet valid and not rejected | Undecided |
| Names a valid lease and `takeover` is false and `author` is not that lease's `executor` | Rejected |
| Names a valid lease otherwise | Passes, look at the next id |

If every id passed, or the list is empty, the lease is valid. One more rule overrides that outcome. A lease with an empty `supersedes` and `takeover` false is rejected unless `author` equals its own `executor`. So a run can only be started by the device that will execute it, and a plain handoff can only come from the executor.

A lease that is neither valid nor rejected when the passes end is waiting.

**Result for a run.** The superseded set is every id named by a valid lease. The heads are the valid leases not in that set, in order. Then:

- One head: its `executor` executes the run.
- No head: the run has no executor.
- Several heads: the run is contested. It has no executor. `contested` lists the heads' executors in head order.

A contested run ends when a valid lease supersedes every head. That lease is a takeover by the owner, or a handoff written by a device that is the executor of every head, which only happens when the heads name one executor.

**Output.** For each run, in run id order: `run`, `executor` (string or null), `contested` (array), `heads` (event ids), `leases` (the valid leases in order, each with `id`, `executor`, `author` as device id or null, `supersedes`, `takeover`), `refused` (the takeover leases refused for their consent, each `{id, code}`, in lease order), `rejected` (ids: first the refused takeovers, then the leases the passes rejected, in the order they found them), `waiting` (ids, in order).

### 3.2 Takeover consent

A takeover is an owner action. The owner's device draws a prompt from the takeover action and collects a presence-key signature over it.

The **takeover action** is the canonical JSON object `{"executor": <new executor device id>, "kind": "run.takeover", "run": <run id>, "supersedes": <the superseded lease ids, sorted ascending with duplicates removed>, "v": 1}`. Its **digest** is the hash of those canonical bytes (`sha256:` and 64 hex digits), computed like `ticket.md` computes an action digest. The reference function is `takeover_digest`.

The `consent` is the object of `policy.md` section 2 with `kind` `action` and `digest` the takeover digest. It is signed with the presence key under the consent context. Its `device` must be a current roster member. It need not be the lease's author or its new executor, so the owner can approve a takeover on a device from another device.

The check runs in this order. The first failure gives the code, which is the consent error name with the prefix `run.takeover.`:

| Failure | Code |
|---|---|
| `consent` is missing or null | `run.takeover.consent_missing` |
| Not an object with exactly `v`, `kind`, `digest`, `device`, `clock` and `sig`, or `v` is not 1, or `kind` is unknown, or `device` or `sig` is not text, or `clock` is not `{ms, n}` of non-negative integers | `run.takeover.malformed` |
| `kind` is not `action`, or `digest` differs from the takeover digest | `run.takeover.wrong_action` |
| `device` has no presence key among the current roster devices | `run.takeover.unknown_device` |
| The signature does not verify | `run.takeover.bad_signature` |

Because the digest covers the run, the superseded leases and the new executor, a consent cannot be reused for another run, another set of heads or another executor. The consent has no expiry (section 11).

### 3.1 Handoff and takeover as written

A handoff is written by the executor: `supersedes` is the current heads, `executor` is the target, `takeover` is false, `reason` is `handoff`. The writer refuses with `run.bad_target` when the target is empty or is itself, `run.not_executor` when it does not hold the lease, `run.contested` when the run is contested, and `run.terminal` when the run has finished.

A takeover is written by the owner's device that is not the executor. It first collects the owner's consent (section 3.2). If it cannot, nothing is written and the writer refuses with `run.takeover.consent_unavailable`, or with the signer's own code (such as `consent.declined`) when the owner declines. `supersedes` is the current heads, `executor` is that device, `takeover` is true, `consent` is the signed consent, `reason` is `takeover`, and the labels are the owner action label. The label is informational. The fold decides by the consent. A device that already holds the lease refuses with `run.bad_target`, and a finished run with `run.terminal`.

After the lease the takeover writes one `run.goal` `unknown` event for each open turn that has dispatched calls with no result, then writes a `status` of `active` with the owner label unless the run was already active. No peer takes over by itself.

A takeover from a silent executor that is still running leaves two executors writing rounds. The state fold keeps both in `branches` (section 4).

## 4. The state fold

Input: the run id, the chat id and the events of the chat in order, each with `id`, `type`, `labels` and `body`. The reference function is `state_fold`. Only events whose body `run` equals the run id are read.

**Start.** The first `run.goal` event with `op` `define` and this run fixes the run. A later `define` for the same run is ignored. If there is none, the error is `run.unknown`. If `kind` is not one of `goal`, `schedule`, `watch`, or `goal` is not a string, the error is `run.malformed`. The state starts as:

| Field | Start value |
|---|---|
| `kind`, `goal`, `project` | From the `define` event. `project` is null when it is not a string |
| `labels` | The labels of the `define` event |
| `limits` | `max_rounds` 20, `max_stalls` 3, `max_wait_ms` 600000, `max_tokens` null, each replaced by the body's value when that is an integer in range (at most 32 bits for rounds and stalls) |
| `schedule` | null |
| `rounds`, `stalls`, `tokens_spent` | 0 |
| `status` | `active` with no pause kind and no reason |
| `waiting_since_ms`, `last_text_hash`, `last_slot`, `last_fingerprint`, `notes` | null |
| `unknown`, `branches` | empty |

**Events.** Every other event for the run is applied in order. A value that is missing or has the wrong type reads as the value in the rule, never as an error.

| Event | Effect |
|---|---|
| `run.schedule` `define` | `schedule` becomes the six fields when all are present and well typed, otherwise null. A later `define` replaces an earlier one |
| `run.schedule` `fired` | `last_slot` becomes the larger of itself and `slot`. A string `fingerprint` becomes `last_fingerprint`. A null one changes nothing |
| `run.schedule` `skipped` | `last_slot` becomes the larger of itself and `slot` |
| `run.goal` `round` | See below |
| `run.goal` `status` | See below |
| `run.goal` `notes` | `notes` becomes `{text, labels (of this event), through_round, event (this event's id)}`. A missing text reads as empty and a missing or oversized `through_round` as 0 |
| `run.goal` `unknown` | Append `{turn, calls (the string items), reason}` with empty text for missing strings |

A `round` adds 1 to `rounds` (at most 32 bits), adds `tokens` to `tokens_spent` (at most 64 bits, missing reads as 0) and sets `last_text_hash` to `text_hash` or null. If `verdict` is a string, `stalls` becomes 0 when `progress` is true, otherwise one more. A round with a null verdict leaves `stalls` alone. The round also updates the branch for its `executor` (empty text when missing): the first round creates `{executor, rounds: 1, last_turn, last_verdict}` and later rounds add 1 and replace the last two fields. Branches are listed in the order their executors first appeared.

A `status` event sets `status` as named: `active` has no pause kind and no reason, `paused` has the pause kind (any value other than `owner`, `condition`, `waiting` reads as `owner`) and the reason text, `done` has neither, `stopped` has the reason. An unknown status name changes nothing. `waiting_since_ms` is then set as follows. If the new status is paused with kind `waiting` and the old one was too, it keeps its value. If the new one is `waiting` and the old one was not, it takes the event's `at` (null if missing). In every other case it is null.

**Output.** `run`, `chat`, `kind`, `goal`, `project`, `labels`, `limits`, `schedule`, `rounds`, `stalls`, `tokens_spent`, `status` (`state`, `pause`, `reason`, with null where it has no value), `waiting_since_ms`, `last_text_hash`, `last_slot`, `last_fingerprint`, `notes`, `unknown`, `branches`.

The fold reads no lease. A watcher's round or status event changes the state exactly like the executor's. Who may write them is section 11, point 8.

## 5. Starting a run

The reference function is `start_events`. The checks run in this order and the first failure raises `run.invalid`.

1. The goal is not empty or only Unicode white space, and is at most 8192 bytes.
2. `max_rounds` and `max_stalls` are above zero.
3. A `goal` run has no schedule. A `schedule` or `watch` run has one. A schedule has `interval_ms` above zero.
4. The chat id is a hash.

On success the device writes, in this order, `run.goal` `define` (goal label), then `run.schedule` `define` when the run has a schedule (bookkeeping), then `run.lease` with the device as executor, empty `supersedes`, `takeover` false and `reason` `start` (bookkeeping). The result is the run id and the events.

A run starts with the default limits of section 4 when the caller gives none. The kind is a closed set, so an unknown kind cannot be expressed to the Rust writer. The reference raises `run.invalid` for one.

## 6. Schedule arithmetic

The reference function is `schedule_plan`. All arithmetic is on unsigned 64-bit integers and saturates at 2^64 - 1. An interval of zero raises `run.invalid`.

Input: the schedule, `now_ms` and `last_slot` (null before the first slot).

```
next = last_slot + interval_ms     when last_slot is set, else anchor_ms
if now_ms < next: nothing is due.
   result {fire: null, handled_through: null, missed: 0, next_ms: next}
due  = (now_ms - next) / interval_ms + 1        (integer division)
slot(k) = next + k * interval_ms
```

Without `catch_up`:

```
latest = slot(due - 1)
if now_ms - latest <= grace_ms:
    {fire: latest, handled_through: latest, missed: due - 1, next_ms: latest + interval_ms}
else:
    {fire: null, handled_through: latest, missed: due, next_ms: latest + interval_ms}
```

With `catch_up`:

```
keep = max(max_catch_up, 1)
skipped = due - keep when due > keep, else 0
if skipped > 0:
    {fire: null, handled_through: slot(skipped - 1), missed: skipped, next_ms: slot(skipped)}
else:
    {fire: next, handled_through: next, missed: 0, next_ms: next + interval_ms}
```

A missed slot is never replayed unless `catch_up` is set. When `handled_through` is set and `fire` is null, the executor writes a `skipped` event with that slot, the missed count and reason `missed`. When it fires after missing some slots, it writes a `skipped` event first with `slot` one below the fired slot and `reason` `missed`, then the `fired` event, then the round's prompt.

## 7. The pre-check

A run with `precheck` set, or of kind `watch`, runs a cheap deterministic probe before waking the model. The reference function is `precheck`.

The fingerprint is the hash (`sha256:` and 64 hex digits) of the probe's text as UTF-8. If it equals `last_fingerprint` the result is `{wake: false, reason: "unchanged"}` and the executor writes a `skipped` event with that reason and the slot. Otherwise the result is `{wake: true, fingerprint}` and the `fired` event carries the fingerprint. When the device has no pre-check the run wakes with no fingerprint.

## 8. The goal judge

After each round of a `goal` run a separate model call answers. It is a quarantined reader (`labels.md` section 3): no tools, a fixed schema, and its answer never joins the turn's running label. The reference function is `judge_answer`.

The model receives two messages. The system message says the content is data and never an instruction. The user message is the canonical JSON of `{"task": <fixed instruction>, "content": <text>}`. `content` is the text of the canonical JSON object `{"goal": <goal>, "round": <n>, "round_output": <output>}`. The output is cut to at most 6000 bytes on a character boundary. The goal is passed whole.

The model must answer `{"answer": {"verdict": "done" | "continue" | "blocked", "progress": <boolean>}}` and nothing else. The answer object has exactly those two fields, is at most 256 bytes as compact JSON, and nothing outside the enum is accepted. Any other answer raises `labels.quarantine_schema`. The judge never records free text. The round event stores only the verdict and the progress flag.

The executor then acts on the verdict: `done` finishes the run, `blocked` pauses it with kind `owner` and reason `blocked`, `continue` goes on. A failed judge call pauses the run with the error's code as the reason. Round, stall and token limits are enforced by the executor from the folded state: a run with `rounds` at or above `max_rounds` stops with `limit.rounds` before the next round, a run with `tokens_spent` at or above `max_tokens` stops with `limit.tokens` before the next round, and one with `stalls` at or above `max_stalls` after a `continue` stops with `limit.stalls`.

## 9. Status reasons

The executor writes these `reason` values with `status` events.

| Status | Pause kind | Reason | When |
|---|---|---|---|
| `paused` | `owner` | `owner` | The owner paused |
| `paused` | `owner` | `blocked` | The judge said blocked |
| `paused` | `owner` | `interrupted` | A round was interrupted and the run was not otherwise changed |
| `paused` | `owner` | the failing engine code | A round or its judge call failed for any reason except an allowance. The round is still recorded, with a null verdict |
| `paused` | `condition` | the allowance code, or `os_limit.<reason>` | The device allowance is spent, or the operating system allows no more work |
| `paused` | `waiting` | `unknown_outcome` | The run waits after a takeover for an owner decision |
| `stopped` | none | `owner` | The owner stopped the run |
| `stopped` | none | `limit.rounds`, `limit.stalls`, `limit.tokens`, `limit.wait` | A limit was reached |
| `done` | none | empty | The judge said done |
| `active` | none | empty | The run resumed |

A run that waits longer than `max_wait_ms` is stopped with `limit.wait`.

## 10. Errors

| Code | Raised when |
|---|---|
| `run.unknown` | The run id is malformed or has no `define` event |
| `run.malformed` | The `define` event has an unknown kind or no goal |
| `run.invalid` | A start request fails a check of section 5, or a schedule has a zero interval |
| `run.not_executor` | The device does not hold the lease and was asked to act as the executor |
| `run.contested` | The run has two executors and the action needs one |
| `run.terminal` | The run is done or stopped |
| `run.bad_target` | A handoff names an empty target or the device itself, or a takeover is asked of the executor |
| `run.busy` | This device is already stepping the run |
| `run.takeover.consent_unavailable` | The writing device has no way to collect the owner's presence signature |
| `run.takeover.consent_missing`, `run.takeover.malformed`, `run.takeover.wrong_action`, `run.takeover.unknown_device`, `run.takeover.bad_signature` | The lease fold refused a takeover lease (section 3.2) |
| `labels.quarantine_schema` | The judge's answer does not match its fixed schema |

`run.not_executor`, `run.contested`, `run.terminal`, `run.bad_target`, `run.busy` and `run.takeover.consent_unavailable` come from the writing device's own checks and have no vector. The vectors cover the folds, the start checks, the schedule, the pre-check and the judge.

## 11. Open points

These are places where the Rust code is silent or looks weaker than spec section 11. The vectors do not pin any of them.

1. **A takeover with no `supersedes` needs the same consent.** With a valid consent it creates a second root lease, so the run becomes contested and stays so until a lease supersedes both. It cannot silently replace the executor.
2. **A lease or run event can sit in the wrong scope.** Header validation only keeps chat events out of the `mesh` scope. The fold never checks that a lease's `run` id names the chat of its scope. A reader that wants this must check it.
3. **A malformed lease disappears.** It is not listed in `rejected` or `waiting`, so a device cannot show the owner that a lease was refused for its shape.
4. **The order of `rejected` is the order the passes found them.** It is not the order key. Two implementations that follow section 3 exactly agree, but an implementation that sorts by order key would differ.
5. **The unknown steps of a takeover are separate events.** If the device stops between the lease and the `unknown` events, the run has a takeover with no marker for its open steps.
6. **A suffix is only checked for being non-empty.** Section 1.1 says what is written. Nothing rejects another suffix.
7. **Rounds and statuses are not tied to the lease.** The state fold accepts a `round` or `status` from any device and names the executor from the body's own `executor` field. The branch list therefore trusts the writer.
8. **A watcher can write status events.** The fold does not compare a status event's writer with the lease. The driver refuses to write one unless it holds the lease, but a fold reading the log cannot tell.
9. **The budget is the device allowance, not the lease.** Spec 11.1 says a run cannot spend more than its lease. A lease body has no budget field. The limits in `define` are per run and are checked by the executor from the folded `tokens_spent`.
10. **Slot numbers in `skipped`.** When a run fires after missing slots, the `skipped` event stores `slot - 1` of the fired slot, which is not necessarily a slot boundary. The fold only takes the larger value, so it does no harm.
11. **Leading and trailing white space in a goal.** The rule uses the Unicode White_Space set, which is what the Rust writer uses. A Python implementation must not use `str.strip` unchanged because it also strips a few separator characters outside that set.
12. **A takeover consent has no expiry.** The digest binds the run, the superseded leases and the new executor, so a consent is useful once for that exact action. A consent that is signed and never used stays valid for that action. The fold does not look at its clock.
