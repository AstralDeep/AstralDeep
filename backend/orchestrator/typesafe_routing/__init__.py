"""Package boundary between the orchestrator and typesafe_sdk: only this package's
modules import the SDK, and every public call returns None instead of raising so a
turn behaves as if the user had no TypeSafe key.
"""

from .budget import (
    TURN_BUDGET_SECONDS,
    CircuitState,
    Deadline,
    Outcome,
    UserCircuit,
    circuit,
    set_circuit,
)
from .client import (
    TYPESAFE_API_BASE,
    TYPESAFE_MODEL,
    TypeSafeAdapterClient,
    TypeSafeUnavailable,
    adapter_client,
    set_adapter_client,
    shutdown_adapter_client,
)
from .decision import (
    RoundOnePlan,
    RoutingDecision,
    SecurityJudgment,
    Tier,
    apply_round_one,
    parse_decision,
)
from .questions import (
    AgentOption,
    QuestionSet,
    RoutingRequest,
    ToolOption,
    build_questions,
)
from .runner import (
    FEATURE_FLAG,
    RoutingNotifier,
    RoutingOutcome,
    await_decision,
    routing_enabled,
    screen_instruction,
    start_routing,
)
from .security_policy import Verdict, security_verdict

__all__ = (
    "FEATURE_FLAG",
    "TURN_BUDGET_SECONDS",
    "TYPESAFE_API_BASE",
    "TYPESAFE_MODEL",
    "AgentOption",
    "CircuitState",
    "Deadline",
    "Outcome",
    "QuestionSet",
    "RoundOnePlan",
    "RoutingDecision",
    "RoutingNotifier",
    "RoutingOutcome",
    "RoutingRequest",
    "SecurityJudgment",
    "Tier",
    "ToolOption",
    "TypeSafeAdapterClient",
    "TypeSafeUnavailable",
    "UserCircuit",
    "Verdict",
    "adapter_client",
    "apply_round_one",
    "await_decision",
    "build_questions",
    "circuit",
    "parse_decision",
    "routing_enabled",
    "screen_instruction",
    "security_verdict",
    "set_adapter_client",
    "set_circuit",
    "shutdown_adapter_client",
    "start_routing",
)
