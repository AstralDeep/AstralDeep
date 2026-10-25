# Native Mac Host Contract

## Existing Transport

Use register_ui.computer_host, computer_request/response/event/session/host; no new frame or agent_host support. Exact descriptor fields: host_id,name,platform,client_version,screens,verbs,protocol. Platform macos/protocol1.

Requests have type,request_id,session_id,verb,args,deadline_ms. IDs are creq_[0-9a-f]{32}; session canonical UUID4. Deadline is local receipt-relative, not a signed issue timestamp. Events/responses go through current authenticated channel only; never offline queue/replay. Existing heartbeat30s has no assumed periodic acknowledgment.

## Admission

Require execution-capable distribution, default-off consent, live OS permissions, current owner/server/channel, active matching session, valid implemented verb, bounded args, unexpired receive deadline and unseen ID. Bind ticket to epochs/args; recheck before each effect/chunk/response. Pause/Stop/lock/permission/root/connection changes invalidate pending work. At most4096 request IDs/live epoch and32 waiting operations; reject capacity, do not evict live replay identities. Bound retired-session ledger128, fail closed rather than resurrecting an old session. Existing2h hard cap.

## Existing Verbs and Bounds

Observe: screenshot,list_windows,get_clipboard,read_file,list_dir,wait. Input: click,double_click,right_click,move,drag,scroll,type_text,press_keys,focus_window,open_app,set_clipboard. Consequential: write_file,delete_path.

Text≤4000chars; clipboard≤16384chars; read≤262144bytes/default65536; write≤262144bytes; listing≤500entries. Screenshot width320–1920/default1280; JPEG complete frame≤4MiB. Screenshot results contain base64,media_type,width,height,scale. Coordinates use current screenshot geometry with per-axis scaling and topology checks; v1 does not prove browser-document identity. Scroll≤20notches; wait≤10s; server operation deadlines unchanged.

Closed key table/recognized modifiers, marked injected events. Missing input monitoring/unknown focus/secure fields fail closed. Open only validated application bundles via NSWorkspace; reject nonempty Mac application argv (qualified Windows launching remains unchanged). Known terminal bundle aliases require existing terminal_ok grant for keyboard input; no direct shell/subprocess path.

Files only within user-selected roots, opened-directory openat/O_NOFOLLOW traversal; reject parent/NUL/symlink/special-file escapes. Atomic create/refuse and explicit atomic overwrite. Delete files/empty directories only; never roots/recursive trees. Root restrictions govern file verbs; separately approved Terminal remains broader computer access.

## UI, Security and Distribution

Local consent/TCC/root controls are separate capabilities. Persistent accessible session panel offers Pause/Resume/Stop. Server-owned generated task/chrome/approval/results remain authoritative. Mac layout can adapt existing semantics without inventing capability catalogs.

DirectDownload variant: sandbox disabled, hardened runtime/registered identity retained. Store/release author-only; marker alone cannot enable sandboxed host. No IAM/provider tokens enter driver. Lower-trust screens/AX/files never authorize actions. Preserve policy/audit/PHI/egress. No sensitive content in repository evidence.

Delivery success is not semantic success. Unknown in-flight effects are never automatically replayed. Direct PTY, semantic browser tools or external drivers require separate additive contracts; no undeclared fallback.

## Same-Mac Review and Explicit Targeting

Mac pointer/text/key requests may carry an optional positive safe-integer `hwnd`, naming a current session-local AX window from list_windows. The exact target is part of reviewed arguments; native focus of that target is part of the reviewed effect. Revalidate AX process/window identity, geometry and actual focus immediately before input. No implicit previous-focus restoration; untargeted requests retain strict foreground checks.

A separate `approve_and_resume` decision may be offered only when the controller channel is also the selected host channel and the pending computer call has an exact reviewed hwnd, validated application target, explicit terminal_app confirmation or exact file path. The button and caption explicitly state that approval resumes control on that target. Ordinary Approve semantics remain unchanged. Combined review accepts only the expected single local-input pause transition, never manual pause, Stop, lock, disconnect, consent/root/owner/host/controller replacement. It revalidates originating task/owner/chat, explicitly resumes the current session, captures fresh authority and re-enters all normal source/risk/policy/audit gates before the stored effect once. Newly introduced risks need new review. Denial or stale/uncertain state cannot resume work.

`confirm_action.terminal_app` is optional closed server tool metadata. Only a recognized explicitly reviewed terminal target may request an admission grant; generic confirmation and file approval do not. The grant does not replace exact text/key review.

## Explicit Per-action Terminal Input

Mac type_text/press_keys may additionally name a canonical recognized terminal_app alongside reviewed hwnd and exact text/chord. The source-bound card includes terminal_input risk. Only a consumed current approval at the physical invocation can synthesize terminal_ok for that one stored call; native verifies the resolved window's actual terminal bundle matches. Changed window/arguments, duplicate invocation, expired/stale authority or new risks refuse the effect. This is an explicit terminal-specific confirmation for the action, not restoration of a180-second session grant. Existing live grants remain separately bounded and are cleared on pause.

## Negotiated Native Workbench Geometry

Opt-in requires exact mac_workbench_v1 capability and device.mac_workbench_v1=true on macOS/console-v2 profiles, including viewport refresh. Server ROTE derives optional console.mac_workbench(version1) split modes/widths and content/toolbar geometry. Native validates and consumes those values without new breakpoints; absent/malformed negotiation preserves the existing ConsoleShell. Other clients retain old console output when not opted in. No permission authority comes from this presentation flag.

## Local Control Authority

Consent and local Resume consume a fresh, one-use app-local HID input proof with a two-second monotonic lifetime. Missing proof, synthetic input and the host's own marked input cannot authorize these transitions; asynchronous UI callbacks gain no authority from a missing current event. Consent additionally requires active Astral focus and an available unlocked desktop. The nonactivating session panel may resume while an external application remains focused, after the same fresh physical-input proof and desktop checks. Disable, pause and disconnect invalidate outstanding proof. This is local input provenance, not remote cryptographic attestation.

Observation may include Astral, but execution cannot target Astral's own process or launch its own bundle. Pointer operations recheck the frontmost visible window at each affected point and refuse Astral overlays, including its floating controls. Keyboard and focus operations reject the host process. A safety mouse-up only releases at the actual current cursor; it cannot move to an old drag destination. Screens and model-selected coordinates never authorize consent, approval or root changes.

## Controller and Cancellation Lifetime

Computer approvals capture the original registered controller ConnectionContext object and its generation. A closing, unregistered, retired, replaced or changed-generation controller cannot approve, continue or deliver a queued effect. Recheck this captured authority under the physical session lock immediately before sending to the selected current host.

Every source-bound proposal, including direct calls without a serialized task origin, captures an immutable cancellation witness before awaited audit work. Stop invalidates matching owner/chat/controller pending and in-flight approvals even after a card has been removed from the pending map. A new turn resetting a cancel flag cannot revive the old witness. Retirement remains bounded and expired witnesses fail closed; no synthetic task origin is invented for direct calls.
