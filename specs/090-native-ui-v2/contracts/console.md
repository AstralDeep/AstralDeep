# Additive native console contract

Implementation intent; exact fixtures/manifest declarations accompany code and must pass before completion.

## Negotiation

- Device descriptor declares `console_contract: "console/v2"`; absent/unknown values retain legacy behavior.
- Authenticated `chrome_menu.model.console` contains a version discriminator and shared presentation content.
- Existing `rote_config.device_profile.console` contains versioned layout derived by ROTE from viewport/capabilities. No new push type/auth mechanism.
- Six existing v2 types are delivered only when console contract and exact supported type are advertised. Otherwise retain existing ROTE fallbacks. Windows remains excluded from this feature until its separate implementation. Apple, watch and Android clients negotiate console/v2 with their actual supported types; watch retains its primitive fallbacks.
- Watch console support uses stack navigation, one column, full-screen pushed surfaces and capability-based `availability` objects on catalog/menu/composer items. A handoff carries a server message; it must not be presented as a working native action. Generic watch picker support remains excluded; authenticated strict notes forms use the scoped guidance capability.
- Supported-type/rendering-capability changes must trigger re-adaptation, including at unchanged width.

## Shared content and placement

- Landing scenarios/categories/agents reuse web's host-authorized builder.
- Navigation: brand/dashboard, History/New chat, Agent Directory/search, account/Settings.
- Composer: attachment, voice, More and Send. More carries ordered background, Advanced settings, Workspace timeline and available Pulse entries from the server.
- The bottom web voice availability warning is excluded from native presentation by owner clarification. Native voice controls and actionable session errors remain; live worker verification is required.
- Settings uses existing menu groups/items and server surfaces. Existing intentional web-only exclusions remain.
- Agent intro offers Run via normal authenticated chat and Load prompt via current-account draft only.

## Responsive reference

- At least1280:350-point sidebar;1024–1279:288-point sidebar; below1024:accessible drawer/toggle/backdrop.
- Below768:bottom composer, one-column content and full-screen settings with horizontal navigation.
- `dialog_width` is the viewport on phones/watch, otherwise min(640, viewport width minus48), separate from settings width.
- Compare logical dimensions to CSS pixels. ROTE selects layout; safe areas/keyboards keep controls reachable.
- Result preview/fullscreen share component identities/authority/state; no agent rerun or duplicate live canvas.

## Validation

Reject malformed versions/unknown commands; bound inputs; render text safely; clear private state on account removal. Existing owner/request/connection fences and mandatory setup remain. Selection responses bind to the outstanding request before replacing selected state.

## Verification

Fixtures cover malformed models, width boundaries, capability changes, legacy output, theme, fallbacks, owner catalog filtering and stale actions. Native tests cover rendering/dispatch; web regressions prove shared extraction preserves accepted UI behavior.
