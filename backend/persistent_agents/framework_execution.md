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

This source prerequisite depends on Plane's new operation-bound capture API.
AstralDeep's composition and Plane86 submodule pin are deliberately unchanged
until the exact component revision completes independent qualification. Local
integration tests may explicitly place the separate candidate's `src` directory
on `PYTHONPATH`; that is unadopted dependency evidence, not a production pin or
release. Both existing framework-credential and persistent-agent flags retain
their default-off posture. Context packing/compaction, when enabled, still requires
the current owner data-sharing acknowledgement, exact USER configuration and matching
operator budget; missing or changed context fails closed before sending.
