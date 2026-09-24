"""Builds ParamPicker onboarding panels for profession, skills, and personality as
create_ui_response envelopes, rendered by the existing DynamicRenderer with no new
frontend code; skill recommendations come from skills_reco.py.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from astralprims import Alert, ParamPicker, create_ui_response


def build_profession_panel(profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    profile = profile or {}
    return create_ui_response([
        ParamPicker(
            title="Tell me about your work",
            description="This personalizes your assistant. It is not medical data — just your role and goals.",
            fields=[
                {"name": "profession", "label": "Your profession / role", "kind": "text",
                 "default": profile.get("profession") or ""},
                {"name": "goals", "label": "What do you want help with? (comma-separated)",
                 "kind": "text", "default": "; ".join(profile.get("goals") or [])},
            ],
            submit_label="Save",
            submit_message_template=(
                "Save my personalization profile — profession: {profession}; goals: {goals}"
            ),
        )
    ])


def build_skills_panel(recommendations: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not recommendations:
        return create_ui_response([
            Alert(
                title="No skills available yet",
                message=(
                    "You don't have any agents enabled yet, so there are no skills to turn on. "
                    "Enable an agent from the Agents panel and come back — your assistant can chat "
                    "in the meantime."
                ),
                variant="info",
            )
        ])

    options = []
    default = []
    for r in recommendations:
        label = f"{r.get('agent_id', '?')}:{r.get('tool_name', '?')} ({r.get('scope', 'tools:read').split(':')[-1]})"
        if not r.get("available", True):
            label += " — needs permission"
        options.append(label)
        if r.get("available", True) and r.get("score", 0) > 0 and len(default) < 3:
            default.append(label)

    return create_ui_response([
        ParamPicker(
            title="Turn on skills",
            description="Pick the capabilities you want. You can change these anytime, and you only ever get access you're authorized for.",
            fields=[{"name": "skills", "label": "Recommended skills", "kind": "checklist",
                     "options": options, "default": default}],
            submit_label="Enable selected",
            submit_message_template="Enable these skills for me: {skills}",
        )
    ])


def build_personality_panel(profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    persona = (profile or {}).get("personality") or {}
    return create_ui_response([
        ParamPicker(
            title="Give your assistant a personality",
            description="How should your assistant sound? (This shapes tone only — never how it handles your data.)",
            fields=[
                {"name": "tone", "label": "Tone", "kind": "select",
                 "options": ["concise", "warm", "formal", "playful"], "default": persona.get("tone") or "concise"},
                {"name": "directness", "label": "Directness", "kind": "select",
                 "options": ["high", "balanced", "gentle"], "default": persona.get("directness") or "balanced"},
                {"name": "verbosity", "label": "Verbosity", "kind": "select",
                 "options": ["low", "medium", "high"], "default": persona.get("verbosity") or "medium"},
                {"name": "notes", "label": "Anything else? (optional)", "kind": "text",
                 "default": persona.get("notes") or ""},
            ],
            submit_label="Save personality",
            submit_message_template=(
                "Set my assistant personality — tone: {tone}; directness: {directness}; "
                "verbosity: {verbosity}; notes: {notes}"
            ),
        )
    ])
