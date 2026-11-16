# PHI detector `astral.phi-detector/v1`

The detector floor labels content on the device that holds it. It reads text or structured tool output and returns a verdict of `phi`, `uncertain` or `none` with the rules that fired. A clear match is PHI. An uncertain match asks the owner, or is PHI when nobody can be asked (spec O23 and C17). The Kernel detector, the node gates and the Apple on-device adapter all implement against this document and its vectors.

The floor ports the deterministic rules of `backend/personalization/phi_gate.py` and `backend/shared/phi_redactor.py`. It does not port Presidio, which is a language model the Kernel cannot run. Existing roster v2 stores and node data are untouched. This contract adds no event type, no roster field and no stored data.

Reference code: `reference/phi_detector.py`. Tables: `reference/phi_rules.json` (the rules), `reference/phi_fold.json` (the skeleton fold, from `reference/generate_phi_fold.py`), `reference/html_entities.json` (from `reference/generate_html_entities.py`) and `reference/phi_filter.json` (the on-device filter). Generator: `reference/generate_phi_vectors.py`. Vectors: `vectors/phi-detector-v1.json`. LETS-CJ/1 comes from the preserved mesh reference.

Every table is data. Implementations load the committed bytes and pin their digests, which the vectors record under `constants`.

## 1. Input

The input is text or a JSON value, such as structured tool output. A value whose root is a string is text. The data model is that of LETS-CJ/1 with floats added: objects with text keys, arrays, text, integers in signed 64 bits, finite floats, booleans and null.

| Check, in this order | Result |
|---|---|
| Text that is not valid Unicode (a lone surrogate) or a value outside the data model (an integer outside signed 64 bits, a float that is not finite, a key that is not text) | `phi`, reason `encoding` |
| A value nested deeper than 64 arrays and objects | `phi`, reason `too_large` |
| More than 1048576 bytes: the UTF-8 of text, or the LETS-CJ/1 bytes of a value | `phi`, reason `too_large` |

LETS-CJ/1 has no floats, so a value's size is the LETS-CJ/1 length of the value with each float written as `0`, plus 23 bytes for each float, which counts every float as 24 bytes, the longest shortest form of a double.

A value is walked depth first. Object members go in LETS-CJ/1 key order, which is code point order. Each member gives its key as a leaf, then the leaves of its value. Arrays go in index order. A string is a leaf. Numbers, booleans and null are not leaves, as in today's redactor. A number written inside text is still read by the text rules, and a number under a PHI key still fires the key rules. Text input is one leaf.

## 2. Output

```
{"v": 1, "verdict": "phi" | "uncertain" | "none", "reason": null | "too_large" | "encoding" | "detector_failed", "findings": [...]}
```

Each finding is `{"rule", "tier", "path", "part", "start", "end"}`.

| Field | Meaning |
|---|---|
| `rule` | A rule id from section 5 |
| `tier` | `clear` or `uncertain`, the rule's tier |
| `path` | `null` for text input. Otherwise the steps from the root to the leaf. A step is an integer: the index in an array, or the member index in key order in an object |
| `part` | `text`, `key` or `value` |
| `start`, `end` | Code point offsets into the leaf's normalized text (section 3) |

A path never holds key text, because a key can itself be the matched text. No verdict, error or log line carries input text.

Findings go in leaf order, then by `start`, then by `rule` id in code point order, then by `end`. Findings with the same leaf, rule, start and end merge into one. The verdict is `phi` when any finding is `clear`, `uncertain` when the findings are all `uncertain`, and `none` when there are none. When `reason` is set the verdict is `phi` and `findings` is empty.

## 3. Normalization of a leaf

Each leaf goes through these steps in this order.

