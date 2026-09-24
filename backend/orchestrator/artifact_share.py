"""Store layer for revocable snapshot share links: mints and SHA-256-hashes tokens,
screens content via personalization.phi_gate.py at mint, and backs api.py's /share
routes behind FF_ARTIFACT_SHARING.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional

from audit.hooks import record_share_event
from orchestrator.plane_repository_context import (
    PlaneRepositoryContext,
    repository_from,
)
from personalization.phi_gate import get_phi_gate
from shared.feature_flags import flags

logger = logging.getLogger("orchestrator.artifact_share")

VALID_SCOPES = ("component", "canvas")

_SHARE_METADATA = frozenset({
    "component_id", "id", "css", "style", "provenance", "source_agent", "source_tool",
    "agent_id", "tool_id", "versions", "render_revision", "schema_version",
})


def _record_labels(labels):
    clinical = any(re.fullmatch(
        r"diagnosis|diagnoses|condition|treatment|medication|mrn|dob|date of birth|patient(?: name| id)?",
        str(label).strip(), re.IGNORECASE,
    ) for label in labels)
    return ["Patient name" if clinical and str(label).strip().lower() in {"name", "full name"}
            else str(label) for label in labels]


class _ShareMarkup(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.attributes = []
        self.in_style = False
        self.tables = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "iframe", "object", "embed"}:
            raise ValueError("share_screen_unsupported_markup")
        if tag == "style":
            self.in_style = True
        if tag in {"div", "p", "li", "tr", "section", "article", "br", "h1", "h2", "h3"}:
            self.text.append("\n")
        if tag in {"td", "th"}:
            self.text.append(" ")
        if tag == "table":
            self.tables.append({"headers": [], "row": [], "cell": None})
        elif self.tables:
            if tag == "tr":
                self.tables[-1]["row"] = []
            elif tag in {"td", "th"}:
                self.tables[-1]["cell"] = [tag, []]
        for key, value in attrs:
            if not value:
                continue
            if key == "style":
                self.attributes.extend(re.findall(r"url\s*\(([^)]+)\)", value, re.IGNORECASE))
            elif key not in {"class", "id", "width", "height", "role", "colspan", "rowspan",
                             "data-component-id", "data-astral-export", "data-astral-share"}:
                self.attributes.append(value)

    def handle_endtag(self, tag):
        if tag == "style":
            self.in_style = False
        if tag in {"div", "p", "li", "tr", "section", "article", "h1", "h2", "h3"}:
            self.text.append("\n")
        if self.tables:
            table = self.tables[-1]
            if tag in {"td", "th"} and table["cell"] is not None:
                kind, values = table["cell"]
                table["row"].append((kind, "".join(values)))
                table["cell"] = None
            elif tag == "tr" and table["row"]:
                if all(kind == "th" for kind, _ in table["row"]):
                    table["headers"] = [value for _, value in table["row"]]
                else:
                    labels = _record_labels(table["headers"])
                    self.text.extend(label + ": " + value + "\n"
                                     for label, (_, value) in zip(labels, table["row"]))
            elif tag == "table":
                self.tables.pop()

    def handle_data(self, data):
        if self.in_style:
            self.attributes.extend(re.findall(r"url\s*\(([^)]+)\)", data, re.IGNORECASE))
        else:
            self.text.append(data)
            if self.tables and self.tables[-1]["cell"] is not None:
                self.tables[-1]["cell"][1].append(data)


def _share_screen_text(snapshot_json: Any, snapshot_html: str) -> str:
    parts = []
    budget = [50_000]

    def markup(value):
        parser = _ShareMarkup()
        parser.feed(value)
        parser.close()
        if parser.in_style:
            raise ValueError("share_screen_invalid_markup")
        return "".join(parser.text) + "\n" + "\n".join(parser.attributes)

    def visit(value, label="", depth=0):
        budget[0] -= 1
        if depth > 32 or budget[0] < 0:
            raise ValueError("share_screen_limit")
        if isinstance(value, dict):
            component = isinstance(value.get("type"), str)
            labels = _record_labels(value.keys())
            headers = value.get("headers") or value.get("columns")
            rows = value.get("rows")
            if value.get("type") == "table" and isinstance(headers, (list, tuple)) and isinstance(rows, (list, tuple)):
                row_labels = _record_labels(headers)
                for row in rows:
                    if isinstance(row, (list, tuple)):
                        for header, cell in zip(row_labels, row):
                            visit(cell, header, depth + 1)
            for (key, child), child_label in zip(value.items(), labels):
                if not isinstance(key, str):
                    raise ValueError("share_screen_invalid")
                if component and (key.startswith("_") or key in _SHARE_METADATA):
                    continue
                visit(child, child_label, depth + 1)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child, label, depth + 1)
        elif isinstance(value, str):
            parts.append(label + ": " + (markup(value) if "<" in value else value))
        elif value is not None and type(value) in (int, float, bool):
            parts.append(label + ": " + str(value))
        elif value is not None:
            raise ValueError("share_screen_invalid")

    if not isinstance(snapshot_html, str) or len(snapshot_html.encode("utf-8")) > 8 * 1024 * 1024:
        raise ValueError("share_screen_limit")
    visit(snapshot_json)
    parts.append(markup(snapshot_html))
    return "\n".join(parts)


class ShareError(Exception):
    pass


class SharingDisabledError(ShareError):
    pass


class SharePHIRefusedError(ShareError):
    pass


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class ShareGrantStore:
    def __init__(
        self,
        db=None,
        *,
        plane_runtime=None,
        plane_repositories=None,
        plane_repository=None,
    ):
        if db is None and plane_runtime is None:
            raise ValueError("ShareGrantStore requires the application Plane runtime")
        repository, runtime = repository_from(
            "share_grants",
            plane_runtime=plane_runtime,
            repositories=plane_repositories,
            legacy_database=db,
        )
        self._grants = PlaneRepositoryContext(
            repository=plane_repository or repository,
            plane_runtime=runtime,
            legacy_database=db,
        )

    async def mint(
        self, *, user_id: str, chat_id: str, scope: str,
        snapshot_html: str, snapshot_json: Any,
        component_id: Optional[str] = None, expires_at=None,
    ) -> Dict[str, Any]:
        if not flags.is_enabled("artifact_sharing"):
            logger.info("share.refused_disabled user=%s chat=%s scope=%s",
                        user_id, chat_id, scope)
            raise SharingDisabledError("artifact sharing is disabled (FF_ARTIFACT_SHARING)")
        if scope not in VALID_SCOPES:
            raise ValueError(f"invalid share scope: {scope!r}")
        if scope == "component" and not component_id:
            raise ValueError("component-scoped share requires component_id")
        if not user_id or not chat_id or not snapshot_html or snapshot_json is None:
            raise ValueError("mint requires user_id, chat_id and a non-empty snapshot")

        try:
            gate_text = _share_screen_text(snapshot_json, snapshot_html)
            phi_hit = await asyncio.to_thread(get_phi_gate().contains_phi_for_sharing, gate_text)
        except Exception:
            phi_hit = True
        if phi_hit:
            logger.warning("share.refused_phi user=%s chat=%s scope=%s component=%s",
                           user_id, chat_id, scope, component_id)
            detail: Dict[str, Any] = {"scope": scope}
            if component_id:
                detail["component_id"] = component_id
            await record_share_event(
                user_id=user_id, action="refused_phi", chat_id=chat_id,
                outcome="failure",
                description="Share mint refused: snapshot flagged as PHI (fail-closed)",
                detail=detail,
            )
            raise SharePHIRefusedError("snapshot content flagged as PHI")

        token = secrets.token_urlsafe(32)
        record = await self._grants.call_async(
            self._grants.repository.create_grant,
            token_sha256=hash_token(token),
            owner_id=user_id,
            chat_id=chat_id,
            scope=scope,
            component_id=component_id,
            snapshot_html=snapshot_html,
            snapshot_json=snapshot_json,
            expires_at=expires_at,
        )
        detail = {"scope": scope}
        if component_id:
            detail["component_id"] = component_id
        await record_share_event(
            user_id=user_id, action="minted", chat_id=chat_id,
            share_id=record.share_id,
            description=f"Share link minted ({scope})", detail=detail,
        )
        logger.info("share.minted share_id=%s user=%s chat=%s scope=%s component=%s",
                    record.share_id, user_id, chat_id, scope, component_id)
        return {
            "id": record.share_id,
            "token": token,
            "share_url": f"/share/{token}",
            "created_at": record.created_at,
            "expires_at": record.expires_at,
        }

    async def list_grants(self, user_id: str) -> List[Dict[str, Any]]:
        records = await self._grants.call_async(
            self._grants.repository.list_grants,
            owner_id=user_id,
            limit=1000,
        )
        return [
            {
                "id": record.share_id,
                "chat_id": record.chat_id,
                "scope": record.scope,
                "component_id": record.component_id,
                "created_at": record.created_at,
                "expires_at": record.expires_at,
                "revoked_at": record.revoked_at,
                "open_count": record.open_count,
            }
            for record in records
        ]

    async def revoke(self, user_id: str, share_id: int) -> bool:
        state = await asyncio.to_thread(self._revoke_sync, user_id, share_id)
        if state == "revoked":
            await record_share_event(
                user_id=user_id, action="revoked", share_id=share_id,
                description="Share link revoked",
            )
        return state != "missing"

    def _revoke_sync(self, user_id: str, share_id: int) -> str:
        state = self._grants.call(
            self._grants.repository.revoke_grant,
            owner_id=user_id,
            share_id=share_id,
            revoked_at=datetime.now(UTC),
        )
        return state.value

    async def resolve(self, token: str) -> Optional[Dict[str, Any]]:
        if not token:
            return None
        record = await self._grants.call_async(
            self._grants.repository.resolve_active_by_digest,
            token_sha256=hash_token(token),
            as_of=datetime.now(UTC),
        )
        if record is None:
            return None
        return {
            "id": record.share_id,
            "user_id": record.owner_id,
            "chat_id": record.chat_id,
            "scope": record.scope,
            "component_id": record.component_id,
            "snapshot_html": record.snapshot_html,
            "snapshot_json": record.snapshot_json,
            "created_at": record.created_at,
            "expires_at": record.expires_at,
            "open_count": record.open_count,
            "_token_sha256": record.token_sha256,
        }

    async def record_open(self, grant: Dict[str, Any]) -> bool:
        record = await self._grants.call_async(
            self._grants.repository.record_open,
            share_id=grant["id"],
            token_sha256=grant["_token_sha256"],
            as_of=datetime.now(UTC),
        )
        if record is None:
            return False
        await record_share_event(
            user_id=grant["user_id"], action="opened", share_id=grant["id"],
            chat_id=grant.get("chat_id"), principal=f"share:{grant['id']}",
            description="Shared snapshot opened (public)",
        )
        return True


_STORE: Optional[ShareGrantStore] = None


def get_share_store() -> ShareGrantStore:
    if _STORE is None:
        raise RuntimeError("share persistence has not been bound to AstralPlane")
    return _STORE


def set_share_store(store: Optional[ShareGrantStore]) -> None:
    global _STORE
    _STORE = store
