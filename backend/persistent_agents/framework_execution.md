# Framework source-less chat execution

Framework operations retain their `framework` / `credential` authority. The worker
captures a private, operation-bound credential observation from AstralPlane and
uses the existing assignment claim, resource reservation, renewal, send,
observation, completion and retained-result fences. Admission is consumed only
when a new operation is accepted. No user JWT, current owner session, downstream
tool or delegated agent authority is supplied by this path. Fixed model reservations
use the captured conversation ID, or the real Work assignment ID when no conversation
was supplied. Existing context usage records persist and recover under that owner-bound
key without creating a history conversation.

Only the existing fixed, source-less chat shape is supported. Sources, tools,
offline grants, selected guidance and retained-source references remain unavailable. Provider configuration
must satisfy the existing exact default OpenAI profile. A future owner-selected
harness needs its own immutable qualified binding; this change does not select a
local model, change the profile default or qualify a live provider.

The framework execution principal is labelled with its opaque credential ID in
the private invocation and model audit. Credential secrets and hashes stay out of
public operations and audit metadata. Current owner, credential, scope, operation,
control, instruction, configuration and execution-fence changes fail closed;
responses received after authority loss remain quarantined.

This integration consumes Plane's operation-bound capture API at exact qualified
revision `c311b978697560d7b3c9c42232370695bc91ec64`, package0.1.7 and schema089.013.
[Plane107](https://github.com/AstralDeep/AstralPlane/pull/107) qualifies that API;
[Plane106](https://github.com/AstralDeep/AstralPlane/pull/106) supplies the inherited
repeat-safe populated089.012-to089.013 project migration and recovery evidence.
The composition pin and package declaration move together. Tests using a separate
candidate source are historical dependency diagnostics; installed-wheel, full consumer
and actual configured SDK/provider qualification remain distinct gates.
Both existing framework-credential and persistent-agent flags retain
their default-off posture. Context packing/compaction, when enabled, still requires
the current owner data-sharing acknowledgement, exact USER configuration and matching
operator budget; missing or changed context fails closed before sending.
