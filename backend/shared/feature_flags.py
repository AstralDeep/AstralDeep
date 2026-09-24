"""Env-driven feature-flag registry (FeatureFlags.is_enabled) read across orchestrator,
agents, and shared modules to gate scheduler execution, recursive delegation, remote
compute, and the MCP/A2A servers.
"""

import os


class FeatureFlags:
    def __init__(self):
        self._flags = {
            "denial_loop_detection": self._read("FF_DENIAL_LOOP_DETECTION", True),
            "tool_concurrency_safety": self._read("FF_TOOL_CONCURRENCY_SAFETY", True),
            "message_compaction": self._read("FF_MESSAGE_COMPACTION", False),
            "progress_streaming": self._read("FF_PROGRESS_STREAMING", False),
            "hook_system": self._read("FF_HOOK_SYSTEM", False),
            "task_state_machine": self._read("FF_TASK_STATE_MACHINE", False),
            "coordinator_mode": self._read("FF_COORDINATOR_MODE", False),
            "knowledge_synthesis": self._read("FF_KNOWLEDGE_SYNTHESIS", False),
            "live_streaming": self._read("FF_LIVE_STREAMING", False),
            "tool_streaming": self._read("FF_TOOL_STREAMING", False),
            "agentic_creation": self._read("FF_AGENTIC_CREATION", True),
            "scheduling_chat": self._read("FF_SCHEDULING_CHAT", True),
            "scheduler_execution": self._read("FF_SCHEDULER_EXECUTION", False),
            "persistent_agents": self._read("FF_PERSISTENT_AGENTS", False),
            "memory_chat": self._read("FF_MEMORY_CHAT", True),
            "desktop_codegen": self._read("FF_DESKTOP_CODEGEN", True),
            "attachment_autoparse": self._read("FF_ATTACHMENT_AUTOPARSE", True),
            "context_engineering": self._read("FF_CONTEXT_ENGINEERING", False),
            "datamarking": self._read("FF_DATAMARKING", True),
            "inprocess_agents": self._read("FF_INPROCESS_AGENTS", True),
            "safe_agents": self._read("FF_SAFE_AGENTS", True),
            "skill_packs": self._read("FF_SKILL_PACKS", True),
            "slash_commands": self._read("FF_SLASH_COMMANDS", True),
            "user_skills": self._read("FF_USER_SKILLS", True),
            "recursive_delegation": self._read("FF_RECURSIVE_DELEGATION", False),
            "typesafe_routing": self._read("FF_TYPESAFE_ROUTING", True),
            "first_turn_contract": self._read("FF_FIRST_TURN_CONTRACT", True),
            "stream_artifacts": self._read("FF_STREAM_ARTIFACTS", True),
            "designer_all_devices": self._read("FF_DESIGNER_ALL_DEVICES", True),
            "component_refine": self._read("FF_COMPONENT_REFINE", True),
            "artifact_export": self._read("FF_ARTIFACT_EXPORT", True),
            "artifact_sharing": self._read("FF_ARTIFACT_SHARING", False),
            "bg_continuity": self._read("FF_BG_CONTINUITY", True),
            "byo_agents": self._read("FF_BYO_AGENTS", False),
            "remote_compute": self._read("FF_REMOTE_COMPUTE", False),
            "computer_use": self._read("FF_COMPUTER_USE", False),
            "mcp_server": self._read("FF_MCP_SERVER", False),
            "a2a_server": self._read("FF_A2A_SERVER", False),
            "conversational_voice": self._read(
                "FF_CONVERSATIONAL_VOICE",
                True,
            ),
            "kiosk_login": self._read("FF_KIOSK_LOGIN", False),
            "rail_caption_variant": self._read("FF_RAIL_CAPTION_VARIANT", False),
            "framework_credentials": self._read("FF_FRAMEWORK_CREDENTIALS", False),
        }

    @staticmethod
    def _read(env_var: str, default: bool) -> bool:
        return os.getenv(env_var, str(default)).lower() in ("true", "1", "yes")

    def is_enabled(self, flag: str) -> bool:
        return self._flags.get(flag, False)


flags = FeatureFlags()
