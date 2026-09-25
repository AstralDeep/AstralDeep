# Consumer contract

Consume Projection 1d4a864 `backend/webrender/chrome/console_model.py`, `backend/rote/console.py` and `contracts/fixtures/console/` without redefining them.

Registration uses `console_contract: console/v2` and exact supported types. `chrome_menu.model.console` supplies content; `rote_config.device_profile.console` supplies layout. Legacy negotiation retains server fallback. Add Windows qualification with the implemented consumer.

Run uses authenticated chat dispatch; Load only edits the draft, including exact-current-surface `compose_prompt`. Advanced uses `chrome_turn_selection_set` and correlated `chrome_surface.selection`. Preserve actionable errors and owner boundaries.

Preview/fullscreen reuse one canvas. Export/share retain owner/chat/render-revision validation. Do not create the web-only passive voice banner.