1. **HTML character references.** The reference reproduces Python's `html.unescape` exactly, from `html_entities.json`. Its `pattern` finds each reference, leftmost first, in one pass over the text. A numeric reference is decimal or hexadecimal, with or without a closing semicolon. Its value is looked up in `numeric_replaced` first (the cp1252 remap of 0x80 to 0x9F, and 0 and 13). A surrogate or a value above 0x10FFFF then gives U+FFFD, whatever the number of digits. A value in `numeric_removed` gives nothing. Any other value gives its code point. A named reference is looked up in `named` with its semicolon when it has one. Failing that, the longest leading part of at least two characters found in `named` is replaced and the rest kept, which only the legacy names without a semicolon can match. Failing that, the text stays as written. So `a&#64example.com` reads as an email address.
2. **Percent decoding.** Up to five passes. A pass runs only when the text holds `%` and two hex digits. It turns each such escape into its byte, keeps every other character as its UTF-8 bytes, and reads the result as strict UTF-8. Bytes that are not valid UTF-8 give `phi` with reason `encoding`. So does an escape still present after the fifth pass.
3. **The skeleton.** Every code point maps to one ASCII character through `phi_fold.json` (section 3.1). The map is one to one, so an offset in the skeleton is an offset in the decoded text.
4. **URL tokens.** Each match of the `url` pattern in the skeleton (`http` or `https` in lowercase, as today) is copied once more with each run of `/ _ . ? & = + % : -` replaced by a space. When there is at least one, a newline and the copies joined by newlines follow the skeleton. This is the first segment.
5. **The text as written.** When steps 1 and 2 changed the leaf, the skeleton of the leaf as written is a second segment. Today's prefilter and redactor read the undecoded text, so `900-12-3456%41` still shows its SSN.

The normalized text is the first segment, followed by a newline and the second segment when there is one. Rules run on each segment on its own, so no match spans both. A finding in the second segment has offsets counted from the start of the normalized text.

In the first segment an offset below the length of the decoded text names the same code point of the decoded text, the leaf after HTML and percent decoding, and an offset past it falls in the appended URL copies, which have no single source position. In the second segment an offset, less the start of that segment, names the same code point of the leaf as written.

### 3.1 The skeleton fold

`generate_phi_fold.py` builds the map from Unicode 16.0 data with these rules, the first that applies winning.

| Code point | Maps to |
|---|---|
| ASCII U+001C to U+001F | A space |
| Any other ASCII | Itself |
| A decimal digit (category Nd) | Its ASCII digit |
| U+0130 and U+0131 | `I` and `i`, which Python's case-insensitive matching equates with ASCII letters |
| One whose NFKC form is one ASCII character of the same class, except the forms of `<`, `>`, `"` and `'` | That character |
| White space | A space |
| Any other letter or number | `x` |
| Anything else, combining marks included | `~` |

The class of a character is the one Python's regular expressions give it: word (`\w`), space (`\s`) or other. The fold keeps that class for every code point, and keeps every decimal digit a digit. So `\b`, `\s` and the classes of the rules read the skeleton exactly as today's rules read the text. `--check` proves it for every code point and fails on any other Unicode version.

| Fold choice | Why | Vectors |
|---|---|---|
| Combining marks become `~` | Python reads a mark as a boundary, so `e` and U+0301 before an SSN still lets it match | `fold_mark_before`, `fold_mark_after`, `name_nfd` |
| Numbers that are not digits, such as U+00BD, become `x` | Python reads them as word characters | `fold_numeric_word` |
| A compatibility form keeps its character only when its class stays, so U+24D0 and U+FF3F become `~` | Python reads them as boundaries | `fold_circled_letter`, `fold_fullwidth_low_line` |
| Forms of `<`, `>`, `"` and `'` become `~` | Today's URL pattern reads them as part of a URL | `url_fullwidth_stop` |
| C0 controls stay themselves, except U+001C to U+001F | Python reads them as part of a URL and as boundaries | `fold_control`, `url_control` |
| U+0130 and U+0131 become `I` and `i` | Today's case-insensitive labels match them | `fold_dotless_i`, `fold_dotted_capital_i` |

### 3.2 HTML table

`generate_html_entities.py` dumps `html.entities.html5` (every name with and without its semicolon), the remap and removal tables and the reference pattern from Python 3.14's `html` module. It refuses any other Python. No implementation calls its runtime's HTML decoder.

## 4. The pattern dialect

