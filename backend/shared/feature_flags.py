"""
Feature Flags — Environment-driven feature gating for safe rollout.

Usage:
    from shared.feature_flags import flags
    if flags.is_enabled("message_compaction"):
        ...
"""
import os


class FeatureFlags:
    """Simple env-var-driven feature flag registry."""

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
            # 001-tool-stream-ui: enables tool→agent→orchestrator→UI push streaming
            # via async-generator tools, the new RECONNECTING state, and per-user
            # multi-client fan-out. Default OFF for safe rollout. See
            # specs/001-tool-stream-ui/ for the full design.
            "tool_streaming": self._read("FF_TOOL_STREAMING", False),
            # 027-agentic-creation-settings: injects the orchestrator meta-tools
            # (create_capability / extend_agent) into the chat LLM's tool list so
            # the assistant can create draft agents/tools on capability gaps.
            # Default ON; gates meta-tool injection only — the settings chrome
            # ships ungated. See specs/027-agentic-creation-settings/.
            "agentic_creation": self._read("FF_AGENTIC_CREATION", True),
            # 030: injects the schedule_recurring_task meta-tool so recurring
            # work is reachable from chat (consent card before creation —
            # the feature-025 scheduler itself ships ungated). Default ON.
            "scheduling_chat": self._read("FF_SCHEDULING_CHAT", True),
            # 030-finish-soul-integration: FAIL-CLOSED gate for the scheduler
            # *execution* loop (unattended job runs under the offline-grant
            # store). Distinct from "scheduling_chat" (which only proposes jobs
            # via a consent card). Per Constitution VII this MUST stay OFF until
            # the lead-dev security review of offline_grant.py is recorded
            # (025 T057 / 030 FR-004/FR-005). Default OFF.
            "scheduler_execution": self._read("FF_SCHEDULER_EXECUTION", False),
            # 030-finish-soul-integration: injects the memory meta-tool
            # (remember / memory_search / memory_get) so the assistant can
            # actively use cross-session memory on request, mirroring
            # scheduling_chat. Passive prompt recall is unaffected. Default ON.
            "memory_chat": self._read("FF_MEMORY_CHAT", True),
            # 039-desktop-codegen-download: injects the offer_desktop_codegen
            # meta-tool so the assistant can surface a download card for the
            # Windows coding-agent .exe (GitHub-released, integrity-checked)
            # when a user asks for code that runs on their machine. Default ON;
            # surfacing a verified download link is safe.
            "desktop_codegen": self._read("FF_DESKTOP_CODEGEN", True),
            # 031-attachment-upload-parsing: when an accepted-but-unparseable
            # file type is uploaded, eagerly draft a safe backend parser by
            # reusing the 027 agentic-creation lifecycle (security gate +
            # isolated self-test + ADMIN approval + global promotion). Gates the
            # auto-creation trigger only — uploading/parsing of already-covered
            # types is unaffected. When OFF, an uncovered upload reports
            # "no reader available" instead of drafting a parser. Default ON.
            # See specs/031-attachment-upload-parsing/.
            "attachment_autoparse": self._read("FF_ATTACHMENT_AUTOPARSE", True),
            # 033 Wave-0 (C-N16 — context engineering): keep the chat system
            # prompt's stable instruction prefix cache-friendly (volatile
            # file/canvas context moved last) AND tombstone stale tool outputs
            # mid-loop so a long tool-calling turn doesn't pin volatile/untrusted
            # text in the window. Byte-identical to today when OFF. Default OFF.
            "context_engineering": self._read("FF_CONTEXT_ENGINEERING", False),
            # 033 Wave-0 (C-S4 — spotlighting/datamarking): wrap untrusted
            # (non-digest) tool output in unforgeable per-turn sentinel markers
            # and instruct the model to treat their contents as data, never
            # instructions — closing a prompt-injection channel. Composes with
            # C-N15 (a tool's _model_digest is trusted and left unmarked).
            # No-op when OFF. Default OFF.
            "datamarking": self._read("FF_DATAMARKING", False),
            # 040-inprocess-agents-skills-commands: run the nine bundled
            # first-party agents IN-PROCESS in the orchestrator (no per-agent
            # uvicorn port). The networked WS path is the kill-switch when OFF.
            # Default ON (the intended posture). See specs/040-.../.
            "inprocess_agents": self._read("FF_INPROCESS_AGENTS", True),
            # 040: owner-approved "safe" agents flip the per-call permission
            # baseline from deny→allow at check time (explicit user opt-out and
            # hard security blocks still win; no per-user rows written). When
            # OFF, the legacy default-deny applies. Default ON.
            "safe_agents": self._read("FF_SAFE_AGENTS", True),
            # 040: load authored capability/technique skill packs on demand by
            # relevance (wires get_techniques_for_agent into the turn). Bounded +
            # fail-open to today's behavior. Default ON.
            "skill_packs": self._read("FF_SKILL_PACKS", True),
            # 040: user-typed /slash-commands in chat (expand-to-prompt or a
            # defined flow), always through the permission/audit/PHI rails. A
            # "/"-prefixed message is ordinary chat when OFF (fail-open). Default ON.
            "slash_commands": self._read("FF_SLASH_COMMANDS", True),
            # 048-recursive-delegation-chains: gates minting/enforcing NESTED,
            # further-attenuated RFC 8693 `act` delegation tokens for sub-agent
            # fan-out and auto-created agents, bound to the persistent WebSocket
            # transport with per-hop provenance in the hash-chained audit. FAIL
            # CLOSED — default OFF; with the flag off the orchestrator uses the
            # single-hop delegation path unchanged (no regression). The mechanism
            # + four enforcement invariants live in orchestrator/delegation.py.
            # See specs/048-recursive-delegation-chains/.
            "recursive_delegation": self._read("FF_RECURSIVE_DELEGATION", False),
            # 055-uniform-artifacts US1: welcome components carry wel_
            # identities and clients purge them locally at turn start; the
            # turn-start welcome-blanking ui_render (which killed client
            # skeletons one RTT after send) is no longer sent. OFF restores
            # the legacy id-less welcome + blanking frame byte-for-byte.
            # Default ON (bug fix). See specs/055-uniform-artifacts/.
            "first_turn_contract": self._read("FF_FIRST_TURN_CONTRACT", True),
            # 055 US2: bridge push-streams to workspace component identities
            # (component_id on ui_stream_data/stream_subscribed frames; the
            # final streamed state persists as a normal workspace component).
            # Fail-open — OFF = today's ephemeral stream-<id> behavior. Default ON.
            "stream_artifacts": self._read("FF_STREAM_ARTIFACTS", True),
            # 055 US3: the adaptive designer runs regardless of originating
            # device; native sockets receive a materialized designed canvas
            # after turn end. OFF restores the native skip tuple. Default ON.
            "designer_all_devices": self._read("FF_DESIGNER_ALL_DEVICES", True),
            # 055 US4: component-scoped refine/restore verbs + bounded
            # per-component version history. OFF = affordances absent and the
            # actions refuse honestly. Default ON.
            "component_refine": self._read("FF_COMPONENT_REFINE", True),
            # 055 US5: authed CSV/HTML export endpoints. Default ON.
            "artifact_export": self._read("FF_ARTIFACT_EXPORT", True),
            # 055 US5: PUBLIC read-only snapshot share links (PHI-gated at
            # mint, hashed tokens, revocable). FAIL CLOSED — default OFF until
            # the operator deliberately enables public serving.
            "artifact_sharing": self._read("FF_ARTIFACT_SHARING", False),
            # 055 cross-device background-task continuity: task_started/
            # task_completed fan to every socket of the user; background
            # (VirtualWebSocket) turns mirror their chat-rail narrative +
            # terminal chat_status to real sockets on the chat; register_ui
            # session resume + durable background_task replay; the scheduled
            # fallback chat is created before the turn. Kill switch — OFF
            # restores originator-only frames byte-identically. Default ON.
            "bg_continuity": self._read("FF_BG_CONTINUITY", True),
            # 057-byo-client-agents: user-authored agents that run on the user's
            # OWN desktop (never the orchestrator) and connect inward, authored
            # through a guided Specify→Clarify→Plan→Tasks→Analyze flow validated
            # against a separate agent constitution. FAIL CLOSED — default OFF;
            # with the flag off no tunnel/registration/authoring path is reachable
            # and behavior is byte-identical to today. The untrusted-at-the-
            # boundary re-verification reuses the existing gate stack. See
            # specs/057-byo-client-agents/.
            "byo_agents": self._read("FF_BYO_AGENTS", False),
            # 063-remote-compute-agents: the two bundled SSH agents (read-only
            # remote-observe-1 + mutating remote-control-1) for cluster (Slurm)
            # jobs and host operations over a single SSH transport. FAIL CLOSED —
            # default OFF; with the flag off neither agent registers, no verb is
            # listed/invocable, the remote-machines surface is absent, and behavior
            # is byte-identical to today. Read once at import (container recreate
            # to enable). See specs/063-remote-compute-agents/.
            "remote_compute": self._read("FF_REMOTE_COMPUTE", False),
        }

    @staticmethod
    def _read(env_var: str, default: bool) -> bool:
        return os.getenv(env_var, str(default)).lower() in ("true", "1", "yes")

    def is_enabled(self, flag: str) -> bool:
        return self._flags.get(flag, False)


flags = FeatureFlags()
