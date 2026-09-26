# Console presentation data model

No new durable database entities or migrations.

## Console model

Optional versioned member of authenticated `chrome_menu.model`: server-owned labels, account-filtered scenario/category/agent catalog and ordered composer actions. Keys are bounded/unique. Prompts are text, never HTML. Actions use a closed set of existing local presentation and authenticated chrome/dispatch operations; existing server chrome controls roles/availability.

Model is connection/account scoped. Invalid versions show a labeled unavailable state and cannot dispatch unknown actions. Reconnect replaces it; account switch clears it. Settings remain existing server component trees.

## ROTE console presentation

Device descriptor declares known console contract plus actual viewport, ratio, touch/pointer, accessibility, supported types and voice capabilities. ROTE validates input and returns an optional versioned presentation in `device_profile`.

Fields describe navigation/sidebar, settings mode/axis, content spacing, scenario columns and preview sizing. Client renders received values without independently classifying its viewport. Malformed presentation grants no authority.

## Ephemeral state

Drawer/history expansion, catalog query/category, dashboard visibility, result expansion and focused field belong to one signed-in client. Conversation changes invalidate result contexts; account removal clears private catalog/draft/selection/result state.

## Turn selection

Reuse existing version-1 agent/skills/notes selection and server validation, scoped by owner and chat. Load changes only draft; Run follows normal send; stale/foreign/malformed selection fails closed. No note contents or credentials enter selection diagnostics.

## Evidence

Each screenshot records filename/hash, actual viewport/state/date, source/runtime identities and synthetic content description. Checkpoints distinguish specified, implemented, test-passing, live-verified and remotely persisted states.
