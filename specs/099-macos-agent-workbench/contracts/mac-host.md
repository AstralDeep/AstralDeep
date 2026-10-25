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