Rule patterns use only what Python `re` with `re.ASCII` and the Rust `regex` crate read the same way: literals, classes, `\d`, `\s`, `\b`, quantifiers, alternation, non-capturing groups, and `(?i)` at the start of a pattern or as a group `(?i:...)` around part of one. There is no lookaround, backreference, atomic group, possessive quantifier, `\w`, `^` or `$`. No pattern matches empty text. Matching is leftmost first and non-overlapping, rule by rule.

Patterns run on the skeleton, which is ASCII, so ASCII and Unicode modes of either engine agree. The reference compiles them with `re.ASCII`. The Kernel compiles them in the `regex` crate's default Unicode mode, which reads an ASCII haystack the same way.

The email patterns take time quadratic in the length of a letter run in a backtracking engine such as Python's, as today's gate does. A run of 40000 letters takes close to a second there. The vectors near the size cap use short tokens. The Kernel's engine is linear.

## 5. Rules

`phi_rules.json` holds one entry per rule: its id, tier, where it runs (`leaf` for every leaf, `key` for keys only), its pattern or token runs, the value it needs for key rules, and the names of today's patterns it comes from.

| Rule | Tier | Runs on | Today's source |
|---|---|---|---|
| `ssn` | clear | every leaf | `_PREFILTER_PATTERNS`, `_SHARE_IDENTIFIERS`, `PHI_VALUE_PATTERNS` |
| `email` | clear | every leaf | `_PREFILTER_PATTERNS`, `_SHARE_IDENTIFIERS` |
| `phone` | clear | every leaf | `_SHARE_IDENTIFIERS` |
| `labeled_id` | clear | every leaf | `_SHARE_IDENTIFIERS`, rewritten without its lookahead |
| `labeled_dob` | clear | every leaf | `_SHARE_IDENTIFIERS` |
| `labeled_phone` | clear | every leaf | `_SHARE_IDENTIFIERS` |
| `street_address` | clear | every leaf | `_SHARE_IDENTIFIERS`, full suffix words |
| `mrn` | clear | every leaf | `_PREFILTER_PATTERNS`, `PHI_VALUE_PATTERNS` |
| `name_labeled` | clear | every leaf | `_SHARE_PATIENT_PREFIX` with its label word, new |
| `field_key_phi` | clear | keys, present value | `PHI_FIELD_PATTERNS` |
| `name_bare_prefix` | uncertain | every leaf | `_SHARE_PATIENT_PREFIX` without a label word, new |
| `name_before_suffix` | uncertain | every leaf | `_SHARE_CLINICAL_SUFFIX`, new |
| `field_key_phi_blank` | uncertain | keys, blank value | `PHI_FIELD_PATTERNS` |
| `date_iso`, `date_slash` | uncertain | every leaf | `_PREFILTER_PATTERNS` |
| `date_ymd`, `date_mdy` | uncertain | every leaf | `PHI_VALUE_PATTERNS` |
| `digit_run` | uncertain | every leaf | `_PREFILTER_PATTERNS` |
| `medical_record` | uncertain | every leaf | `_PREFILTER_PATTERNS` |
| `ipv4` | uncertain | every leaf | `PHI_VALUE_PATTERNS` |
| `phone_loose` | uncertain | every leaf | `_PREFILTER_PATTERNS`, `PHI_VALUE_PATTERNS` |
| `email_like` | uncertain | every leaf | `PHI_VALUE_PATTERNS` |
| `street_address_abbrev` | uncertain | every leaf | `_SHARE_IDENTIFIERS`, abbreviated suffixes |
| `field_key_generic` | uncertain | keys | `PHI_FIELD_PATTERNS` |

Every pattern is today's pattern on the skeleton, or a union of today's patterns, with `\w` written as `[A-Za-z0-9_]`. So any text today's pattern matches gives a match on its skeleton.

