"""Authorizes owner-scoped projects and atomic chat creation through the application Plane.
Authenticated cursors and negotiated client policy are shared by REST and WebSocket entry points.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass

from astralplane import ProjectChatCursor, ProjectCursor
from astralplane.repositories import (
    RepositoryConflictError,
    RepositoryNotFoundError,
    RepositoryValidationError,
)

from orchestrator.external_identity_links import (
    decode_signed_payload,
    encode_signed_payload,
)
from orchestrator.plane_repository_context import (
    PlaneRepositoryContext,
    repository_from,
)
from orchestrator.web_auth import _secret


_PROJECT_CURSOR_MAX_BYTES = 4096


@dataclass(frozen=True)
class ProjectDeletion:
    chats: tuple


def _cursor_scope(owner_id, scope):
    return hashlib.sha256(
        json.dumps([owner_id, scope], ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


class ProjectError(ValueError):
    def __init__(self, code, status=400):
        self.code = code
        self.status = status
        self.message = {
            "project_selection_required": "Select or create a project before starting this chat.",
            "project_not_found": "Project is unavailable. Select an existing project.",
            "project_conflict": "Project changed. Refresh and try again.",
            "chat_project_conflict": "This chat already belongs to another project or has no project.",
            "invalid_project_request": "Check the project name, identifiers and revision.",
            "invalid_project_cursor": "Refresh the project list and try again.",
        }.get(code, "Project request could not be completed.")
        super().__init__(code)


def canonical_uuid(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise ProjectError("invalid_project_request") from None
    return value


def project_workspaces_negotiated(profile, device, capabilities):
    return (
        getattr(getattr(profile, "device_type", None), "value", None) == "macos"
        and getattr(profile, "console_contract", None) == "console/v2"
        and getattr(profile, "project_workspaces_v1", False) is True
        and isinstance(device, dict)
        and device.get("project_workspaces_v1") is True
        and "project_workspaces_v1" in (capabilities or ())
    )


def socket_requires_project(orchestrator, websocket):
    return (getattr(orchestrator, "ui_sessions", {}).get(websocket) or {}).get(
        "_project_workspaces_v1"
    ) is True


def request_requires_project(orchestrator, request, owner_id):
    device = request.headers.get("X-Astral-Device-Id")
    generation = request.headers.get("X-Astral-Connection-Generation")
    if not device or not generation:
        return False
    for socket, session in getattr(orchestrator, "ui_sessions", {}).items():
        context = getattr(orchestrator, "_connection_contexts", {}).get(id(socket))
        if (
            session.get("sub") == owner_id
            and session.get("_project_device_id") == device
            and session.get("_project_connection_generation") == generation
            and context is not None
            and not context.closing
            and str(context.connection_generation) == generation
        ):
            return socket_requires_project(orchestrator, socket)
    return False


class ProjectService:
    def __init__(self, history):
        self.history = history
        repository, runtime = repository_from(
            "projects",
            plane_runtime=history.plane_runtime,
            repositories=history.plane_repositories,
        )
        self.context = PlaneRepositoryContext(
            repository=repository, plane_runtime=runtime
        )
        self.repository = repository
        self.conversations = history._history.repository.conversations

    def _call(self, method, **kwargs):
        try:
            return self.context.call(method, **kwargs)
        except RepositoryNotFoundError:
            raise ProjectError("project_not_found", 404) from None
        except RepositoryConflictError:
            raise ProjectError("project_conflict", 409) from None
        except RepositoryValidationError:
            raise ProjectError("invalid_project_request") from None

    @staticmethod
    def _public(record):
        value = asdict(record)
        value.pop("owner_id")
        return value

    def create(self, owner_id, project_id, name, position=0):
        return self._public(
            self._call(
                self.repository.create,
                owner_id=owner_id,
                project_id=project_id,
                name=name,
                position=position,
                created_at=int(time.time() * 1000),
            )
        )

    def get(self, owner_id, project_id):
        record = self._call(
            self.repository.get, owner_id=owner_id, project_id=project_id
        )
        if record is None:
            raise ProjectError("project_not_found", 404)
        return self._public(record)

    def rename(self, owner_id, project_id, name, revision):
        return self._public(
            self._call(
                self.repository.rename,
                owner_id=owner_id,
                project_id=project_id,
                name=name,
                expected_revision=revision,
                updated_at=int(time.time() * 1000),
            )
        )

    def delete(self, owner_id, project_id, revision):
        if type(revision) is not int or not 1 <= revision < 2**53 - 1:
            raise ProjectError("invalid_project_request")

        def remove(transaction):
            project = self.repository.get(
                transaction, owner_id=owner_id, project_id=project_id, for_update=True
            )
            if project is None:
                raise RepositoryNotFoundError("project not found")
            mutations = []
            cursor = None
            while True:
                page = self.repository.list_chats(
                    transaction, owner_id=owner_id, project_id=project_id,
                    cursor=cursor, limit=200,
                )
                for chat in page.records:
                    mutations.append(self.history.delete_chat(
                        chat.chat_id, user_id=owner_id, transaction=transaction,
                    ))
                cursor = page.next_cursor
                if cursor is None:
                    break
            self.repository.delete(
                transaction, owner_id=owner_id, project_id=project_id,
                expected_revision=project.revision,
            )
            return ProjectDeletion(tuple(mutations))

        return self._call(remove)

    @staticmethod
    def _cursor(cursor, owner_id, scope):
        if cursor is None:
            return None
        try:
            if (
                not isinstance(cursor, str)
                or not cursor.isascii()
                or len(cursor) > _PROJECT_CURSOR_MAX_BYTES
            ):
                raise ValueError
            payload = decode_signed_payload(
                cursor, _secret(), expected_type="project_page"
            )
            if payload.get("scope") != _cursor_scope(owner_id, scope):
                raise ValueError
            values = payload["cursor"]
            if scope == "projects":
                return ProjectCursor(owner_id=owner_id, **values)
            if not isinstance(values, dict) or set(values) != {"chat_id_utf8"}:
                raise ValueError
            raw = base64.b64decode(
                values["chat_id_utf8"], altchars=b"-_", validate=True
            )
            if len(raw) > 2048:
                raise ValueError
            return ProjectChatCursor(
                owner_id=owner_id,
                project_id=scope.removeprefix("chats:"),
                cutoff=None,
                chat_id=raw.decode("utf-8"),
            )
        except (ValueError, TypeError, KeyError):
            raise ProjectError("invalid_project_cursor") from None

    @staticmethod
    def _next(cursor, owner_id, scope):
        if cursor is None:
            return None
        now = int(time.time())
        ordering = (
            {"position": cursor.position, "project_id": cursor.project_id}
            if scope == "projects"
            else {
                "chat_id_utf8": base64.urlsafe_b64encode(
                    cursor.chat_id.encode("utf-8")
                ).decode("ascii")
            }
        )
        token = encode_signed_payload(
            {
                "v": 1,
                "type": "project_page",
                "iat": now,
                "exp": now + 300,
                "scope": _cursor_scope(owner_id, scope),
                "cursor": ordering,
            },
            _secret(),
        )
        if len(token) > _PROJECT_CURSOR_MAX_BYTES:
            raise ProjectError("invalid_project_cursor")
        return token

    def list(self, owner_id, *, cursor=None, limit=50):
        page = self._call(
            self.repository.list,
            owner_id=owner_id,
            limit=limit,
            cursor=self._cursor(cursor, owner_id, "projects"),
        )
        return {
            "projects": [self._public(row) for row in page.records],
            "next_cursor": self._next(page.next_cursor, owner_id, "projects"),
        }

    def list_chats(self, owner_id, project_id, *, cursor=None, limit=50):
        scope = "chats:" + canonical_uuid(project_id)
        page = self._call(
            self.repository.list_chats,
            owner_id=owner_id,
            project_id=project_id,
            limit=limit,
            cursor=self._cursor(cursor, owner_id, scope),
        )
        chats = []
        for row in page.records:
            summary = self.history.get_chat_summary(row.chat_id, owner_id)
            if summary is not None:
                chats.append({**summary, "project_id": project_id})
        return {
            "project_id": project_id,
            "chats": chats,
            "next_cursor": self._next(page.next_cursor, owner_id, scope),
        }

    def project_for_chat(self, owner_id, chat_id):
        project = self._call(
            self.repository.project_for_chat, owner_id=owner_id, chat_id=chat_id
        )
        return project.project_id if project else None

    def require_chat(self, owner_id, chat_id):
        project_id = self.project_for_chat(owner_id, chat_id)
        if project_id is None:
            raise ProjectError("project_selection_required", 409)
        return project_id

    def create_chat(self, owner_id, project_id, chat_id, agent_id=None):
        if project_id is None:
            raise ProjectError("project_selection_required", 409)
        canonical_uuid(chat_id)
        try:
            with self.context.transaction() as transaction:
                project = self.repository.get(
                    transaction,
                    owner_id=owner_id,
                    project_id=project_id,
                    for_update=True,
                )
                if project is None:
                    raise ProjectError("project_not_found", 404)
                chat = self.conversations.get(
                    transaction,
                    owner_id=owner_id,
                    conversation_id=chat_id,
                    for_update=True,
                )
                if chat is not None:
                    membership = self.repository.project_for_chat(
                        transaction, owner_id=owner_id, chat_id=chat_id
                    )
                    if (
                        membership is None
                        or membership.project_id != project_id
                        or chat.agent_id != agent_id
                    ):
                        raise ProjectError("chat_project_conflict", 409)
                    return chat_id
                self.conversations.create(
                    transaction,
                    owner_id=owner_id,
                    conversation_id=chat_id,
                    title="New Chat",
                    agent_id=agent_id,
                    created_at=int(time.time() * 1000),
                )
                self.repository.attach_chat(
                    transaction,
                    owner_id=owner_id,
                    project_id=project_id,
                    chat_id=chat_id,
                )
                return chat_id
        except RepositoryConflictError:
            raise ProjectError("chat_project_conflict", 409) from None
        except RepositoryValidationError:
            raise ProjectError("invalid_project_request") from None


async def create_socket_chat(orchestrator, websocket, owner_id, payload, chat_id=None):
    import asyncio

    project_id = payload.get("project_id")
    if (
        project_id is not None
        or (chat_id is None and payload.get("chat_id") is not None)
        or socket_requires_project(orchestrator, websocket)
    ):
        service = ProjectService(orchestrator.history)
        chat_id = await asyncio.to_thread(
            service.create_chat,
            owner_id,
            project_id,
            chat_id or payload.get("chat_id"),
            payload.get("agent_id"),
        )
        await orchestrator._broadcast_user_history(
            user_id=owner_id, projects_changed=True
        )
        return chat_id, {"project_id": project_id}
    if chat_id:
        await asyncio.to_thread(
            orchestrator.history.create_chat, chat_id, user_id=owner_id
        )
    else:
        chat_id = await asyncio.to_thread(
            orchestrator.history.create_chat, user_id=owner_id
        )
    return chat_id, {}


async def check_socket_chat(orchestrator, websocket, owner_id, chat_id):
    import asyncio

    if socket_requires_project(orchestrator, websocket):
        return await asyncio.to_thread(
            ProjectService(orchestrator.history).require_chat, owner_id, chat_id
        )
    return None


async def send_project_error(orchestrator, websocket, error, frame):
    import json

    correlation = {
        key: frame[key]
        for key in ("connection_generation", "request_generation", "submission_id")
        if key in frame
    }
    resume = frame.get("resume")
    if isinstance(resume, dict):
        correlation.update(
            chat_id=resume["active_chat_id"],
            request_generation=resume["request_generation"],
        )
    await orchestrator._safe_send(
        websocket,
        json.dumps(
            {
                "type": "error",
                "code": error.code,
                "message": error.message,
                **correlation,
            }
        ),
    )
