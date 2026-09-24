"""Reconciles canonical tutorial content at startup through Plane's revision API.
Creates missing steps and refreshes system-owned copy while preserving admin edits.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Final

from orchestrator.plane_repository_context import PlaneRepositoryContext, repository_from

logger = logging.getLogger("Onboarding.Seed")

_SEED_EDITOR: Final = "system:tutorial-seed-030"


def _step(
    slug: str,
    audience: str,
    display_order: int,
    target_kind: str,
    target_key: str | None,
    title: str,
    body: str,
) -> MappingProxyType:
    return MappingProxyType(
        {
            "slug": slug,
            "audience": audience,
            "display_order": display_order,
            "target_kind": target_kind,
            "target_key": target_key,
            "title": title,
            "body": body,
        }
    )


DEFAULT_TUTORIAL_STEPS: Final = (
    _step(
        "welcome-tour", "user", 10, "none", None, "Welcome to AstralDeep",
        "Ask in plain language and your agents answer with interactive results. This tour opens the controls it describes on desktop and mobile. Use Next and Back, or Skip tour at any time. Replay it from Settings → Take the tour.",
    ),
    _step(
        "meet-the-canvas", "user", 20, "static", "canvas.workspace",
        "The canvas — where results appear",
        "Your conversation and interactive results appear here. Scroll through the chat to revisit answers. Each component's toolbar lets you expand, download or share it. Before a chat starts, the dashboard offers example requests.",
    ),
    _step(
        "turn-on-agents", "user", 30, "static", "sidebar.agents",
        "Turn your agents on",
        "Enable agents in Agents & permissions. Open an agent, review its tools, then save the permissions you want to grant. New accounts start with agents switched off. The tour only shows these controls; it does not change your permissions.",
    ),
    _step(
        "ask-in-plain-language", "user", 40, "static", "chat.input",
        "Ask in plain language",
        "Type your request here, then press Enter or the send arrow. Shift+Enter adds a line. Use the paperclip for files and the microphone for voice. The three-dot menu contains Advanced options, background work and the Workspace timeline.",
    ),
    _step(
        "open-settings-menu", "user", 50, "static", "topbar.settings",
        "Settings — everything else lives here",
        "Settings is the gear beside your account at the bottom of the sidebar. On a phone, open the hamburger menu to find it. Settings pages use a navigation rail on wide screens and a horizontally scrollable navigation row on small screens.",
    ),
    _step(
        "agents-and-permissions", "user", 60, "static", "sidebar.agents",
        "Agents & permissions",
        "Browse available agents, including built-in agents under Public. Open an agent to review tool permissions and save any changes. You can return here to reduce or revoke access whenever you need.",
    ),
    _step(
        "configure-your-model", "user", 65, "static", "sidebar.llm",
        "Connect your AI provider",
        "LLM settings holds your provider, endpoint, model and optional TypeSafe routing in one place. Review the data-sharing acknowledgement before saving. Saved API keys stay hidden; test your connection after changing the configuration.",
    ),
    _step(
        "personalize-your-assistant", "user", 70, "static", "sidebar.personalization",
        "Make it yours",
        "Use Personalization to set your profession, goals and preferred tone, and review memory and background activity. Your preferences shape answers without changing tool permissions or privacy controls.",
    ),
    _step(
        "review-your-audit-log", "user", 80, "static", "sidebar.audit",
        "Your private audit log",
        "Review recorded activity for your account, including agent actions and tool calls. Filter by event class or outcome, then open an entry to inspect its details.",
    ),
    _step(
        "workspace-timeline", "user", 90, "static", "topbar.timeline",
        "Step back through your workspace",
        "Workspace timeline is in the three-dot menu beside the message box. It opens snapshots from earlier turns, so you can review how a result developed without changing your live workspace.",
    ),
    _step(
        "help-anytime", "user", 100, "static", "sidebar.guide",
        "Help, whenever you need it",
        "The User guide explains attachments, voice, themes, privacy and other features. Take the tour is a separate Settings page whenever you want to repeat this walkthrough.",
    ),
    _step(
        "tour-complete", "user", 110, "none", None, "You're all set",
        "Close the tour and ask a question, or try a dashboard example. Your chats stay in History in the sidebar. Use New Chat for a separate conversation.",
    ),
    _step(
        "admin-tool-quality", "admin", 200, "static", "sidebar.tool-quality",
        "Admin: Tool quality",
        "Review flagged tools and their recent failures or feedback. Tool quality also has runtime diagnostics. Tutorial editing lives on its own Tutorial admin page.",
    ),
    _step(
        "admin-knowledge-proposals", "admin", 210, "static", "sidebar.tool-quality",
        "Admin: Knowledge proposals",
        "Inspect the evidence behind a flagged tool and any proposed knowledge update before deciding whether to apply it. Opening this page or taking the tour does not approve changes.",
    ),
    _step(
        "admin-edit-this-tour", "admin", 220, "static", "sidebar.tutorial-admin",
        "Admin: Edit this tour",
        "Edit step titles, text, audience, order and highlight targets here. You can add, archive or restore steps. Changes apply when the next tour starts, and revision history preserves edits. Product updates preserve your customized steps.",
    ),
)


def seed_tutorial_steps(
    *,
    plane_runtime,
    plane_repositories=None,
    tutorial_repository=None,
) -> int:
    repository, runtime = repository_from(
        "tutorials",
        plane_runtime=plane_runtime,
        repositories=plane_repositories,
    )
    context = PlaneRepositoryContext(
        repository=tutorial_repository or repository,
        plane_runtime=runtime,
    )
    observed_at = datetime.now(timezone.utc)
    created = 0
    refreshed = 0
    with context.transaction() as transaction:
        for values in DEFAULT_TUTORIAL_STEPS:
            result = context.repository.create_seed_if_absent(
                transaction,
                **values,
                editor_id=_SEED_EDITOR,
                observed_at=observed_at,
            )
            created += int(result.created)
            if result.created or result.record.archived_at is not None:
                continue
            changes = {
                key: value for key, value in values.items()
                if key != "slug" and getattr(result.record, key) != value
            }
            if not changes:
                continue
            revisions = context.repository.list_revisions(
                transaction, step_id=result.record.step_id, limit=1,
            )
            if not revisions or revisions[0].editor_id != _SEED_EDITOR:
                continue
            context.repository.update_with_revision(
                transaction,
                step_id=result.record.step_id,
                expected_updated_at=result.record.updated_at,
                changes=changes,
                editor_id=_SEED_EDITOR,
                updated_at=max(observed_at, result.record.updated_at + timedelta(microseconds=1)),
            )
            refreshed += 1
    logger.info("Tutorial seed reconciled (%s created, %s refreshed)", created, refreshed)
    return created


__all__ = ("DEFAULT_TUTORIAL_STEPS", "seed_tutorial_steps")
