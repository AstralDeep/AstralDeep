"""Owner-scoped REST routes for personalization profile, memory and the skills catalog;
PHI-gates every free-text value before persistence and audits changes. Registered by
orchestrator/orchestrator.py, backed by phi_gate.py and schemas.py.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response

from pydantic import BaseModel

from orchestrator.auth import get_current_user_payload, require_user_id
from audit.hooks import record_generic

from .phi_gate import get_phi_gate
from .schemas import ProfileResponse, ProfileUpdateRequest

logger = logging.getLogger("Personalization.API")

personalization_router = APIRouter(prefix="/api/personalization", tags=["Personalization"])
skills_router = APIRouter(prefix="/api/skills", tags=["Skills"])
memory_router = APIRouter(prefix="/api/memory", tags=["Memory"])


def _service(request: Request):
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        root_app = getattr(request.app, "_root_app", None) or request.app
        orch = getattr(root_app.state, "orchestrator", None)
    svc = getattr(orch, "personalization_service", None) if orch else None
    if svc is None:
        raise HTTPException(status_code=503, detail="Personalization subsystem not initialized")
    return svc


def _profile_to_response(row: Dict[str, Any] | None) -> ProfileResponse:
    if not row:
        return ProfileResponse()
    return ProfileResponse(
        profession=row.get("profession"),
        goals=list(row.get("goals") or []),
        personality=dict(row.get("personality") or {}),
        dreaming_enabled=bool(row.get("dreaming_enabled", True)),
    )


def _phi_reject(field: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"error": "value_rejected", "field": field,
                 "reason": "looks like protected health information"},
    )


@personalization_router.get("/profile", response_model=ProfileResponse)
async def get_profile(request: Request, user_id: str = Depends(require_user_id)):
    svc = _service(request)
    return _profile_to_response(svc.repo.get_profile(user_id))


@personalization_router.put("/profile", response_model=ProfileResponse)
async def put_profile(
    body: ProfileUpdateRequest,
    request: Request,
    user_id: str = Depends(require_user_id),
    payload: dict = Depends(get_current_user_payload),
):
    svc = _service(request)
    gate = get_phi_gate()

    if body.profession and gate.contains_phi(body.profession):
        return _phi_reject("profession")
    if body.goals:
        for g in body.goals:
            if gate.contains_phi(g):
                return _phi_reject("goals")
    personality_dict = None
    if body.personality is not None:
        personality_dict = body.personality.model_dump(exclude_none=True)
        if personality_dict.get("notes") and gate.contains_phi(personality_dict["notes"]):
            return _phi_reject("personality.notes")

    updated = svc.repo.upsert_profile(
        user_id,
        profession=body.profession,
        goals=body.goals,
        personality=personality_dict,
        dreaming_enabled=body.dreaming_enabled,
    )

    changed_personality = personality_dict is not None
    await record_generic(
        claims=payload,
        event_class="personalization",
        action_type="personalization.personality_update" if changed_personality
        else "personalization.profile_update",
        description="Updated personalization profile" if not changed_personality
        else "Updated assistant personality",
        outputs_meta={"changed": [k for k, v in body.model_dump(exclude_none=True).items()]},
    )
    return _profile_to_response(updated)


@personalization_router.delete("/profile", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile(
    request: Request,
    user_id: str = Depends(require_user_id),
    payload: dict = Depends(get_current_user_payload),
):
    svc = _service(request)
    svc.repo.reset_profile(user_id)
    await record_generic(
        claims=payload,
        event_class="personalization",
        action_type="personalization.profile_update",
        description="Reset personalization profile to defaults",
        outputs_meta={"reset": True},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class MemoryUpdateRequest(BaseModel):
    value: str


@memory_router.get("")
async def list_memory(request: Request, user_id: str = Depends(require_user_id),
                      payload: dict = Depends(get_current_user_payload)):
    svc = _service(request)
    items = svc.repo.list_memory(user_id)
    await record_generic(claims=payload, event_class="memory", action_type="memory.view",
                         description="Viewed durable memory", outputs_meta={"count": len(items)})
    return {"items": items}


@memory_router.put("/{mem_id}")
async def update_memory(mem_id: str, body: MemoryUpdateRequest, request: Request,
                        user_id: str = Depends(require_user_id),
                        payload: dict = Depends(get_current_user_payload)):
    svc = _service(request)
    if get_phi_gate().contains_phi(body.value):
        return _phi_reject("value")
    if not svc.repo.update_memory_value(user_id, mem_id, body.value):
        raise HTTPException(status_code=404, detail="memory item not found")
    await record_generic(claims=payload, event_class="memory", action_type="memory.update",
                         description="Updated a memory item", outputs_meta={"id": mem_id})
    return svc.repo.get_memory(user_id, mem_id)


@memory_router.delete("/{mem_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(mem_id: str, request: Request,
                        user_id: str = Depends(require_user_id),
                        payload: dict = Depends(get_current_user_payload)):
    svc = _service(request)
    if not svc.repo.delete_memory(user_id, mem_id):
        raise HTTPException(status_code=404, detail="memory item not found")
    await record_generic(claims=payload, event_class="memory", action_type="memory.delete",
                         description="Deleted a memory item", outputs_meta={"id": mem_id})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


class SkillToggleRequest(BaseModel):
    agent_id: str
    tool_name: str
    enabled: bool


def _tool_permissions(request: Request):
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        root_app = getattr(request.app, "_root_app", None) or request.app
        orch = getattr(root_app.state, "orchestrator", None)
    tp = getattr(orch, "tool_permissions", None) if orch else None
    if tp is None:
        raise HTTPException(status_code=503, detail="Tool permissions not initialized")
    return tp


@skills_router.get("")
async def list_skills(request: Request, user_id: str = Depends(require_user_id)):
    tp = _tool_permissions(request)
    catalog: List[Dict[str, Any]] = []
    for agent_id in list(getattr(tp, "_tool_scope_map", {}).keys()):
        scope_map = tp.get_tool_scope_map(agent_id)
        for tool_name, scope in scope_map.items():
            catalog.append({
                "agent_id": agent_id,
                "tool_name": tool_name,
                "scope": scope,
                "enabled": tp.is_tool_allowed(user_id, agent_id, tool_name),
                "authorized": tp.is_skill_authorized(user_id, agent_id, tool_name),
            })
    return {"skills": catalog}


@skills_router.put("")
async def toggle_skill(body: SkillToggleRequest, request: Request,
                       user_id: str = Depends(require_user_id),
                       payload: dict = Depends(get_current_user_payload)):
    tp = _tool_permissions(request)
    required_scope = tp.get_tool_scope(body.agent_id, body.tool_name)
    if body.enabled and not tp.is_skill_authorized(user_id, body.agent_id, body.tool_name):
        raise HTTPException(
            status_code=403,
            detail=f"This skill needs the '{required_scope}' permission, which you haven't been granted.",
        )
    tp.set_skill_enabled(user_id, body.agent_id, body.tool_name, body.enabled)
    await record_generic(
        claims=payload, event_class="skill",
        action_type="skill.enable" if body.enabled else "skill.disable",
        description=f"{'Enabled' if body.enabled else 'Disabled'} skill {body.agent_id}:{body.tool_name}",
        outputs_meta={"agent_id": body.agent_id, "tool_name": body.tool_name, "enabled": body.enabled},
    )
    return {"agent_id": body.agent_id, "tool_name": body.tool_name, "enabled": body.enabled}