- **Loose forms.** Today's prefilter phone takes optional separators, and the redactor's phone takes a one or two digit country code. The redactor's email takes a one letter ending such as `a@b.c` and versions such as `react@18.3.1`. These are `phone_loose` and `email_like`, uncertain, so a plain date or package pin asks rather than labels.
- **Street addresses.** A street address that ends in `street`, `road`, `avenue`, `lane`, `drive` or `boulevard` is clear. One that ends in `st`, `rd`, `ave`, `ln`, `dr` or `blvd` is uncertain, because "Seen 12 patients today by Dr. Smith" matches it. `blvd` is an abbreviation, so it is uncertain.
- **The labeled identifier.** Today's pattern needs a digit somewhere in the run of letters, digits and hyphens after the label, even past the end of the matched token. The rewrite takes the token when it holds a digit. Otherwise it takes the run up to a following underscore, which is the only case where the digit lies past today's token (`MRN: ABC-123_x`). The two match the same inputs at the same starts, and `test_phi_detector_parity.py` proves it on the corpus and on every short identifier.
- **Names.** A name is clear only when a label names it: `patient` or `subject` followed by `name`, `identifier`, `id` or `record`, or by a line that starts `name:`. A name after a bare `patient` or `subject`, and a name before a clinical suffix such as `was diagnosed with`, are uncertain, so "Patient Safety Committee meets Tuesday" and "Outcomes in Older Adults Diagnosed With Heart Failure" ask rather than label. A name is two or three words, each starting with a capital ASCII letter or `x` and ending with a lowercase letter, `x` or `~`, so `José` written with a combining accent stays one word. Each rule writes its prefix or suffix and the name as one pattern, which needs no end anchor, finds "Dear Patient Avery Quill", and runs in one linear pass. The case-insensitive words sit in `(?i:...)` groups. Today these patterns run only around a Presidio name, so the parity proof does not cover them. They are new and their vectors pin them.
- **PHI field keys.** A key whose text holds `ssn`, `social_security`, `mrn`, `medical_record`, `patient_id`, `dob`, `birth`, `birthdate`, `health_plan_id`, `insurance_id` or `beneficiary`, in any case and with `_`, `-`, `.`, a space or nothing between its words, fires on its value. A blank value (the empty string, the integer 0 or false) is uncertain. An absent value (null, an empty array or an empty object, which today's redactor leaves unchanged) fires neither rule. Any other value is present and clear.
- **Generic field keys.** A key is split into tokens: each run of ASCII letters and digits, split again where a lowercase letter meets a capital, where a letter meets a digit, and before the last capital of a run of capitals that a lowercase letter follows. Tokens compare in lowercase. A generic pattern from the redactor's field list counts when its words equal a run of consecutive tokens, so `user_name`, `deviceId`, `IPAddress` and `address2` are uncertain and `filename` is not. Splitting at every separator and at digits keeps `user/name` and `phone2` asking, as today. A token also counts when it contains a word of the rule's `glued` list, such as `lastname`, `zipcode`, `phonenumber` or `email`, so common keys written without separators still ask. Every occurrence of every token pattern and glued word is a finding, and overlapping findings are all kept. A finding spans from the start of its first token to the end of its last. The value does not matter.

## 6. Failure

| Reason | When |
|---|---|
| `too_large` | Input over the size cap, or nested deeper than the depth limit |
| `encoding` | Input outside the data model, invalid UTF-8 after percent decoding, or an escape left after five passes |
| `detector_failed` | Any internal error, such as a caught panic in the Kernel |

Each gives `phi`. No setting turns a reason into a lower verdict.

## 7. Parity with today's rules

The parity oracle is the deterministic part of today's gate: the prefilter of `contains_phi`, the decoding and `_SHARE_IDENTIFIERS` of `contains_phi_for_sharing` (with its size and encoding refusals), `PHI_VALUE_PATTERNS` on every string, and the redactor's field keys with a value it would mask. Presidio is not part of it. `backend/tests/test_phi_detector_parity.py` runs those live rules over every vector and the corpus. Every input they flag gives `phi` or `uncertain` from the floor, except the listed token rule differences. The test reads that list from the vectors and checks each one is flagged only through a generic key that the token rule reads differently.

The token rule differences are `field_key_token_filename`, `field_key_token_hostname`, and corpus rows `corpus_08018`, `corpus_08027`, `corpus_08036`, `corpus_08063`, `corpus_08090`, `corpus_08099` and `corpus_08189`. Each is a key such as `hostname` or `userzip` that holds a generic pattern inside one token and no glued word.

Known limits:

- A character the fold does not equate with ASCII, such as a Cyrillic letter in an SSN label, hides the label.
- A name with no clinical context is caught today only by Presidio. The floor misses it, and the on-device model may raise it.
- Today's prefix check looks back at most 160 characters. The floor's name pattern has no such window, so it can match more.
- The fold uses Unicode 16.0. The production gate runs on Python 3.11 with Unicode 14.0, which reads a character assigned since then as unassigned. A letter new in Unicode 15 or 16 glued to an identifier is a boundary to today's gate and a letter to the floor.

## 8. The on-device filter `astral.phi-filter/v1`

`phi_filter.json` fixes the request to the on-device model and the check of its answer. The Kernel pins its digest and the Swift test bundle carries a byte copy.

- **What is screened.** Only content the floor marked `none`, as text, at most `budget_bytes` (16384) of UTF-8 per append. A cut never splits a code point. The text is cut into chunks of at most `chunk_bytes` (4096), again at code point boundaries, with no overlap. Text past the budget is screened by the floor alone.
- **The request.** A quarantined reader call whose instructions are the `instructions` text and whose prompt is the chunk exactly. The answer must follow `schema`: an object with one field `findings`, a list of at most 32 objects with exactly `kind` (one of `kinds`) and `text` (1 to 64 code points).
- **The answer check.** The answer parses as strict LETS-CJ/1 JSON. Any other shape, any float, a duplicate key, an unknown kind, a missing or extra field, or a text outside its length is a failure. The call also fails when it errors, refuses, overflows the context or runs past `timeout_ms` (20000) on the Kernel clock. A busy model is waited for inside that time. A failure gives `phi` with reason `detector_failed`.
- **Counting a finding.** A finding counts only when its text occurs in the chunk. Both are compared after the fold of `phi_fold.json`, with each ASCII letter that a run of `~` follows written as one `x`, ASCII letters in lowercase and each run of white space as one space, trimmed. So precomposed and decomposed accents compare equal. Every counted finding is `uncertain`. An empty answer, or one whose texts do not occur, changes nothing.

The chunk size and the timeout are owner decisions. The values above are defaults.

## 9. Vectors

`vectors/phi-detector-v1.json` holds:

- `constants`: the format, limits, table digests, reasons, the corpus seed, the synthetic values and `token_rule_differences`.
- `detect`: an input (`text`, `json`, or a recipe `text_repeat` of `unit` repeated `count` times then `tail`, or `json_repeat` of an array of `item` repeated `count` times) and the full output.
- `normalize`: a text and its decoded text and segments, or reason `encoding`.
- `corpus`: 8231 inputs from templates over the synthetic values with a fixed seed, each with its verdict and the sorted ids of the rules that fired.
- `filter_chunks`: a recipe and the byte length of each chunk. `filter_answer`: a chunk, a model answer and the outcome.

Every rule has a positive and a negative case. Every row of `backend/tests/fixtures/phi-clinical-controls.json` is a case. Other groups cover HTML numeric, named and legacy forms, percent escapes nested to five and six passes and invalid UTF-8, URLs with underscores, non-ASCII hosts and control characters, fullwidth and other Unicode digits, each fold choice, field keys by token, by glued word and by substring, each value class under a PHI key, unscreened integers and floats, multilingual benign text, and inputs at and one past the size and depth limits. Test values are synthetic. No case reads a clock or the network.

Regenerate with `python specs/106-sign-in-free-clients/contracts/reference/generate_phi_vectors.py` and verify with `--check`, which writes nothing. The backend parity test reads byte copies of the vectors and `phi_rules.json` from `backend/tests/fixtures/phi_detector/` and checks them against these files.

## 10. Versioning

A change to a rule, a table or the output raises the format version. This document, the tables, the vectors and the reference change together, and the generators' `--check`, the reference tests and the parity test pass before a change merges.
