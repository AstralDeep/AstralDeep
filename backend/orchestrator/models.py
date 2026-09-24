"""Pydantic request and response models for AstralDeep's REST API, powering the
auto-generated OpenAPI docs. Covers chat, saved components, agents, permissions,
credentials, draft agents, and the dashboard; consumed by orchestrator/api.py.
"""

from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional
from enum import Enum


class ChatStatusEnum(str, Enum):
    idle = "idle"
    thinking = "thinking"
    executing = "executing"
    done = "done"


class ChatMessageRequest(BaseModel):
    message: str = Field(..., description="The user's message text")
    display_message: Optional[str] = Field(None, description="Optional formatted version of the message to display in the UI")

    model_config = {"json_schema_extra": {"examples": [{"message": "What is the weather in Lexington, KY?"}]}}


class ChatMessageResponse(BaseModel):
    chat_id: str = Field(..., description="The chat session ID")
    status: str = Field("accepted", description="Message acceptance status")
    message: str = Field("Message received. Results will stream via WebSocket.", description="Info message")


class ChatSummary(BaseModel):
    id: str
    title: str
    agent_id: Optional[str] = Field(
        None,
        description=(
            "Feature 013 / FR-006: the agent currently bound to this chat. "
            "NULL for legacy chats created before agent binding shipped."
        ),
    )
    updated_at: int = Field(..., description="Unix timestamp in milliseconds")
    preview: str = Field("", description="Preview of the last message")
    has_saved_components: Optional[bool] = None


class ChatMessage(BaseModel):
    role: str = Field(..., description="'user' or 'assistant'")
    content: Any = Field(..., description="Message content — string for user, component list for assistant")
    timestamp: Optional[int] = None


class ChatDetail(BaseModel):
    id: str
    title: str
    agent_id: Optional[str] = Field(
        None,
        description=(
            "Feature 013 / FR-006: the agent currently bound to this chat. "
            "NULL for legacy chats created before agent binding shipped."
        ),
    )
    updated_at: int
    messages: List[ChatMessage] = []


class ChatListResponse(BaseModel):
    chats: List[ChatSummary]


class ChatCreateRequest(BaseModel):
    agent_id: Optional[str] = Field(None, description="Agent to bind this chat to.")


class ChatCreateResponse(BaseModel):
    chat_id: str
    agent_id: Optional[str] = Field(
        None,
        description="Echo of the agent the chat is bound to (Feature 013).",
    )
    message: str = "Chat created successfully"


class ChatDetailResponse(BaseModel):
    chat: ChatDetail


class DeleteResponse(BaseModel):
    success: bool = True
    message: str = "Deleted successfully"


class ComponentSaveRequest(BaseModel):
    component_data: Dict[str, Any] = Field(..., description="The component tree (JSON object)")
    component_type: str = Field(..., description="Type of component (e.g. 'card', 'table', 'bar_chart')")
    title: Optional[str] = Field(None, description="Display title for the saved component")


class SavedComponent(BaseModel):
    id: str
    chat_id: str
    component_data: Dict[str, Any]
    component_type: str
    title: str
    created_at: int


class ComponentSaveResponse(BaseModel):
    component: SavedComponent


class ComponentListResponse(BaseModel):
    components: List[SavedComponent]


class ComponentCombineRequest(BaseModel):
    source_id: str = Field(..., description="ID of the first component")
    target_id: str = Field(..., description="ID of the second component")


class ComponentCondenseRequest(BaseModel):
    component_ids: List[str] = Field(..., min_length=2, description="IDs of components to condense")


class ComponentCombineResponse(BaseModel):
    removed_ids: List[str]
    new_components: List[SavedComponent]


class AgentTool(BaseModel):
    name: str
    description: str
    input_schema: Optional[Dict[str, Any]] = None


