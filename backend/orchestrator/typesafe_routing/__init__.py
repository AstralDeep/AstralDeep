"""TypeSafe System One routing adapter (feature 089).

This package is the seam between a turn and TypeSafe. Nothing outside it
imports ``typesafe_sdk``, and nothing inside it touches the orchestrator's
state: the orchestrator hands in a bounded request and gets back a decision,
or ``None``.

``None`` is the important case. It is returned for every reason routing did
not happen -- no key, circuit open, flag off, SDK missing, deadline passed,
error of any class -- and it means exactly one thing to the caller: behave as
though the user had no TypeSafe key. That is why nothing in this package
raises into a turn.
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
