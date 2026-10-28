"""Host adapter for the shared Atlas browsing surface: owner-scoped page lists, page and
historical revision reads, link/backlink navigation, and authenticated source opening over
personalization/atlas_service.py. Web rendering uses webrender chrome helpers and native
clients receive approved SDUI primitives; every navigation rides the existing chrome_open
action vocabulary.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Optional
from uuid import UUID

from personalization.atlas_pages import (
    GLOBAL_PROJECT,
    MAX_CURSOR_BYTES,
    MAX_REVISION,
    MAX_SLUG_CHARACTERS,
    AtlasPageError,
    parse_envelope,
)
from personalization.atlas_service import (
    AtlasConflict,
    AtlasNotFound,
    AtlasRefused,
    AtlasUnavailable,
    AtlasPageService,
)
from webrender.chrome import esc, notice_block
from webrender.chrome.surfaces import _sdui

logger = logging.getLogger("Orchestrator.Chrome.Atlas")

TITLE = "Atlas"
SURFACE_KEY = "atlas"
HANDLERS: dict = {}

_VIEWS = ("list", "page", "history", "source")
_LIST_LIMIT = 20
_FACET_SCAN_LIMIT = 200
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.ASCII
)
_SOURCE_REF = re.compile(
    r"^(memory|note):([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{12})@([0-9]{1,16})$",
    re.ASCII,
)
_CURSOR_CHARS = re.compile(r"[A-Za-z0-9_-]+")

_BTN_PRIMARY = (
    "px-3 py-1.5 rounded-lg text-xs font-medium bg-astral-primary/20 "
    "text-astral-primary border border-astral-primary/30 hover:bg-astral-primary/30"
)
_BTN_GHOST = (
    "px-3 py-1.5 rounded-lg text-xs font-medium bg-white/5 text-astral-text "
    "border border-white/10 hover:bg-white/10"
)
_CARD_CLS = "bg-white/5 border border-white/10 rounded-lg p-4"
_META_CLS = "text-xs text-astral-muted"

_READ_ERRORS = (AtlasPageError, AtlasNotFound, AtlasConflict, AtlasRefused, AtlasUnavailable)


@dataclass(frozen=True, slots=True)
class _Nav:
    view: str = "list"
    project: Optional[str] = None
    page_ref: str = ""
    revision: Optional[int] = None
    cursor: str = ""
    source: str = ""
    source_kind: str = ""
    source_id: str = ""
    source_revision: Optional[int] = None
    error: str = ""


@dataclass(frozen=True, slots=True)
class _Failure:
    message: str


def _scope(value) -> Optional[str]:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError
    scope = value.strip()
    if not scope or "\x00" in scope or len(scope.encode("utf-8")) > 128:
        raise ValueError
    return None if scope == GLOBAL_PROJECT else scope


def _page_ref(value) -> str:
    if not isinstance(value, str):
        raise ValueError
    ref = value.strip()
    if not ref or "\x00" in ref or len(ref) > MAX_SLUG_CHARACTERS:
        raise ValueError
    if _UUID.fullmatch(ref):
        return ref.lower()
    return ref


def _revision(value) -> Optional[int]:
    if value is None or value == "":
        return None
    number = int(value)
    if not 1 <= number <= MAX_REVISION:
        raise ValueError
    return number


def _cursor(value) -> str:
    if value is None or value == "":
        return ""
    if (not isinstance(value, str) or len(value) > MAX_CURSOR_BYTES
            or _CURSOR_CHARS.fullmatch(value) is None):
        raise ValueError
    return value


def _source_ref(value) -> tuple[str, str, Optional[int]]:
    if not isinstance(value, str):
        raise ValueError
    match = _SOURCE_REF.fullmatch(value.strip())
    if match is None:
        raise ValueError
    revision = int(match.group(3))
    if revision > MAX_REVISION:
        raise ValueError
    return match.group(1), str(UUID(match.group(2))), revision


def _nav(params) -> _Nav:
    params = params if isinstance(params, dict) else {}
    error = ""
    view = params.get("view") or "list"
    if view not in _VIEWS:
        view, error = "list", "That Atlas view is unavailable."
    raw_ref = params.get("page_ref")
    raw_source = params.get("source")
    try:
        project = _scope(params.get("project"))
        page_ref = _page_ref(raw_ref) if raw_ref else ""
        revision = _revision(params.get("revision"))
        cursor = _cursor(params.get("cursor"))
        source = raw_source.strip() if isinstance(raw_source, str) else ""
        if raw_source:
            source_kind, source_id, source_revision = _source_ref(raw_source)
        else:
            source_kind, source_id, source_revision = "", "", None
    except (ValueError, TypeError, UnicodeError):
        return _Nav(error="That Atlas navigation link is invalid.")
    if view in {"page", "history"} and not page_ref:
        return _Nav(project=project, error="Choose an Atlas page to open.")
    if view == "source" and not source_kind:
        return _Nav(project=project, error="Choose an Atlas source to open.")
    return _Nav(view=view, project=project, page_ref=page_ref, revision=revision,
                cursor=cursor, source=source, source_kind=source_kind,
                source_id=source_id, source_revision=source_revision, error=error)


def _service(orch) -> AtlasPageService:
    plane = getattr(getattr(orch, "runtime_composition", None), "plane", None)
    fernet = getattr(getattr(orch, "credential_manager", None), "_fernet", None)
    return AtlasPageService(
        plane_runtime=getattr(plane, "runtime", None),
        plane_repositories=getattr(plane, "repositories", None),
        audit_repo=getattr(orch, "audit_repo", None),
        fernet=fernet,
    )


def _claims(orch, user_id: str) -> dict:
    from orchestrator.chrome_events import current_surface_socket

    websocket = current_surface_socket.get()
    claims = (getattr(orch, "ui_sessions", None) or {}).get(websocket)
    if isinstance(claims, dict) and claims.get("sub") == user_id:
        return claims
    return {"sub": user_id}


async def _audit_view(orch, user_id: str, action: str, description: str, outputs: dict) -> None:
    from audit.hooks import record_generic

    await record_generic(
        claims=_claims(orch, user_id),
        event_class="memory",
        action_type=action,
        description=description,
        outputs_meta=outputs,
    )


def _read_failure(exc: Exception) -> _Failure:
    if isinstance(exc, AtlasNotFound):
        return _Failure("That Atlas page or revision no longer exists.")
    if isinstance(exc, AtlasPageError):
        return _Failure(f"That Atlas request was rejected ({exc.code}).")
    if isinstance(exc, (AtlasConflict, AtlasRefused)):
        return _Failure("That Atlas request was refused.")
    logger.debug("atlas surface read unavailable", exc_info=exc)
    return _Failure("The Atlas subsystem is temporarily unavailable. Try again.")


async def _safe_read(loader):
    try:
        return await asyncio.to_thread(loader), None
    except _READ_ERRORS as exc:
        return None, _read_failure(exc)


def _open_params(**params) -> dict:
    cleaned = {key: value for key, value in params.items() if value not in (None, "")}
    return {"surface": SURFACE_KEY, "params": cleaned}


def _payload_attr(data) -> str:
    return esc(json.dumps(data))


def _open_button(label: str, surface: str, params: dict, *, cls: str = _BTN_GHOST) -> str:
    payload = _payload_attr({"surface": surface, "params": params})
    return (
        f'<button type="button" class="{cls}" data-ui-action="chrome_open" '
        f"data-ui-payload='{payload}'>{esc(label)}</button>"
    )


def _atlas_button(label: str, params: dict, *, cls: str = _BTN_GHOST) -> str:
    cleaned = {key: value for key, value in params.items() if value not in (None, "")}
    return _open_button(label, SURFACE_KEY, cleaned, cls=cls)


def _sdui_open(label: str, params: dict, *, variant: str = "secondary"):
    return _sdui.button(label, "chrome_open", _open_params(**params), variant=variant)


def _fmt_ts(ms) -> str:
    if not ms:
        return "—"
    try:
        moment = datetime.fromtimestamp(int(ms) / 1000.0, tz=UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return "—"
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def _paragraphs(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"\n\s*\n", text or "") if part.strip()]


def _body_html(text: str) -> str:
    paragraphs = _paragraphs(text)
    if not paragraphs:
        return f'<p class="{_META_CLS}">This page has no content yet.</p>'
    return "".join(
        f'<p class="text-sm text-astral-text leading-relaxed whitespace-pre-wrap">{esc(part)}</p>'
        for part in paragraphs
    )


def _body_components(text: str) -> list:
    paragraphs = _paragraphs(text)
    if not paragraphs:
        return []
    return [_sdui.text(part, "body") for part in paragraphs]


def _scope_label(project: Optional[str]) -> str:
    return "the global project" if project is None else f"project “{project}”"


def _project_param(project: Optional[str]) -> str:
    return GLOBAL_PROJECT if project is None else project


def _facets(service: AtlasPageService, user_id: str) -> list[Optional[str]]:
    scan = service.scan_pages(owner_id=user_id, all_projects=True, limit=_FACET_SCAN_LIMIT)
    seen: list[Optional[str]] = []
    for page in scan.pages:
        if page.project_id not in seen:
            seen.append(page.project_id)
    return seen


def _failure_notice(failure: Optional[_Failure]) -> str:
    return notice_block("error", failure.message) if failure else ""


async def _list_html(orch, user_id: str, nav: _Nav, failure: Optional[_Failure] = None) -> str:
    batch = None
    read_failure = failure
    if failure is None:
        batch, read_failure = await _safe_read(
            lambda: _service(orch).list_page_batch(
                owner_id=user_id, project_id=nav.project, limit=_LIST_LIMIT,
                cursor=nav.cursor or None,
            )
        )
    if batch is None:
        return (
            '<div class="space-y-4">'
            f'{_failure_notice(read_failure)}'
            f'<p class="{_META_CLS}">Browsing Atlas pages in {_scope_label(nav.project)}.'
            "</p></div>"
        )
    await _audit_view(
        orch, user_id, "atlas_pages_view", "Viewed Atlas page list",
        {"project": nav.project or GLOBAL_PROJECT, "count": len(batch.pages)},
    )
    facets: list[Optional[str]] = []
    if not nav.cursor:
        try:
            facets = await asyncio.to_thread(_facets, _service(orch), user_id)
        except _READ_ERRORS:
            facets = []
    extra_scopes = [scope for scope in facets if scope != nav.project]
    parts = [_failure_notice(failure)] if failure else []
    parts.append(
        f'<p class="{_META_CLS}">Browsing Atlas pages in {_scope_label(nav.project)}. '
        "Pages are created and edited through the Atlas API and agent promotions.</p>"
    )
    if extra_scopes:
        chips = "".join(
            _atlas_button("Global project" if scope is None else str(scope),
                          {"view": "list", "project": _project_param(scope)})
            for scope in extra_scopes
        )
        parts.append(
            f'<div class="flex flex-wrap gap-2 items-center"><span class="{_META_CLS}">'
            f"Other page scopes:</span>{chips}</div>"
        )
    if not batch.pages:
        parts.append(
            f'<div class="{_CARD_CLS} text-sm text-astral-muted">No Atlas pages here yet.</div>'
        )
    else:
        rows = [
            f'<div class="{_CARD_CLS} space-y-2">'
            f'<div class="flex items-center justify-between gap-2">'
            f'<span class="text-sm font-medium text-astral-text">{esc(page.title)}</span>'
            f'<span class="px-2 py-0.5 rounded-full text-[10px] font-medium border '
            f'border-white/10 bg-white/5 text-astral-muted">rev {int(page.revision)}</span></div>'
            f'<div class="{_META_CLS}">{esc(page.slug)} · updated {_fmt_ts(page.updated_at)}</div>'
            + '<div class="flex gap-2 justify-end">'
            + _atlas_button("Open page", {"view": "page", "project": _project_param(nav.project),
                                          "page_ref": page.page_id}, cls=_BTN_PRIMARY)
            + _atlas_button("History", {"view": "history",
                                        "project": _project_param(nav.project),
                                        "page_ref": page.page_id})
            + "</div></div>"
            for page in batch.pages
        ]
        parts.append(f'<div class="space-y-2">{"".join(rows)}</div>')
    if batch.next_cursor:
        parts.append(
            _atlas_button("Older pages", {"view": "list",
                                          "project": _project_param(nav.project),
                                          "cursor": batch.next_cursor})
        )
    return f'<div class="space-y-4">{"".join(parts)}</div>'


async def _page_html(orch, user_id: str, nav: _Nav) -> str:
    page, failure = await _safe_read(
        lambda: _service(orch).get_page(
            owner_id=user_id, project_id=nav.project, reference=nav.page_ref,
            revision=nav.revision,
        )
    )
    if page is None:
        return await _list_html(orch, user_id, _Nav(project=nav.project), failure)
    await _audit_view(
        orch, user_id, "atlas_page_view", "Viewed Atlas page",
        {"page_id": page.page_id, "revision": page.revision,
         "historical": nav.revision is not None},
    )
    links, _links_failure = await _safe_read(
        lambda: _service(orch).page_links(owner_id=user_id, project_id=nav.project,
                                          reference=page.page_id)
    )
    links = links or ()
    parts: list[str] = []
    if nav.revision is not None:
        parts.append(
            notice_block(
                "info",
                f"You are reading historical revision {nav.revision}; the page has moved "
                "on. Open the current version for the live content.",
            )
        )
        parts.append(_atlas_button("Open current version",
                                   {"view": "page", "project": _project_param(nav.project),
                                    "page_ref": page.page_id}, cls=_BTN_PRIMARY))
    head = [
        f'<div class="flex items-center justify-between gap-2">'
        f'<h3 class="text-lg font-semibold text-astral-text">{esc(page.title)}</h3>'
        f'<span class="px-2 py-0.5 rounded-full text-[10px] font-medium border '
        f'border-white/10 bg-white/5 text-astral-muted">revision {int(page.revision)}</span></div>',
        f'<div class="{_META_CLS}">{esc(page.slug)} · created {_fmt_ts(page.created_at)} · '
        f"updated {_fmt_ts(page.updated_at)}</div>",
    ]
    parts.append(
        f'<div class="{_CARD_CLS} space-y-3">{"".join(head)}{_body_html(page.body or "")}</div>'
    )
    parts.append(_sources_html(nav, page))
    parts.append(_links_html(nav, page, links))
    actions = [
        _atlas_button("All pages", {"view": "list", "project": _project_param(nav.project)}),
        _atlas_button("Revision history", {"view": "history",
                                           "project": _project_param(nav.project),
                                           "page_ref": page.page_id}),
    ]
    parts.append(f'<div class="flex flex-wrap gap-2">{"".join(actions)}</div>')
    return f'<div class="space-y-4">{"".join(parts)}</div>'


def _sources_html(nav: _Nav, page) -> str:
    rows: list[str] = []
    for source in page.sources or ():
        reference = _parse_source(source)
        if reference is None:
            rows.append(
                f'<li class="bg-white/5 border border-white/10 rounded-lg px-3 py-2 '
                f'text-sm text-astral-text break-words">{esc(source)}</li>'
            )
            continue
        kind, _identifier, revision = reference
        rows.append(
            f'<li class="bg-white/5 border border-white/10 rounded-lg px-3 py-2 '
            f'space-y-1"><div class="text-sm text-astral-text break-words">{esc(source)}</div>'
            f'<div class="flex items-center justify-between gap-2">'
            f'<span class="{_META_CLS}">{esc(kind)} · cited revision {revision}</span>'
            + _atlas_button("Open source",
                            {"view": "source", "project": _project_param(nav.project),
                             "page_ref": page.page_id, "source": source})
            + "</div></li>"
        )
    if not rows:
        return (
            f'<div class="{_CARD_CLS} space-y-1"><div class="text-[10px] uppercase '
            f'tracking-wider {_META_CLS}">Sources</div>'
            f'<p class="{_META_CLS}">This page records no sources.</p></div>'
        )
    return (
        f'<div class="{_CARD_CLS} space-y-1"><div class="text-[10px] uppercase '
        f'tracking-wider {_META_CLS}">Sources</div>'
        f'<ul class="space-y-2">{"".join(rows)}</ul></div>'
    )


def _links_html(nav: _Nav, page, links) -> str:
    if not links:
        return (
            f'<div class="{_CARD_CLS} space-y-1"><div class="text-[10px] uppercase '
            f'tracking-wider {_META_CLS}">Linked pages</div>'
            f'<p class="{_META_CLS}">No linked pages yet. Links and backlinks appear here.'
            "</p></div>"
        )
    rows = [
        f'<li class="bg-white/5 border border-white/10 rounded-lg px-3 py-2 flex '
        f'items-center justify-between gap-2">'
        f'<span class="text-sm text-astral-text">{esc(link.title or link.slug)}</span>'
        + _atlas_button("Open", {"view": "page", "project": _project_param(nav.project),
                                 "page_ref": link.page_id})
        + "</li>"
        for link in links
    ]
    return (
        f'<div class="{_CARD_CLS} space-y-1"><div class="text-[10px] uppercase '
        f'tracking-wider {_META_CLS}">Linked pages (links and backlinks)</div>'
        f'<ul class="space-y-2">{"".join(rows)}</ul></div>'
    )


async def _history_html(orch, user_id: str, nav: _Nav) -> str:
    history, failure = await _safe_read(
        lambda: _service(orch).page_history(owner_id=user_id, project_id=nav.project,
                                            reference=nav.page_ref)
    )
    if history is None:
        return await _list_html(orch, user_id, _Nav(project=nav.project), failure)
    await _audit_view(
        orch, user_id, "atlas_history_view", "Viewed Atlas revision history",
        {"page_id": history[0].page_id, "revisions": len(history)},
    )
    rows = []
    for index, entry in enumerate(history):
        badge = (
            '<span class="px-2 py-0.5 rounded-full text-[10px] font-medium border '
            'border-green-500/20 bg-green-500/10 text-green-400">current</span>'
            if index == 0
            else ""
        )
        rows.append(
            f'<li class="bg-white/5 border border-white/10 rounded-lg px-3 py-2 space-y-1">'
            f'<div class="flex items-center justify-between gap-2">'
            f'<span class="text-sm text-astral-text">Revision {int(entry.revision)} '
            f"— {esc(entry.title)}</span>{badge}</div>"
            f'<div class="{_META_CLS}">updated {_fmt_ts(entry.updated_at)}</div>'
            + _atlas_button("Open this revision",
                            {"view": "page", "project": _project_param(nav.project),
                             "page_ref": entry.page_id, "revision": entry.revision})
            + "</li>"
        )
    back = [
        _atlas_button("Back to page", {"view": "page", "project": _project_param(nav.project),
                                       "page_ref": history[0].page_id}, cls=_BTN_PRIMARY),
        _atlas_button("All pages", {"view": "list", "project": _project_param(nav.project)}),
    ]
    parts = [
        f'<h3 class="text-lg font-semibold text-astral-text">Revision history — '
        f'{esc(history[0].title)}</h3>',
        f'<ul class="space-y-2">{"".join(rows)}</ul>',
        f'<div class="flex flex-wrap gap-2">{"".join(back)}</div>',
    ]
    return f'<div class="space-y-4">{"".join(parts)}</div>'


_SOURCE_STATES = {
    "deleted": ("The source memory no longer exists. It was deleted after this page "
                "cited it.", "error"),
    "revoked": ("The source memory is no longer active — it was superseded or its "
                "validity window closed.", "error"),
    "stale": ("The source has changed since this page cited it, so its current content "
              "is not shown. Re-cite the source to update this page.", "error"),
    "invalid": ("That source reference points at an Atlas-internal record, not a "
                "citable memory.", "error"),
    "unbound": ("The memory subsystem is unavailable, so this source cannot be opened "
                "right now.", "error"),
}


def _memory_state(record, cited_revision: int) -> tuple[str, str]:
    if record is None:
        return "deleted", ""
    if record.get("superseded_at") is not None or record.get("superseded_by"):
        return "revoked", ""
    now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
    valid_to = record.get("valid_to")
    if isinstance(valid_to, int) and now_ms >= valid_to:
        return "revoked", ""
    valid_from = record.get("valid_from")
    if isinstance(valid_from, int) and now_ms < valid_from:
        return "revoked", ""
    if record.get("updated_at") != cited_revision:
        return "stale", ""
    if parse_envelope(record.get("value")) is not None:
        return "invalid", ""
    return "current", str(record.get("value") or "")


def _open_source_state(orch, user_id: str, nav: _Nav):
    if nav.source_kind != "memory":
        return "note", "", ""
    repo = getattr(getattr(orch, "personalization_service", None), "repo", None)
    if repo is None:
        return "unbound", "", ""
    try:
        record = repo.get_memory(user_id, nav.source_id)
    except Exception:
        logger.debug("atlas: source memory read failed", exc_info=True)
        return "unbound", "", ""
    state, content = _memory_state(record, nav.source_revision or 0)
    category = str(record.get("category") or "") if record else ""
    return state, content, category


async def _source_html(orch, user_id: str, nav: _Nav) -> str:
    state, content, category = await asyncio.to_thread(_open_source_state, orch, user_id, nav)
    await _audit_view(
        orch, user_id, "atlas_source_open", "Opened Atlas page source",
        {"source": nav.source, "kind": nav.source_kind, "state": state},
    )
    return _render_source_html(nav, state, content, category)


def _render_source_html(nav: _Nav, state: str, content: str, category: str) -> str:
    parts: list[str] = []
    if nav.page_ref:
        parts.append(
            _atlas_button("Back to page",
                          {"view": "page", "project": _project_param(nav.project),
                           "page_ref": nav.page_ref})
        )
    parts.append(
        f'<h3 class="text-lg font-semibold text-astral-text">Source</h3>'
        f'<div class="{_META_CLS} break-words">{esc(nav.source)}</div>'
    )
    if state == "current":
        meta = f"kind: {esc(nav.source_kind)}"
        if category:
            meta += f" · category: {esc(category)}"
        meta += f" · matches cited revision {nav.source_revision}"
        parts.append(f'<div class="{_CARD_CLS} space-y-3">'
                     f'<div class="{_META_CLS}">{meta}</div>{_body_html(content)}</div>')
    elif state == "note":
        parts.append(
            notice_block(
                "info",
                "This source is an encrypted private note. Note content opens in the "
                "Private notes surface, never inside Atlas.",
            )
        )
        parts.append(
            f'<div class="flex gap-2">'
            f'{_open_button("Open Private notes", "guidance", {"mode": "list"}, cls=_BTN_PRIMARY)}'
            "</div>"
        )
    elif state in _SOURCE_STATES:
        message, _kind = _SOURCE_STATES[state]
        parts.append(notice_block("error", message))
    return f'<div class="space-y-4">{"".join(parts)}</div>'


def _parse_source(source: str):
    try:
        return _source_ref(source)
    except (ValueError, TypeError, UnicodeError):
        return None


async def render(orch, user_id, roles, params) -> str:
    nav = _nav(params)
    if nav.error:
        body = await _list_html(orch, user_id, _Nav(project=nav.project))
        return f'<div class="space-y-4">{notice_block("error", nav.error)}{body}</div>'
    if nav.view == "page":
        return await _page_html(orch, user_id, nav)
    if nav.view == "history":
        return await _history_html(orch, user_id, nav)
    if nav.view == "source":
        return await _source_html(orch, user_id, nav)
    return await _list_html(orch, user_id, nav)


async def components(orch, user_id, roles, params):
    nav = _nav(params)
    if nav.error:
        body = await _list_components(orch, user_id, _Nav(project=nav.project))
        return [_sdui.alert(nav.error, "error"), *body]
    if nav.view == "page":
        return await _page_components(orch, user_id, nav)
    if nav.view == "history":
        return await _history_components(orch, user_id, nav)
    if nav.view == "source":
        return await _source_components(orch, user_id, nav)
    return await _list_components(orch, user_id, nav)


async def _list_components(orch, user_id: str, nav: _Nav,
                           failure: Optional[_Failure] = None) -> list:
    batch = None
    read_failure = failure
    if failure is None:
        batch, read_failure = await _safe_read(
            lambda: _service(orch).list_page_batch(
                owner_id=user_id, project_id=nav.project, limit=_LIST_LIMIT,
                cursor=nav.cursor or None,
            )
        )
    if batch is None:
        return [_sdui.alert(read_failure.message, "error")]
    await _audit_view(
        orch, user_id, "atlas_pages_view", "Viewed Atlas page list",
        {"project": nav.project or GLOBAL_PROJECT, "count": len(batch.pages)},
    )
    out: list = []
    if failure:
        out.append(_sdui.alert(failure.message, "error"))
    out.append(_sdui.text(f"Scope: {_scope_label(nav.project)}", "caption"))
    for page in batch.pages:
        out.append(_sdui.card(page.title, [
            _sdui.key_value([
                {"label": "Slug", "value": page.slug},
                {"label": "Revision", "value": str(page.revision)},
                {"label": "Updated", "value": _fmt_ts(page.updated_at)},
            ]),
            _sdui.container([
                _sdui_open("Open page", {"view": "page",
                                         "project": _project_param(nav.project),
                                         "page_ref": page.page_id}, variant="primary"),
                _sdui_open("History", {"view": "history",
                                       "project": _project_param(nav.project),
                                       "page_ref": page.page_id}),
            ], direction="row"),
        ]))
    if batch.next_cursor:
        out.append(_sdui_open("Older pages", {"view": "list",
                                              "project": _project_param(nav.project),
                                              "cursor": batch.next_cursor}))
    return out


async def _page_components(orch, user_id: str, nav: _Nav) -> list:
    page, failure = await _safe_read(
        lambda: _service(orch).get_page(
            owner_id=user_id, project_id=nav.project, reference=nav.page_ref,
            revision=nav.revision,
        )
    )
    if page is None:
        return await _list_components(orch, user_id, _Nav(project=nav.project), failure)
    await _audit_view(
        orch, user_id, "atlas_page_view", "Viewed Atlas page",
        {"page_id": page.page_id, "revision": page.revision,
         "historical": nav.revision is not None},
    )
    links, _links_failure = await _safe_read(
        lambda: _service(orch).page_links(owner_id=user_id, project_id=nav.project,
                                          reference=page.page_id)
    )
    links = links or ()
    out: list = []
    if nav.revision is not None:
        out.append(_sdui.alert(f"You are reading historical revision {nav.revision}.", "info"))
        out.append(_sdui_open("Open current version",
                              {"view": "page", "project": _project_param(nav.project),
                               "page_ref": page.page_id}, variant="primary"))
    out.append(_sdui.card(page.title, [
        _sdui.key_value([
            {"label": "Slug", "value": page.slug},
            {"label": "Revision", "value": str(page.revision)},
            {"label": "Created", "value": _fmt_ts(page.created_at)},
            {"label": "Updated", "value": _fmt_ts(page.updated_at)},
        ]),
        *_body_components(page.body or ""),
    ]))
    out.append(_sdui.text("Sources", "h3"))
    if page.sources:
        for source in page.sources:
            reference = _parse_source(source)
            if reference is None:
                out.append(_sdui.card(source, []))
                continue
            out.append(_sdui.card(source, [
                _sdui.text(f"{reference[0]} · cited revision {reference[2]}", "caption"),
                _sdui_open("Open source", {"view": "source",
                                           "project": _project_param(nav.project),
                                           "page_ref": page.page_id, "source": source}),
            ]))
    out.append(_sdui.text("Linked pages (links and backlinks)", "h3"))
    if links:
        for link in links:
            out.append(_sdui.card(link.title or link.slug, [
                _sdui_open("Open", {"view": "page", "project": _project_param(nav.project),
                                    "page_ref": link.page_id}),
            ]))
    out.append(_sdui.container([
        _sdui_open("All pages", {"view": "list", "project": _project_param(nav.project)}),
        _sdui_open("Revision history", {"view": "history",
                                        "project": _project_param(nav.project),
                                        "page_ref": page.page_id}),
    ], direction="row"))
    return out


async def _history_components(orch, user_id: str, nav: _Nav) -> list:
    history, failure = await _safe_read(
        lambda: _service(orch).page_history(owner_id=user_id, project_id=nav.project,
                                            reference=nav.page_ref)
    )
    if history is None:
        return await _list_components(orch, user_id, _Nav(project=nav.project), failure)
    await _audit_view(
        orch, user_id, "atlas_history_view", "Viewed Atlas revision history",
        {"page_id": history[0].page_id, "revisions": len(history)},
    )
    out: list = [_sdui.text(f"Revision history — {history[0].title}", "h3")]
    for index, entry in enumerate(history):
        label = f"Revision {entry.revision} — {entry.title}"
        if index == 0:
            label += " (current)"
        out.append(_sdui.card(label, [
            _sdui.text(f"updated {_fmt_ts(entry.updated_at)}", "caption"),
            _sdui_open("Open this revision",
                       {"view": "page", "project": _project_param(nav.project),
                        "page_ref": entry.page_id, "revision": entry.revision}),
        ]))
    out.append(_sdui.container([
        _sdui_open("Back to page", {"view": "page", "project": _project_param(nav.project),
                                    "page_ref": history[0].page_id}, variant="primary"),
        _sdui_open("All pages", {"view": "list", "project": _project_param(nav.project)}),
    ], direction="row"))
    return out


async def _source_components(orch, user_id: str, nav: _Nav) -> list:
    state, content, category = await asyncio.to_thread(_open_source_state, orch, user_id, nav)
    await _audit_view(
        orch, user_id, "atlas_source_open", "Opened Atlas page source",
        {"source": nav.source, "kind": nav.source_kind, "state": state},
    )
    out: list = [_sdui.text("Source", "h3"), _sdui.text(nav.source, "caption")]
    if nav.page_ref:
        out.append(_sdui_open("Back to page",
                              {"view": "page", "project": _project_param(nav.project),
                               "page_ref": nav.page_ref}))
    if state == "current":
        meta = f"kind: {nav.source_kind}"
        if category:
            meta += f" · category: {category}"
        meta += f" · matches cited revision {nav.source_revision}"
        out.append(_sdui.card("Source content",
                              [*_body_components(content), _sdui.text(meta, "caption")]))
    elif state == "note":
        out.append(_sdui.alert(
            "This source is an encrypted private note. Note content opens in the "
            "Private notes surface, never inside Atlas.", "info"))
        out.append(_sdui.button("Open Private notes", "chrome_open",
                                {"surface": "guidance", "params": {"mode": "list"}},
                                variant="primary"))
    elif state in _SOURCE_STATES:
        message, _kind = _SOURCE_STATES[state]
        out.append(_sdui.alert(message, "error"))
    return out