class AgentInfo(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    tools: List[AgentTool] = []
    scopes: Optional[Dict[str, bool]] = Field(None, description="Scope-level permissions (tools:read, tools:write, tools:search, tools:system)")
    tool_scope_map: Optional[Dict[str, str]] = Field(None, description="Map of tool_name to required scope")
    permissions: Optional[Dict[str, bool]] = Field(None, description="Per-tool permission map derived from scopes (tool_name: allowed)")
    security_flags: Optional[Dict[str, Any]] = Field(None, description="System-level security flags per tool from proactive review")
    status: str = "connected"
    owner_email: Optional[str] = Field(None, description="Email of the agent owner")
    is_public: bool = Field(False, description="Whether the agent is publicly available to all users")
    disabled: bool = Field(
        False,
        description=(
            "Feature 013 follow-up: per-user agent disable state. True ⇒ "
            "the requesting user has muted this agent; the orchestrator "
            "skips its tools without changing scopes/permissions."
        ),
    )


class AgentListResponse(BaseModel):
    agents: List[AgentInfo]


class AgentPermissionsRequest(BaseModel):
    scopes: Optional[Dict[str, bool]] = Field(None, description="Legacy four-scope map (tools:read/write/search/system). Accepted for one release.")
    tool_overrides: Optional[Dict[str, bool]] = Field(None, description="Legacy per-tool enable/disable overrides (paired with scopes).")
    per_tool_permissions: Optional[Dict[str, Dict[str, bool]]] = Field(
        None,
        description=(
            "Feature 013: per-(tool, permission_kind) toggles. Shape: "
            "{tool_name: {permission_kind: enabled}}. permission_kind ∈ "
            "{tools:read, tools:write, tools:search, tools:system}."
        ),
    )

    model_config = {"json_schema_extra": {"examples": [
        {"per_tool_permissions": {"search_web": {"tools:read": True, "tools:search": True}, "send_email": {"tools:write": False}}},
        {"scopes": {"tools:read": True, "tools:write": False, "tools:search": True, "tools:system": False}, "tool_overrides": {"some_tool": False}},
    ]}}


class AgentPermissionsResponse(BaseModel):
    agent_id: str
    agent_name: str
    scopes: Dict[str, bool] = Field(default_factory=dict, description="Legacy scope-level permissions (tools:read, tools:write, tools:search, tools:system). Echoed for transitional clients.")
    tool_scope_map: Optional[Dict[str, str]] = Field(None, description="Map of tool_name to required scope")
    permissions: Dict[str, bool] = Field(default_factory=dict, description="Per-tool boolean derived from per-tool, per-kind state (tool_name: allowed). Honors Feature 013 resolution order.")
    per_tool_permissions: Dict[str, Dict[str, bool]] = Field(
        default_factory=dict,
        description=(
            "Feature 013: resolved per-(tool, permission_kind) state. "
            "Only the kinds that apply to each tool are present (FR-014)."
        ),
    )
    tool_overrides: Dict[str, bool] = Field(default_factory=dict, description="Legacy per-tool disable overrides (only NULL-kind disable rows listed).")
    tool_descriptions: Optional[Dict[str, str]] = Field(None, description="Map of tool_name to description")
    security_flags: Optional[Dict[str, Any]] = Field(None, description="System-level security flags per tool from proactive review")


class AgentVisibilityRequest(BaseModel):
    is_public: bool = Field(..., description="Whether the agent should be publicly available")


class ToolSelectionResponse(BaseModel):
    agent_id: str
    selected_tools: Optional[List[str]] = Field(
        None,
        description="None ≡ no narrowing (default). A list ≡ the user's explicit subset.",
    )


class ToolSelectionUpdate(BaseModel):
    agent_id: str = Field(..., description="The agent the selection applies to.")
    selected_tools: List[str] = Field(..., description="Non-empty list of tool names.")


class AgentEnabledUpdate(BaseModel):
    agent_id: str = Field(..., description="The agent to toggle.")
    enabled: bool = Field(..., description="True = enabled (visible to chat); False = disabled.")


class AgentEnabledResponse(BaseModel):
    agent_id: str
    enabled: bool


class CredentialSetRequest(BaseModel):
    credentials: Dict[str, str] = Field(..., description="Map of credential_key to value (e.g. CLASSIFY_API_KEY: abc123)")

    model_config = {"json_schema_extra": {"examples": [{"credentials": {"CLASSIFY_URL": "https://classify.example.com", "CLASSIFY_API_KEY": "abc123"}}]}}


class CredentialListResponse(BaseModel):
    agent_id: str
    agent_name: str
    credential_keys: List[str] = Field(default_factory=list, description="Stored credential key names (no values)")
    required_credentials: List[Dict[str, Any]] = Field(default_factory=list, description="Credentials the agent declares it needs")
    credential_test: Optional[str] = Field(
        default=None,
        description=(
            "Verdict of the save-time credential probe. Present only when the agent exposes "
            "a `_credentials_check` tool. One of: 'ok', 'auth_failed', 'unreachable', 'unexpected'."
        ),
    )
    credential_test_detail: Optional[str] = Field(
        default=None,
        description="Human-readable explanation of a non-ok credential_test verdict.",
    )


class CredentialDeleteResponse(BaseModel):
    success: bool = True
    message: str = "Credential deleted successfully"


class ToolSpec(BaseModel):
    name: str = Field(..., description="Tool function name (snake_case)")
    description: str = Field(..., description="What the tool does")
    input_schema: Optional[Dict[str, Any]] = Field(None, description="JSON Schema for tool inputs")
    scope: str = Field("tools:read", description="Required scope: tools:read, tools:write, tools:search, tools:system")


class DraftAgentCreateRequest(BaseModel):
    agent_name: str = Field(..., min_length=2, max_length=100, description="Human-readable agent name")
    description: str = Field(..., min_length=10, description="What the agent does")
    tools: Optional[List[ToolSpec]] = Field(None, description="Tool specifications (optional — AI will generate based on description)")
    skill_tags: Optional[List[str]] = Field(None, description="Skill tags for routing")
    packages: Optional[List[str]] = Field(None, description="Python packages the agent may import (e.g., requests, pandas)")

    model_config = {"json_schema_extra": {"examples": [{"agent_name": "Stock Tracker", "description": "An agent that tracks stock prices and provides analysis", "tools": [{"name": "get_stock_price", "description": "Get current stock price by ticker symbol", "scope": "tools:read"}], "skill_tags": ["stocks", "finance"], "packages": ["requests"]}]}}


class DraftAgentRefineRequest(BaseModel):
    message: str = Field(..., min_length=1, description="What to change about the agent")


class AdminReviewRequest(BaseModel):
    decision: str = Field(..., description="'approve' or 'reject'")
    notes: Optional[str] = Field(None, description="Admin notes")


class DraftAgentResponse(BaseModel):
    id: str
    user_id: str
    agent_name: str
    agent_slug: str
    description: str
    tools_spec: Optional[Any] = None
    skill_tags: Optional[Any] = None
    packages: Optional[Any] = None
    status: str
    generation_log: Optional[Any] = None
    security_report: Optional[Any] = None
    validation_report: Optional[Any] = None
    error_message: Optional[str] = None
    port: Optional[int] = None
    review_notes: Optional[str] = None
    reviewed_by: Optional[str] = None
    refinement_history: Optional[Any] = None
    required_credentials: Optional[Any] = None
    created_at: Optional[int] = None
    updated_at: Optional[int] = None


class DraftAgentListResponse(BaseModel):
    drafts: List[DraftAgentResponse]


class DashboardResponse(BaseModel):
    agents: List[AgentInfo]
    total_tools: int
    capabilities: Dict[str, Any]


class UploadResponse(BaseModel):
    status: str = "success"
    filename: str
    file_path: str
    user_id: str


class ErrorResponse(BaseModel):
    error: str
    detail: Optional[str] = None


WS_PROTOCOL_DOCS = """
## WebSocket Protocol

Connect to `ws://<host>:<port>/ws` for real-time communication.

### Authentication

After connecting, send a `register_ui` message with your JWT token:

```json
{
    "type": "register_ui",
    "token": "<JWT_TOKEN>",
    "capabilities": ["render", "stream"],
    "session_id": "ui-<timestamp>"
}
```

### Client → Server Messages

All client messages use the `ui_event` type with an `action` field:

| Action | Payload | Description |
|--------|---------|-------------|
| `chat_message` | `{message, chat_id?, display_message?}` | Send a chat message |
| `get_history` | `{}` | Request list of recent chats |
| `load_chat` | `{chat_id}` | Load a specific chat with messages |
| `new_chat` | `{}` | Create a new chat session |
| `get_dashboard` | `{}` | Request system config/dashboard |
| `discover_agents` | `{}` | Request list of connected agents |
| `save_component` | `{chat_id, component_data, component_type, title?}` | Save a UI component |
| `get_saved_components` | `{chat_id?}` | Get saved components |
| `delete_saved_component` | `{component_id}` | Delete a saved component |
| `combine_components` | `{source_id, target_id}` | Combine two components via LLM |
| `condense_components` | `{component_ids[]}` | Condense multiple components via LLM |

**Message format:**
```json
{
    "type": "ui_event",
    "action": "<action_name>",
    "session_id": "<optional_chat_id>",
    "payload": { ... }
}
```

### Server → Client Messages

| Type | Fields | Description |
|------|--------|-------------|
| `system_config` | `{config: {agents[], total_tools}}` | Dashboard/system info |
| `agent_registered` | `{agent_id, name, tools[]}` | New agent connected |
| `agent_list` | `{agents[]}` | Full agent list |
| `chat_status` | `{status, message}` | Processing status (thinking/executing/done) |
| `chat_created` | `{payload: {chat_id, from_message}}` | New chat created |
| `chat_loaded` | `{chat: {id, title, messages[]}}` | Chat data loaded |
| `history_list` | `{chats[]}` | List of recent chats |
| `ui_render` | `{components[]}` | Render UI components (new message) |
| `ui_update` | `{components[]}` | Update current UI components |
| `ui_append` | `{components[]}` | Append to current UI components |
| `saved_components_list` | `{components[]}` | Saved components data |
| `component_saved` | `{component: {...}}` | Component save confirmation |
| `component_deleted` | `{component_id}` | Component delete confirmation |
| `combine_status` | `{status, message}` | Component combine in progress |
| `components_combined` | `{removed_ids[], new_components[]}` | Combine result |
| `components_condensed` | `{removed_ids[], new_components[]}` | Condense result |
| `combine_error` | `{error}` | Combine/condense failed |

### UI Component Types

Components are JSON objects with a `type` field:

`text`, `card`, `metric`, `table`, `grid`, `container`, `list`, `alert`,
`progress`, `bar_chart`, `line_chart`, `pie_chart`, `plotly_chart`,
`code`, `divider`, `collapsible`, `image`, `tabs`, `button`, `input`
"""
