# Native UI v2 research

## Decisions

- **Reuse account-filtered landing data.** `backend/orchestrator/web_landing.py` owns scenarios/category order/visible agents. Its web-only restriction reflects feature089's old scope. Native registration will reuse the builder rather than copy prompts into apps.
- **Negotiate additive chrome.** `chrome_menu` tolerates extra model fields; deliver console copy/catalog/action placement for a declared console contract. Existing topbar/menu/signout definitions remain reusable. No new push-frame type is necessary.
- **Consume ROTE.** Apple ignores `rote_config` and locally selects obsolete canvas/chat layouts. ROTE will return authoritative navigation mode/sidebar width, settings presentation/orientation, scenario columns and preview constraints.
- **Separate platform from form factor.** iPad remains `ios` with its logical viewport/scale/input. Narrow Mac/split-view windows receive the same layout as comparable web widths. Effective capability changes trigger re-adaptation even without resizing.
- **Opt in to existing primitives.** The six v2 types already exist in Primitives/manifest but are force-degraded for every native profile. Negotiated renderers receive only advertised types; unsupported types preserve values through existing fallbacks. Windows keeps old dispositions pending its separate implementation; watch preserves primitive fallbacks while negotiated console support is prepared.
- **Preserve authority seams.** Apple AppModel owns chat fencing/drafts/PKCE/uploads/voice/export/share/history. Replace presentation rather than duplicate services; apply the same approach to Android after Apple verification.
- **Close Advanced selection parity.** Web opens guidance with `view=selection` and advertises `guidance_selection_v1`; Apple has no consumer. Native guidance uses a notes-only adapter, so selection must use the correct shared builder. Selection accompanies ordinary `chat_message` and clears on account/chat changes.
- **Preserve fullscreen state.** The newest live workspace has one authoritative state/capture identity. Presentation changes retain tabs/forms/charts, reject stale actions and exit on conversation reset.
- **Use the web font.** Web serves Open Sans; shared Apple core names Inter/JetBrains Mono. App-target typography/resources can match web with shared native appearance sources for the watch implementation. Preserve licensing and record conversion.

## Alternatives considered

Copying menu/scenario definitions creates another UI authority. Device-name layout selection fails tablets/split view. Replacing apps with browser wrappers risks native capability/voice/attachment paths. Advertising unimplemented types loses output. Reusing the August local image would create stale references. Each is rejected.

## Setup

Deep baseline `341f58c5625d7dda530720e4cac6b3d35ae47492`; Projection `7cb7e25483e023df68f3c6a762ef87b2e36417fb`. Origins expose only main; initial product trees were clean. Feature090 was collision-checked across all spec trees. Docker was stopped. ARM build lacked OpenSSL development libraries for aicspylibczi; amd64 retry was interrupted by Docker updating. After the owner reported ready, Docker 29.8.0 and Xcode27.0/27A266a respond; current ARM rebuild includes the missing build prerequisite. Live captures remain pending.
